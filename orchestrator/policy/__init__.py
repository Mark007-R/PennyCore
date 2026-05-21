"""Policy engines for the orchestrator.

Day 10 shipped one implementation — :class:`DeclarativePolicyEngine` —
the dict-backed champion that satisfies the external scorecard's
``configure_policies(...)`` contract. Day 17 (Phase 3) adds three
competitors so the comparison study can pick a champion on
correctness, latency, cost, auditability, and maintainability:

* :class:`PythonRulesPolicyEngine` — same policy table compiled into a
  per-tenant Python callable. Demonstrates the cost of expressiveness
  over a config dict.
* :class:`NaivePolicyEngine` — "just ask the LLM" baseline; the policy
  is rendered as English narrative and the LLM emits free-form text.
* :class:`LLMJudgePolicyEngine` — structured LLM-as-judge over a JSON
  policy table; same correctness as declarative in mock mode, real
  LLM cost.

All four engines share the ``set_policies(tenant_id, dict)`` /
``decide(tenant_id, action_type, event_context=None)`` shape so the
Day-17 benchmark harness can drop them into the same evaluation loop
without per-engine glue code.

Multi-tenant invariant (rule 15): every engine keys its internal
state by ``tenant_id`` with no shared mutable cross-tenant table.
"""

from __future__ import annotations

from orchestrator.policy.declarative import (
    DeclarativePolicyEngine,
    PolicyEngine,
    UnknownPolicyDecisionError,
)
from orchestrator.policy.llm_judge import LLMJudgePolicyEngine
from orchestrator.policy.naive import NaivePolicyEngine
from orchestrator.policy.python_rules import PythonRulesPolicyEngine

__all__ = [
    "DeclarativePolicyEngine",
    "LLMJudgePolicyEngine",
    "NaivePolicyEngine",
    "PolicyEngine",
    "PythonRulesPolicyEngine",
    "UnknownPolicyDecisionError",
]
