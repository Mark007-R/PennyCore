"""Quorum policy — maps (tenant, action_type) → N-of-M approval rule
(Day 26, Phase 5).

The Phase-3 champion policy engine (`DeclarativePolicyEngine`) answers a
single question: *should this action auto-execute, queue for approval, or
be rejected?* It deliberately knows nothing about *how many* approvers a
queued action needs. That keeps the champion small and the Day-17
comparison clean.

Parallel-approval ("needs N-of-M approvers") is a separate concern, so it
lives in a separate, composable layer rather than bloating the policy
engine. The `DecisionPipeline` consults a `QuorumPolicy` only on the
`approval_required` branch, to decide how to *arm* the queue row. A tenant
with no quorum rule configured falls back to single-approver — the exact
Phase-2 behavior — so this layer is invisible until someone opts in.

## Why config-on-the-pipeline rather than on the policy table

A `Policy` row already carries a `body` JSONB that *could* hold quorum
config. Threading it through `decide()` would mean either widening the
engine's return type (touching all four Phase-3 engines + the benchmark
harness) or having the pipeline re-read the policy body after the
decision. Both are more invasive than a small side map keyed the same way
the engine keys its own state — `(tenant_id, action_type)`. When the
production wiring moves to Postgres, this map becomes a
`SELECT ... FROM approval_quorum_rules` lookup behind the same `resolve`
shape; nothing else changes.

Multi-tenant invariant (rule 15): rules are keyed by `(tenant_id,
action_type)` with no shared cross-tenant default beyond the universal
single-approver fallback.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from contracts.actions import ActionType

# The universal fallback: one approver, open pool. Shared frozen instance —
# safe because it's immutable.
_SINGLE_APPROVER: QuorumRule  # forward ref, defined below


@dataclass(frozen=True)
class QuorumRule:
    """How many distinct approvals an action needs, and who may vote.

    `eligible_approvers=None` means any approver may vote (an open pool);
    a tuple constrains voting to those named approvers (the M in N-of-M).
    Frozen + hashable so it can be shared without defensive copying.
    """

    required_approvals: int = 1
    eligible_approvers: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.required_approvals < 1:
            raise ValueError("required_approvals must be >= 1")
        if self.eligible_approvers is not None:
            if len(set(self.eligible_approvers)) != len(self.eligible_approvers):
                raise ValueError("eligible_approvers must not contain duplicates")
            if self.required_approvals > len(self.eligible_approvers):
                raise ValueError(
                    f"required_approvals={self.required_approvals} exceeds "
                    f"eligible pool size {len(self.eligible_approvers)}"
                )


_SINGLE_APPROVER = QuorumRule()


class QuorumPolicy:
    """Per-(tenant, action_type) quorum configuration.

    Thread-safe via RLock (same posture as the policy engine + queue).
    `resolve` always returns a rule — the single-approver fallback when
    nothing is configured — so callers never branch on ``None``.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rules: dict[tuple[str, ActionType], QuorumRule] = {}

    def set_rule(
        self,
        tenant_id: str,
        action_type: ActionType,
        *,
        required_approvals: int,
        eligible_approvers: list[str] | tuple[str, ...] | None = None,
    ) -> QuorumRule:
        """Configure the quorum for one tenant's action type. Validates
        eagerly via `QuorumRule.__post_init__` so a bad config fails at
        set time, not at vote time."""
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        rule = QuorumRule(
            required_approvals=required_approvals,
            eligible_approvers=(
                tuple(eligible_approvers)
                if eligible_approvers is not None
                else None
            ),
        )
        with self._lock:
            self._rules[(tenant_id, action_type)] = rule
        return rule

    def resolve(self, tenant_id: str, action_type: ActionType) -> QuorumRule:
        """Return the quorum rule for `(tenant_id, action_type)`, or the
        universal single-approver fallback if none is configured."""
        with self._lock:
            return self._rules.get((tenant_id, action_type), _SINGLE_APPROVER)

    def clear(self) -> None:
        """Drop all configured rules. Used by tests."""
        with self._lock:
            self._rules.clear()
