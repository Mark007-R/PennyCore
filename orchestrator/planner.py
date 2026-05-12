"""orchestrator LLM planner (Day 9, Phase 2).

Given an inbound event envelope (and, optionally, a brief from the
context-engine), decide what ONE action the system should propose.
The result is always an `ActionProposal` (the pre-policy shape from
`contracts.actions`); the Day-10 policy engine takes it from there.

## Design choices

**Why "tool calling" maps to a strict-JSON `complete()` prompt rather
than provider-native tool API.** Anthropic, Azure Foundry, and OpenAI
each expose tool calling differently — different field names, different
streaming semantics, different validation guarantees. The dispatch layer
in `context_engine/llm` was kept intentionally narrow (`complete` +
`summarize`) so the planner can stay provider-portable. We use a strict
JSON-output system prompt + post-parse + Pydantic validation against the
`ActionType` enum, which gives the same correctness guarantees as
provider-native tool calling (structured output, schema-validated)
without coupling to any one provider's tool API. Day 15's LLM-as-judge
follows the same pattern, so the two add up to a single coherent
strategy.

**Why ONE proposal per event today.** Phase 3 will compare 4 policy
strategies, two of which (LLM-as-judge, naive baseline) accept multiple
proposals. For Day 9 — MVP path — the simplest contract is one event
→ one proposal. The `ActionProposal` shape allows the Phase-3 strategies
to fan out into multiple by calling the planner N times with different
prompts, or by introducing a `propose_actions` (plural) variant later.
No need to over-engineer the Day-9 surface.

**Why `proposed_by=fallback` on ANY LLM failure.** The rule table is the
degraded path — it should be marked in the audit log whenever the LLM
didn't produce the proposal. SYSTEM_DESIGN §5.5 mandates this so the
Day-10 audit log surfaces "this was a fallback decision, the LLM was
unavailable / produced bad JSON / chose an unknown action_type" to
compliance reviewers. The `ProposedBy` enum in `contracts.actions`
exists for exactly this signal.

**Fallback rule table.** Hardcoded mapping event_type → action_type
covering the Phase-2 mortgage demo. Conservative by design — when in
doubt, escalate to a loan officer rather than auto-text the borrower.
Phase 3's policy-engine comparison will revisit these mappings under
benchmark; for now they're "obviously correct for the demo" rather
than tuned.

## Multi-tenant invariant (rule 15)

Every proposal carries `tenant_id` from the envelope. The planner does
NOT consult any tenant-scoped store (no DB read here) — but if Phase 3
adds per-tenant prompt customization, the lookup will be scoped by the
envelope's `tenant_id` before any LLM call.

## Audit invariant (rule 16)

The planner itself does not write to the audit log — that's the Day-10
executor's job. But the `proposed_by` field is the hook the audit
writer keys on to distinguish "LLM said this" from "rule table said
this because LLM was degraded".
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable

from context_engine.llm import LLMClient, get_client
from contracts import ActionProposal, ActionType, ProposedBy

_LOG = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Fallback rule table (SYSTEM_DESIGN §5.5).
#
# Conservative defaults for the Phase-2 mortgage demo:
#   * customer-originated text → acknowledge the customer (send_borrower_message)
#   * docs / status / anomalies → notify the loan officer (human in the loop)
#   * system_event → no_op (e.g. heartbeats shouldn't trigger action)
#
# Phase 3's policy benchmark will measure whether these picks are
# correct on a 200-tuple ground truth set. If the LLM consistently
# outperforms the rules, we'll narrow the fallback scope.
# ----------------------------------------------------------------------------

_FALLBACK_RULES: dict[str, ActionType] = {
    "message_received": ActionType.SEND_BORROWER_MESSAGE,
    "document_uploaded": ActionType.UPDATE_STATUS,
    "status_changed": ActionType.NOTIFY_LOAN_OFFICER,
    "anomaly_detected": ActionType.NOTIFY_LOAN_OFFICER,
    "system_event": ActionType.NO_OP,
}

# Conservative default for unknown event types — escalate to a human.
_FALLBACK_DEFAULT: ActionType = ActionType.NOTIFY_LOAN_OFFICER


# ----------------------------------------------------------------------------
# LLM prompt — strict-JSON output. Mirrored by `_parse_llm_response`.
# ----------------------------------------------------------------------------

_PLANNER_SYSTEM_PROMPT = """\
You are the planner for a customer-service AI orchestrator. Given a
customer event and a short customer brief, decide ONE action the system
should propose. Output ONLY a JSON object with EXACTLY this shape — no
prose, no markdown fences, no preamble:

{"action_type": "<one of: send_borrower_message, notify_loan_officer, schedule_call, request_document, update_status, no_op>",
 "reasoning": "<one short sentence on why>",
 "payload": {"<key>": "<value>"}}

Rules:
- "action_type" MUST be one of the listed values. Never invent a new one.
- "payload" MUST be a JSON object (possibly empty `{}`). For send_borrower_message,
  include a "message" key with the text to send. For notify_loan_officer, include
  a "reason" key. For request_document, include a "document_type" key.
- When in doubt, choose "notify_loan_officer" — a human can always escalate up
  or down from there. Never choose "no_op" unless the event is clearly a system
  heartbeat or duplicate.
- "reasoning" is for the audit log, not for the customer. Keep it factual and
  one sentence."""


_VALID_ACTION_VALUES: set[str] = {at.value for at in ActionType}


# ----------------------------------------------------------------------------
# Request + factory
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanRequest:
    """Inputs to a planning decision.

    `envelope` is the dict the event listener received (shape defined by
    `context_engine.event_bus.envelope_for`). `brief_text` is the
    customer brief from context-engine's assembler — empty string when
    no customer is linked or when the brief lookup failed.
    """

    envelope: dict[str, Any]
    brief_text: str = ""


def _default_proposal_id() -> str:
    """UUID4 for new proposals — same shape `context_engine.ingestion`
    uses for event IDs. Stable, sortable-enough for the diagnostic
    surface, opaque enough to not leak structure to clients."""
    return f"prop_{uuid.uuid4().hex}"


# ----------------------------------------------------------------------------
# Public surface
# ----------------------------------------------------------------------------


def propose_action(
    request: PlanRequest,
    *,
    client: LLMClient | None = None,
    proposal_id_factory: Callable[[], str] = _default_proposal_id,
) -> ActionProposal:
    """Produce a single `ActionProposal` for the supplied event.

    Resolution path:
      1. If `client` is a mock adapter (name == "mock"), short-circuit
         to the rule table. Mock-mode benchmarks must be deterministic
         and free of JSON-parse luck.
      2. Otherwise call `client.complete()` with the strict-JSON system
         prompt + the envelope/brief as the user message, parse, validate
         the action_type against the enum, and emit a proposal with
         `proposed_by=ProposedBy.LLM`.
      3. On ANY failure in step 2 (network error, JSONDecodeError,
         unknown action_type, missing fields), fall through to the rule
         table with `proposed_by=ProposedBy.FALLBACK`. The failure is
         logged at WARNING so degraded mode is visible in logs without
         silencing alarms.

    The function never raises on LLM failure — the audit invariant
    requires that every event has either a successful proposal or a
    fallback proposal. There is no "no proposal" outcome.
    """
    tenant_id = request.envelope.get("tenant_id")
    event_id = request.envelope.get("event_id")
    if not tenant_id or not event_id:
        raise ValueError(
            f"envelope missing required fields: tenant_id={tenant_id!r}, "
            f"event_id={event_id!r}"
        )

    client = client if client is not None else get_client()

    if client.name == "mock":
        return _fallback_proposal(request, proposal_id_factory)

    try:
        raw = client.complete(
            prompt=_format_user_prompt(request),
            system=_PLANNER_SYSTEM_PROMPT,
            max_tokens=256,
            temperature=0.0,
        )
    except Exception as exc:  # network / SDK errors
        _LOG.warning(
            "planner LLM call failed (%s: %s); falling back to rule table",
            type(exc).__name__,
            exc,
        )
        return _fallback_proposal(request, proposal_id_factory)

    parsed = _parse_llm_response(raw)
    if parsed is None:
        _LOG.warning(
            "planner LLM produced unparseable response; falling back to rule table"
        )
        return _fallback_proposal(request, proposal_id_factory)

    action_type, payload = parsed
    return ActionProposal(
        id=proposal_id_factory(),
        tenant_id=tenant_id,
        event_id=event_id,
        customer_id=request.envelope.get("customer_id"),
        action_type=action_type,
        proposed_by=ProposedBy.LLM,
        payload=payload,
    )


# ----------------------------------------------------------------------------
# Internals — prompt formatting + parsing + rule-table fallback
# ----------------------------------------------------------------------------


def _format_user_prompt(request: PlanRequest) -> str:
    """Render the envelope + brief as the user-side prompt. The shape is
    deliberately stable — Phase 3 prompt-comparison runs will diff
    against this baseline."""
    env = request.envelope
    brief_block = (
        f"Customer brief:\n{request.brief_text}\n"
        if request.brief_text
        else "Customer brief: (no brief available — customer not yet linked or brief lookup failed)\n"
    )
    return (
        f"Event:\n"
        f"  event_id: {env.get('event_id')}\n"
        f"  tenant_id: {env.get('tenant_id')}\n"
        f"  customer_id: {env.get('customer_id') or '(unlinked)'}\n"
        f"  channel_code: {env.get('channel_code')}\n"
        f"  event_type: {env.get('event_type')}\n"
        f"  received_at: {env.get('received_at')}\n"
        f"\n{brief_block}"
        f"\nReturn ONE JSON object as instructed."
    )


def _parse_llm_response(raw: str) -> tuple[ActionType, dict[str, Any]] | None:
    """Parse the JSON-only LLM response. Returns `None` on ANY shape
    violation so the caller can fall back without a try/except chain.

    Tolerates leading/trailing whitespace and a single markdown code
    fence (some models still emit ```json despite system prompt
    instructions). Does NOT tolerate trailing prose — that's a hard
    schema violation and the caller will fall back.
    """
    if not raw:
        return None

    cleaned = raw.strip()
    # Strip a single ```json ... ``` fence if present.
    if cleaned.startswith("```"):
        # Drop the opening fence line.
        lines = cleaned.splitlines()
        # First line is the fence (possibly ```json); drop it.
        lines = lines[1:]
        # If a closing fence is present, drop everything from it on.
        for i, line in enumerate(lines):
            if line.strip().startswith("```"):
                lines = lines[:i]
                break
        cleaned = "\n".join(lines).strip()

    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    if not isinstance(obj, dict):
        return None

    action_str = obj.get("action_type")
    if not isinstance(action_str, str) or action_str not in _VALID_ACTION_VALUES:
        return None

    payload = obj.get("payload", {})
    if not isinstance(payload, dict):
        # An LLM that returns a list or string for payload is malformed
        # per our schema. Fall back rather than coerce — coercion would
        # hide real prompt-following regressions.
        return None

    # `reasoning` is captured in the payload so the Day-10 audit log
    # can surface it without a separate column.
    reasoning = obj.get("reasoning")
    if isinstance(reasoning, str) and reasoning and "_planner_reasoning" not in payload:
        payload = {**payload, "_planner_reasoning": reasoning}

    return ActionType(action_str), payload


def _fallback_proposal(
    request: PlanRequest,
    proposal_id_factory: Callable[[], str],
) -> ActionProposal:
    """Build an `ActionProposal` from the rule table.

    `proposed_by=ProposedBy.FALLBACK` is the audit signal. The payload
    carries `_planner_reasoning` explaining the rule that fired, so the
    Day-10 audit log can surface the rationale identically to an
    LLM-generated proposal.
    """
    envelope = request.envelope
    event_type = envelope.get("event_type", "")
    action_type = _FALLBACK_RULES.get(event_type, _FALLBACK_DEFAULT)

    reason = (
        f"rule-table fallback for event_type={event_type!r}"
        if event_type in _FALLBACK_RULES
        else f"rule-table default (unknown event_type={event_type!r}, escalating)"
    )

    return ActionProposal(
        id=proposal_id_factory(),
        tenant_id=envelope["tenant_id"],
        event_id=envelope["event_id"],
        customer_id=envelope.get("customer_id"),
        action_type=action_type,
        proposed_by=ProposedBy.FALLBACK,
        payload={"_planner_reasoning": reason},
    )


# ----------------------------------------------------------------------------
# Diagnostic surface — tenant-scoped ring buffer for /proposals/recent.
#
# Mirrors `RecentEventsBuffer` in event_listener.py one-for-one. The
# Day-10 audit log will become the canonical record; this is purely for
# operator visibility during Phase 2.
# ----------------------------------------------------------------------------


class ProposalsBuffer:
    """Tenant-scoped ring buffer for `GET /proposals/recent`.

    Same isolation properties as `RecentEventsBuffer`: bounded
    per-tenant, RLock-guarded, dropping cross-tenant queries silently.
    Keeping the buffer type separate from RecentEventsBuffer (rather
    than parameterizing it) reads more clearly at the call site and
    leaves room for proposal-specific filters in Phase 3.
    """

    def __init__(self, max_per_tenant: int = 50) -> None:
        self._max = max_per_tenant
        self._lock = RLock()
        self._by_tenant: dict[str, list[ActionProposal]] = {}

    def append(self, proposal: ActionProposal) -> None:
        with self._lock:
            buf = self._by_tenant.setdefault(proposal.tenant_id, [])
            buf.append(proposal)
            # Drop oldest if over cap. `deque(maxlen=)` would be cheaper
            # but proposals are infrequent and the visibility of a list
            # under test inspection is worth the O(N) trim.
            if len(buf) > self._max:
                del buf[: len(buf) - self._max]

    def recent(self, tenant_id: str, limit: int = 20) -> list[ActionProposal]:
        with self._lock:
            buf = self._by_tenant.get(tenant_id, [])
            return list(reversed(buf[-limit:]))

    def clear(self) -> None:
        with self._lock:
            self._by_tenant.clear()


def make_planning_handler(
    proposals: ProposalsBuffer,
    *,
    client_factory: Callable[[], LLMClient] = get_client,
    proposal_id_factory: Callable[[], str] = _default_proposal_id,
) -> Callable[[dict[str, Any]], None]:
    """Build a listener handler that runs the planner on every envelope
    and appends the proposal to `proposals`.

    Composable with the Day-8 buffered handler via `chain_handlers`:
    the event_listener can route a single publish through multiple
    handlers without coupling the planner to the recent-events buffer.
    """

    def _handle(envelope: dict[str, Any]) -> None:
        try:
            request = PlanRequest(envelope=envelope, brief_text="")
            proposal = propose_action(
                request,
                client=client_factory(),
                proposal_id_factory=proposal_id_factory,
            )
        except Exception as exc:
            # Handler must never raise out of the listener — the Day-8
            # RedisEventListener swallows handler exceptions, but the
            # in-memory bus's subscribe loop also depends on this
            # contract. Log + drop rather than poison the listener.
            _LOG.warning(
                "planner handler swallowed unexpected error (%s: %s) — "
                "event will be retried only if the publisher resends",
                type(exc).__name__,
                exc,
            )
            return
        proposals.append(proposal)

    return _handle


def chain_handlers(
    *handlers: Callable[[dict[str, Any]], None],
) -> Callable[[dict[str, Any]], None]:
    """Run multiple handlers in order on each envelope. A bug in one
    handler does NOT stop later handlers — the listener invariant from
    Day 8 (subscriber bugs do not poison the publisher) extends here to
    "one handler's bug does not poison the next handler in the chain".
    """

    def _handle(envelope: dict[str, Any]) -> None:
        for h in handlers:
            try:
                h(envelope)
            except Exception as exc:
                _LOG.warning(
                    "chained handler %r raised %s: %s; continuing chain",
                    getattr(h, "__name__", repr(h)),
                    type(exc).__name__,
                    exc,
                )
                continue

    return _handle
