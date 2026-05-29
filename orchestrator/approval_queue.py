"""Approval queue — pending-action state store (Day 10, Phase 2).

When the policy engine returns `PolicyDecision.APPROVAL_REQUIRED`, the
DecisionPipeline enqueues the action here. A human (today: an admin UI,
Day 31; tomorrow: the takehome evaluator) then calls `approve(action_id)`
or `reject(action_id, reason)` to resolve the pending row.

## State machine (mirrors `contracts.policies.ApprovalRule.state`)

    pending ─┬─▶ approved      (quorum of approvals reached)
             ├─▶ rejected      (any one eligible reject — veto)
             └─▶ expired       (Day 23 — TTL sweep, not yet implemented)

Re-decisioning is forbidden — once a row leaves `pending`, both
`approve` and `reject` raise `ApprovalStateError`. This satisfies the
external take-home assessment scenario-3 "double-approve raises an
exception" check.

## N-of-M quorum (Day 26, Phase 5)

`enqueue(... required_approvals=N, eligible_approvers=[...])` arms a row
for parallel approval: it needs N *distinct* approvers (drawn from the M
eligible names, if a pool is supplied) before it resolves to `approved`.
Each `approve(action_id, decided_by=...)` records one vote:

  * If the voter is outside the eligible pool → `ApproverNotEligibleError`.
  * If the voter already voted on this row → `DuplicateApproverError`
    (a person can't satisfy two seats of the quorum).
  * If recording the vote reaches N → the row transitions to `approved`.
  * Otherwise the row stays `pending` and `approve` returns the
    partially-approved row (`approvals_remaining > 0`).

`reject` is a veto — a single eligible rejection resolves the row to
`rejected` no matter how many approvals have accumulated. The safe
default for a compliance surface: any one reviewer can stop an action.

`required_approvals=1` with no pool (the default) is the exact Day-10
single-approver behavior — one `approve` call resolves the row — so the
takehome adapter and the Phase-2 pipeline are unchanged.

## Multi-tenant invariant (rule 15)

Two-level index: `_by_id` (action_id → ApprovalRule) for O(1)
approve/reject, and `_by_tenant` (tenant_id → set[action_id]) for
tenant-scoped listing. The pending listing path NEVER iterates the
global `_by_id` map — it walks the tenant's set instead, so a
mis-supplied tenant_id cannot see another tenant's queue even by bug.

## Why in-memory for Day 10

The Postgres `approval_queue` table exists (DDL §0001 — table 11), but
wiring the orchestrator's approval flow through psycopg today would
couple the takehome adapter to a live DB. The Phase-2 milestone is
"runs end-to-end under docker-compose"; the adapter under
`takehome/orchestrator/` is instantiated with NO arguments per the
evaluator contract, so it must work without a DB connection. The
in-memory store satisfies both. Day 23 (hardening) re-points the
production wiring at the Postgres-backed implementation; the in-memory
variant stays for the takehome adapter and unit tests.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from contracts.policies import ApprovalRule


def _default_queue_id() -> str:
    """Stable, unique queue-row IDs. Same `uuid4().hex` pattern the
    planner uses for proposal IDs."""
    return f"appr_{uuid.uuid4().hex}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalNotFoundError(LookupError):
    """Raised when approve/reject is called for an unknown action_id."""


class ApprovalStateError(RuntimeError):
    """Raised when approve/reject is called on a row that's already
    been resolved (state != "pending"). The external take-home
    assessment scenario 3 asserts this on a double-approve."""


class ApproverNotEligibleError(PermissionError):
    """Raised when `decided_by` is not in the row's `eligible_approvers`
    pool (or is missing on a quorum row that requires named voters). The
    row is left untouched — an ineligible vote never counts toward
    quorum and never vetoes."""


class DuplicateApproverError(RuntimeError):
    """Raised when the same `decided_by` tries to approve a quorum row a
    second time. One approver satisfies at most one seat of an N-of-M
    quorum; a repeat vote is rejected rather than silently ignored so
    the caller (and the audit trail) sees the attempt."""


@runtime_checkable
class ApprovalQueue(Protocol):
    """The shape the DecisionPipeline depends on.

    Phase 4 (Day 21) will add a `PostgresApprovalQueue` honoring this
    same protocol — pipeline construction stays unchanged."""

    def enqueue(
        self,
        action_id: str,
        tenant_id: str,
        *,
        assigned_to: str | None = None,
        required_approvals: int = 1,
        eligible_approvers: list[str] | None = None,
    ) -> ApprovalRule: ...

    def list_pending(self, tenant_id: str) -> list[ApprovalRule]: ...

    def approve(
        self, action_id: str, *, decided_by: str | None = None
    ) -> ApprovalRule: ...

    def reject(
        self,
        action_id: str,
        *,
        reason: str = "",
        decided_by: str | None = None,
    ) -> ApprovalRule: ...

    def get(self, action_id: str) -> ApprovalRule | None: ...

    def is_pending(self, action_id: str) -> bool: ...


class InMemoryApprovalQueue:
    """Dict + per-tenant set implementation of `ApprovalQueue`.

    Thread-safe via RLock. Same trade-offs as
    `DeclarativePolicyEngine` — single-process safe today, ready for
    the Postgres swap in Phase 4.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: dict[str, ApprovalRule] = {}
        self._pending_by_tenant: dict[str, list[str]] = {}

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------

    def enqueue(
        self,
        action_id: str,
        tenant_id: str,
        *,
        assigned_to: str | None = None,
        required_approvals: int = 1,
        eligible_approvers: list[str] | None = None,
    ) -> ApprovalRule:
        """Add a new pending row. Idempotent on `action_id` —
        re-enqueueing the same action returns the existing row rather
        than raising or duplicating, which keeps the
        DecisionPipeline's duplicate-event path simple (`enqueue` is
        called from the dedup-guarded handler, but an additional guard
        here costs nothing and surfaces the invariant).

        `required_approvals` (N) and `eligible_approvers` (the M pool)
        arm the row for N-of-M quorum. Defaults reproduce single-approver
        behavior. A pool smaller than the quorum can never resolve, so
        that misconfiguration is rejected here, at the config boundary,
        rather than surfacing as a stuck row later."""
        if not action_id:
            raise ValueError("action_id must be non-empty")
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        if required_approvals < 1:
            raise ValueError("required_approvals must be >= 1")
        if eligible_approvers is not None:
            if len(set(eligible_approvers)) != len(eligible_approvers):
                raise ValueError("eligible_approvers must not contain duplicates")
            if required_approvals > len(eligible_approvers):
                raise ValueError(
                    f"required_approvals={required_approvals} exceeds the "
                    f"eligible pool size {len(eligible_approvers)} — quorum "
                    f"could never be reached"
                )
        with self._lock:
            existing = self._by_id.get(action_id)
            if existing is not None:
                return existing
            rule = ApprovalRule(
                id=_default_queue_id(),
                tenant_id=tenant_id,
                action_id=action_id,
                state="pending",
                assigned_to=assigned_to,
                required_approvals=required_approvals,
                eligible_approvers=(
                    list(eligible_approvers)
                    if eligible_approvers is not None
                    else None
                ),
                enqueued_at=_now(),
            )
            self._by_id[action_id] = rule
            self._pending_by_tenant.setdefault(tenant_id, []).append(action_id)
            return rule

    def approve(
        self, action_id: str, *, decided_by: str | None = None
    ) -> ApprovalRule:
        """Record one approval vote toward the row's quorum.

        For the default single-approver row (`required_approvals=1`),
        the first vote resolves the row to `approved` — identical to the
        Day-10 behavior. For an N-of-M row, the vote is recorded and the
        row stays `pending` until N distinct approvers have voted, then
        transitions to `approved`.

        Raises:
          * `ApprovalNotFoundError` — no row for `action_id`.
          * `ApprovalStateError` — the row is already resolved.
          * `ApproverNotEligibleError` — `decided_by` is outside the
             eligible pool.
          * `DuplicateApproverError` — `decided_by` already voted on
             this row.

        Returns the row as it stands after the vote — `approved` if
        quorum was reached, otherwise the still-`pending` row with the
        new voter appended to `approvals`.
        """
        with self._lock:
            rule = self._require_pending(action_id, action="approve")
            self._check_eligible(rule, decided_by)

            if rule.required_approvals <= 1:
                # Single-approver fast path: one vote resolves the row.
                # decided_by may be None (back-compat) — no distinct-voter
                # bookkeeping is needed because the row resolves now.
                recorded = list(rule.approvals)
                if decided_by is not None and decided_by not in recorded:
                    recorded.append(decided_by)
                return self._resolve(
                    rule,
                    target_state="approved",
                    decided_by=decided_by,
                    note=None,
                    approvals=recorded,
                )

            # Quorum path — named, distinct voters required.
            if decided_by is None:
                raise ApproverNotEligibleError(
                    f"approval for action_id={action_id!r} requires a named "
                    f"approver (decided_by) because it needs "
                    f"{rule.required_approvals} distinct approvals"
                )
            if decided_by in rule.approvals:
                raise DuplicateApproverError(
                    f"{decided_by!r} has already approved action_id="
                    f"{action_id!r}; one approver cannot fill two quorum seats"
                )

            recorded = [*rule.approvals, decided_by]
            if len(recorded) >= rule.required_approvals:
                return self._resolve(
                    rule,
                    target_state="approved",
                    decided_by=decided_by,
                    note=None,
                    approvals=recorded,
                )

            # Partial quorum — persist the vote, stay pending.
            updated = rule.model_copy(
                update={
                    "approvals": recorded,
                    "row_version": rule.row_version + 1,
                }
            )
            self._by_id[action_id] = updated
            return updated

    def reject(
        self,
        action_id: str,
        *,
        reason: str = "",
        decided_by: str | None = None,
    ) -> ApprovalRule:
        """Veto a pending row — a single eligible rejection resolves it
        to `rejected` regardless of how many approvals have accrued.
        Raises if absent, already resolved, or the voter is ineligible.
        `reason` is the audit note."""
        with self._lock:
            rule = self._require_pending(action_id, action="reject")
            self._check_eligible(rule, decided_by)
            return self._resolve(
                rule,
                target_state="rejected",
                decided_by=decided_by,
                note=reason or None,
                approvals=list(rule.approvals),
            )

    def _require_pending(self, action_id: str, *, action: str) -> ApprovalRule:
        """Look up a row and assert it's still `pending`. Shared by
        approve + reject so both raise the same not-found / already-resolved
        errors."""
        rule = self._by_id.get(action_id)
        if rule is None:
            raise ApprovalNotFoundError(
                f"no approval row for action_id={action_id!r}"
            )
        if rule.state != "pending":
            raise ApprovalStateError(
                f"approval for action_id={action_id!r} is already "
                f"{rule.state!r}; cannot {action}"
            )
        return rule

    @staticmethod
    def _check_eligible(rule: ApprovalRule, decided_by: str | None) -> None:
        """Enforce the eligible-approver pool. A row with no pool accepts
        any voter (including an anonymous one); a row with a pool rejects
        anyone outside it. Applied to both approve and reject so an
        outsider can neither vote nor veto."""
        if rule.eligible_approvers is None:
            return
        if decided_by is None or decided_by not in rule.eligible_approvers:
            raise ApproverNotEligibleError(
                f"{decided_by!r} is not an eligible approver for action_id="
                f"{rule.action_id!r}"
            )

    def _resolve(
        self,
        rule: ApprovalRule,
        *,
        target_state: str,
        decided_by: str | None,
        note: str | None,
        approvals: list[str],
    ) -> ApprovalRule:
        """Terminal transition of a pending row to `target_state`. The
        caller has already verified the row is pending and the voter is
        eligible. Drops the row from the tenant's pending list."""
        # Pydantic v2 models default to mutable on `extra="forbid"`
        # unless `frozen=True` is set (it isn't for ApprovalRule).
        # `model_copy(update=...)` is the recommended pattern.
        resolved = rule.model_copy(
            update={
                "state": target_state,
                "approvals": approvals,
                "decided_at": _now(),
                "decided_by": decided_by,
                "decision_note": note,
                "row_version": rule.row_version + 1,
            }
        )
        self._by_id[rule.action_id] = resolved
        pending = self._pending_by_tenant.get(rule.tenant_id)
        if pending is not None:
            # Drop the action_id from the tenant's pending list. We
            # use list.remove (O(N)) over a set because per-tenant
            # queue sizes are bounded by approval throughput
            # (hundreds, not millions) and a list preserves
            # enqueue order for FIFO listing — which the admin UI
            # depends on for "oldest first" sorting.
            try:
                pending.remove(rule.action_id)
            except ValueError:
                pass
        return resolved

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    def list_pending(self, tenant_id: str) -> list[ApprovalRule]:
        """Return all `state=pending` rows for `tenant_id`, FIFO."""
        with self._lock:
            ids = list(self._pending_by_tenant.get(tenant_id, []))
            return [self._by_id[a] for a in ids if a in self._by_id]

    def get(self, action_id: str) -> ApprovalRule | None:
        with self._lock:
            return self._by_id.get(action_id)

    def is_pending(self, action_id: str) -> bool:
        with self._lock:
            rule = self._by_id.get(action_id)
            return rule is not None and rule.state == "pending"

    def clear(self) -> None:
        """Drop every row. Used by tests."""
        with self._lock:
            self._by_id.clear()
            self._pending_by_tenant.clear()
