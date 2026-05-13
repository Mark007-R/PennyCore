"""Policy engines for the orchestrator (Day 10, Phase 2).

The policy engine sits between the planner's `ActionProposal` and the
executor / approval queue. Given (tenant_id, action_type), it returns a
`PolicyDecision` from `contracts.policies`:

  * `AUTO`              — pipeline executes immediately (audit logs it).
  * `APPROVAL_REQUIRED` — pipeline enqueues for human review.
  * `REJECT`            — pipeline declines the action; no execution path.

Day 10 ships ONE implementation — `DeclarativePolicyEngine` — a dict-backed
champion that satisfies the external scorecard's `configure_policies(...)`
contract. Phase 3 (Days 16-18) introduces three competitors:
`python_rules`, `llm_judge`, `naive`, all sharing the same `PolicyEngine`
Protocol. Picking the champion is the Phase-3 deliverable; today the
declarative engine is locked in for the Phase-2 MVP and for the takehome
adapter.

Multi-tenant invariant (rule 15): every engine method takes `tenant_id` —
there is no global / cross-tenant config path. The declarative store is a
two-level dict (`{tenant_id: {action_type: decision}}`) so a tenant's
policy mutation cannot reach another tenant by construction.
"""

from __future__ import annotations

from orchestrator.policy.declarative import (
    DeclarativePolicyEngine,
    PolicyEngine,
    UnknownPolicyDecisionError,
)

__all__ = [
    "DeclarativePolicyEngine",
    "PolicyEngine",
    "UnknownPolicyDecisionError",
]
