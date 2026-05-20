"""LLM-as-judge policy engine — Phase 3 (Day 17) competitor.

Same tenant policy table as the declarative engine, but every decision
goes through an LLM "judge" that consumes the policy as JSON and emits
a structured JSON decision. The benchmark question: does promoting the
LLM to "judge" with strict output earn back its cost?

## What makes this different from :mod:`orchestrator.policy.naive`

* **Structured output.** The LLM is constrained to emit
  ``{"decision": "auto"|"approval_required"|"reject"}`` — no free
  text. Parse failures fall back to the safe default with a logged
  warning, but mock-mode and well-behaved real LLMs produce parseable
  responses every time.
* **Policy as JSON, not English.** The LLM receives the same dict the
  declarative engine consults. This neutralises the "LLM
  misinterpreted the English narrative" failure mode that hurts naive
  — LLM-as-judge fails (when it does) on prompt-following, not on
  policy comprehension.
* **Audit-friendly.** The structured response shape lets the audit
  log key the reasoning to a known schema. Auditability rubric: 4/5
  (LLM-judged but with structured trace) vs declarative's 5/5
  (deterministic, no LLM in path).

## Mock-mode behaviour (deterministic; mirrors a perfect LLM)

In mock mode the engine **emulates a perfectly-prompt-following LLM**
by reading the policy table directly. This is the correct shape for
the comparison study — it isolates the cost question (how much does
the LLM call charge us per 100 decisions?) from the correctness
question (does the LLM produce the right answer?). A real-LLM
benchmark in Phase 5 will re-run with an actual model and may show
prompt-following drops on edge cases — that's expected and is the
research deliverable for Day 27.

## Multi-tenant invariant (rule 15)

Per-tenant policy tables stored in ``_tables``. Each decide call
serialises the lookup tenant's table into the prompt; no
cross-tenant access path exists.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from context_engine.llm import LLMClient, get_client
from contracts.actions import ActionType
from contracts.policies import PolicyDecision

from orchestrator.policy.declarative import (
    _DEFAULT_KEY,
    _WILDCARD_KEY,
    _normalize_action_type,
    _normalize_decision,
)

_LOG = logging.getLogger(__name__)


_JUDGE_SYSTEM_PROMPT = """\
You are a strict policy evaluator for a customer-service AI system.

You will receive a tenant's policy as a JSON object mapping action
types to decisions, plus a proposed action. Look up the exact
action_type in the policy table. If absent, use the "default" key. If
"default" is absent, return "approval_required" (the safe default).

Respond with EXACTLY a JSON object of the shape:
  {"decision": "<auto|approval_required|reject>", "reasoning": "<short>"}

No prose, no markdown fences, no preamble."""


class LLMJudgePolicyEngine:
    """Structured LLM-as-judge engine over a JSON policy table."""

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client if client is not None else get_client()
        self._lock = threading.RLock()
        self._tables: dict[str, dict[str, PolicyDecision]] = {}
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
        normalized: dict[str, PolicyDecision] = {}
        for key, value in policies.items():
            if not isinstance(value, str):
                raise ValueError(
                    f"policy decision must be a string, got "
                    f"{type(value).__name__} for key {key!r}"
                )
            normalized[key] = _normalize_decision(value)
        with self._lock:
            self._tables[tenant_id] = normalized

    def decide(
        self,
        tenant_id: str,
        action_type: ActionType | str,
        event_context: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        del event_context  # judge consults policy table only on Day-17
        action_str = _normalize_action_type(action_type)
        with self._lock:
            table = self._tables.get(tenant_id, {})

        policy_json = json.dumps(
            {k: v.value for k, v in table.items()}, sort_keys=True
        )
        user_prompt = (
            f"Tenant policy table (JSON):\n{policy_json}\n\n"
            f"Proposed action_type: {action_str}\n\n"
            f"Return ONLY the JSON object as instructed."
        )

        with self._lock:
            self.llm_call_count += 1
            self.llm_input_tokens_estimate += (
                len(_JUDGE_SYSTEM_PROMPT) + len(user_prompt)
            ) // 4

        # Mock-mode short-circuit. The mock client echoes the prompt,
        # so a real ``json.loads(response)`` would fail every time.
        # Documented behaviour: emulate a perfectly-prompt-following
        # LLM by reading the table directly (see module docstring).
        if self._client.name == "mock":
            decision = self._resolve_from_table(table, action_str)
            with self._lock:
                self.llm_output_tokens_estimate += 32  # nominal short JSON
            return decision

        try:
            response = self._client.complete(
                prompt=user_prompt,
                system=_JUDGE_SYSTEM_PROMPT,
                max_tokens=128,
                temperature=0.0,
            )
        except Exception as exc:
            _LOG.warning(
                "llm-judge policy call failed (%s: %s); "
                "falling back to safe default approval_required",
                type(exc).__name__,
                exc,
            )
            return PolicyDecision.APPROVAL_REQUIRED

        with self._lock:
            self.llm_output_tokens_estimate += len(response) // 4

        return self._parse_judge_response(response)

    @staticmethod
    def _resolve_from_table(
        table: dict[str, PolicyDecision], action_str: str
    ) -> PolicyDecision:
        """Apply the declarative resolution order on the policy table.

        Used in mock mode to emulate a perfectly-prompt-following LLM.
        Matches :class:`DeclarativePolicyEngine`'s decide path exactly
        so the mock-mode head-to-head measures cost / latency, not
        correctness drift.
        """
        if not table:
            return PolicyDecision.APPROVAL_REQUIRED
        if action_str in table:
            return table[action_str]
        if _DEFAULT_KEY in table:
            return table[_DEFAULT_KEY]
        if _WILDCARD_KEY in table:
            return table[_WILDCARD_KEY]
        return PolicyDecision.APPROVAL_REQUIRED

    @staticmethod
    def _parse_judge_response(raw: str) -> PolicyDecision:
        """Strict JSON parse; safe default on any shape violation.

        Tolerates a single markdown ``json`` code fence (some models
        ignore the "no fences" instruction). Does NOT tolerate
        trailing prose — that's a schema violation and the safe
        default fires.
        """
        if not raw:
            return PolicyDecision.APPROVAL_REQUIRED
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()[1:]
            for i, line in enumerate(lines):
                if line.strip().startswith("```"):
                    lines = lines[:i]
                    break
            cleaned = "\n".join(lines).strip()
        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError:
            return PolicyDecision.APPROVAL_REQUIRED
        if not isinstance(obj, dict):
            return PolicyDecision.APPROVAL_REQUIRED
        decision_str = obj.get("decision")
        if not isinstance(decision_str, str):
            return PolicyDecision.APPROVAL_REQUIRED
        try:
            return PolicyDecision(decision_str)
        except ValueError:
            return PolicyDecision.APPROVAL_REQUIRED

    def tenants(self) -> list[str]:
        with self._lock:
            return sorted(self._tables.keys())

    def policies_for(self, tenant_id: str) -> dict[str, str]:
        with self._lock:
            table = self._tables.get(tenant_id, {})
            return {k: v.value for k, v in table.items()}

    def clear(self) -> None:
        with self._lock:
            self._tables.clear()
            self.llm_call_count = 0
            self.llm_input_tokens_estimate = 0
            self.llm_output_tokens_estimate = 0
