"""Naive LLM policy engine — Phase 3 (Day 17) baseline.

This is the "just ask the LLM" approach most AI startups ship in 2025-
2026. The tenant policy is rendered as English narrative and stuffed
into the prompt, and the LLM is asked in free text which decision to
return. The benchmark question this engine answers is: "how much does
the structured policy table actually buy us compared to letting the
LLM read the policy in plain English?"

## What this engine deliberately does badly

* **Free-form output.** No JSON schema, no enum constraint — the
  engine looks for the words ``auto`` / ``approval_required`` /
  ``reject`` in the response and falls back to ``APPROVAL_REQUIRED``
  on ambiguity. This is the realistic shape of "naive" code in the
  wild — promoting structure to the system prompt only happens once
  someone gets bitten.
* **One LLM call per decision.** Even for tenants whose policy says
  "always require approval", every decision pays the LLM cost. The
  declarative engine resolves the same query in O(1) dict lookup.
* **Tenant policy as English.** Compliance teams find this easier to
  read; the LLM finds it harder to follow exactly. The benchmark
  exposes that tradeoff numerically.

## Mock-mode behaviour (deterministic, intentionally biased)

The mock client can't read the English policy block — it just echoes
the prompt. In mock mode we fall back to an event-type heuristic that
**ignores the tenant policy table entirely**, mirroring the real
failure mode of a small / cheap LLM that over-indexes on event
semantics rather than tenant rules. This is the right shape for the
comparison study: it surfaces "naive LLM forgets that Tenant X has
different rules from Tenant Y" without needing a real LLM call.

## Multi-tenant invariant (rule 15)

The policy narrative is stored per-tenant — `_policy_text[tenant_id]`.
The LLM prompt is constructed fresh per call from that tenant's
narrative; no shared mutable state can leak across tenants.

## Audit invariant (rule 16)

Every decision call increments `llm_call_count` and accumulates
estimated token counts. The orchestrator's audit log captures the
free-form reasoning string the LLM produced so compliance can
reconstruct WHY a decision was made — even though the reasoning is
free-form rather than structured.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from context_engine.llm import LLMClient, get_client
from contracts.actions import ActionType
from contracts.policies import PolicyDecision

from orchestrator.policy.declarative import _normalize_action_type

_LOG = logging.getLogger(__name__)


# Event-type heuristic used in mock mode. Intentionally tenant-agnostic
# — that's what makes it "naive". Numbers chosen to match the failure
# mode of a small LLM that learned generic customer-service patterns
# rather than tenant-specific rules.
_NAIVE_MOCK_HEURISTIC: dict[str, PolicyDecision] = {
    "message_received": PolicyDecision.AUTO,
    "document_uploaded": PolicyDecision.APPROVAL_REQUIRED,
    "status_changed": PolicyDecision.APPROVAL_REQUIRED,
    "anomaly_detected": PolicyDecision.APPROVAL_REQUIRED,
    "system_event": PolicyDecision.AUTO,
}
_NAIVE_MOCK_DEFAULT = PolicyDecision.APPROVAL_REQUIRED


_NAIVE_SYSTEM_PROMPT = """\
You are a customer-service AI policy engine. Given a tenant's policy
described in English and a proposed action, decide whether the action
should auto-execute, require human approval, or be rejected outright.

Respond with the single word "auto", "approval_required", or
"reject", followed by one short sentence explaining why."""


class NaivePolicyEngine:
    """LLM-driven free-form policy engine (the naive baseline)."""

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client if client is not None else get_client()
        self._lock = threading.RLock()
        self._policy_text: dict[str, str] = {}
        self._raw_tables: dict[str, dict[str, str]] = {}
        # Metrics — read by the Day-17 benchmark harness for cost
        # accounting. ``reset_metrics`` clears between runs.
        self.llm_call_count: int = 0
        self.llm_input_tokens_estimate: int = 0
        self.llm_output_tokens_estimate: int = 0

    def reset_metrics(self) -> None:
        with self._lock:
            self.llm_call_count = 0
            self.llm_input_tokens_estimate = 0
            self.llm_output_tokens_estimate = 0

    def set_policies(
        self, tenant_id: str, policies: dict[str, str]
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        for key, value in policies.items():
            if not isinstance(value, str):
                raise ValueError(
                    f"policy decision must be a string, got "
                    f"{type(value).__name__} for key {key!r}"
                )
        narrative = self._render_narrative(tenant_id, policies)
        with self._lock:
            self._policy_text[tenant_id] = narrative
            self._raw_tables[tenant_id] = dict(policies)

    @staticmethod
    def _render_narrative(
        tenant_id: str, policies: dict[str, str]
    ) -> str:
        """Render the policy dict as English narrative the LLM can read.

        Deliberately verbose — this is the prompt shape a naive engineer
        would write before realising structured output is cheaper.
        """
        head = (
            f"Tenant {tenant_id!r} has the following customer-service "
            f"policy rules:"
        )
        body_lines: list[str] = []
        for key, value in sorted(policies.items()):
            if key == "default":
                body_lines.append(
                    f"  - For any action not listed below, the system "
                    f"should treat it as '{value}'."
                )
            elif key == "*":
                body_lines.append(
                    f"  - For any action, the system should treat it "
                    f"as '{value}'."
                )
            else:
                body_lines.append(
                    f"  - When the proposed action is '{key}', the "
                    f"system should treat it as '{value}'."
                )
        return head + "\n" + "\n".join(body_lines)

    def decide(
        self,
        tenant_id: str,
        action_type: ActionType | str,
        event_context: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        action_str = _normalize_action_type(action_type)
        with self._lock:
            policy_text = self._policy_text.get(
                tenant_id, "(no policy configured for this tenant)"
            )

        event_type = (event_context or {}).get("event_type", "unknown")
        user_prompt = (
            f"Policy narrative:\n{policy_text}\n\n"
            f"Proposed action: {action_str}\n"
            f"Source event type: {event_type}\n\n"
            f"What should the system do?"
        )

        with self._lock:
            self.llm_call_count += 1
            self.llm_input_tokens_estimate += (
                len(_NAIVE_SYSTEM_PROMPT) + len(user_prompt)
            ) // 4

        try:
            response = self._client.complete(
                prompt=user_prompt,
                system=_NAIVE_SYSTEM_PROMPT,
                max_tokens=128,
                temperature=0.0,
            )
        except Exception as exc:
            _LOG.warning(
                "naive policy LLM call failed (%s: %s); "
                "defaulting to approval_required",
                type(exc).__name__,
                exc,
            )
            return PolicyDecision.APPROVAL_REQUIRED

        with self._lock:
            self.llm_output_tokens_estimate += len(response) // 4

        return self._parse_decision(response, event_type)

    def _parse_decision(
        self, response: str, event_type: str
    ) -> PolicyDecision:
        """Extract a PolicyDecision from free-form LLM text.

        Mock-mode short-circuit: the mock client echoes the prompt, so
        looking for the keywords matches every time and returns the
        wrong answer. Detect that path and run the heuristic instead —
        this is the documented mock-mode behaviour for naive (see
        module docstring).
        """
        if self._client.name == "mock":
            return _NAIVE_MOCK_HEURISTIC.get(event_type, _NAIVE_MOCK_DEFAULT)

        lower = response.lower()
        # Order matters — "approval_required" contains "auto" only
        # accidentally, but we want to match the more specific token
        # first to be safe on real-LLM output that includes both.
        if "reject" in lower:
            return PolicyDecision.REJECT
        if "approval_required" in lower or "approval required" in lower:
            return PolicyDecision.APPROVAL_REQUIRED
        if "approval" in lower:
            return PolicyDecision.APPROVAL_REQUIRED
        if "auto" in lower:
            return PolicyDecision.AUTO
        # Ambiguous / unparseable → safe default. This mirrors the
        # "fallback to approval_required when in doubt" pattern the
        # external scorecard recommends in its D9 rubric.
        return PolicyDecision.APPROVAL_REQUIRED

    def tenants(self) -> list[str]:
        with self._lock:
            return sorted(self._policy_text.keys())

    def policies_for(self, tenant_id: str) -> dict[str, str]:
        with self._lock:
            return dict(self._raw_tables.get(tenant_id, {}))

    def clear(self) -> None:
        with self._lock:
            self._policy_text.clear()
            self._raw_tables.clear()
            self.llm_call_count = 0
            self.llm_input_tokens_estimate = 0
            self.llm_output_tokens_estimate = 0
