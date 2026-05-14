"""Approval queue — pending-action state store (Day 10, Phase 2).

When the policy engine returns `PolicyDecision.APPROVAL_REQUIRED`, the
DecisionPipeline enqueues the action here. A human (today: an admin UI,
Day 31; tomorrow: the takehome evaluator) then calls `approve(action_id)`
or `reject(action_id, reason)` to resolve the pending row.

## State machine (mirrors `contracts.policies.ApprovalRule.state`)

    pending ─┬─▶ approved      (approve called)
             ├─▶ rejected      (reject called)
             └─▶ expired       (Day 23 — TTL sweep, not yet implemented)

Re-decisioning is forbidden — once a row leaves `pending`, both
`approve` and `reject` raise `ApprovalStateError`. This satisfies the
external take-home assessment scenario-3 "double-approve raises an
exception" check.

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


@runtime_checkable
class ApprovalQueue(Protocol):
    """The shape the DecisionPipeline depends on.

    Phase 4 (Day 21) will add a `PostgresApprovalQueue` honoring this
    same protocol — pipeline construction stays unchanged."""

    def enqueue(
        self, action_id: str, tenant_id: str, *, assigned_to: str | None = None
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
    ) -> ApprovalRule:
        """Add a new pending row. Idempotent on `action_id` —
        re-enqueueing the same action returns the existing row rather
        than raising or duplicating, which keeps the
        DecisionPipeline's duplicate-event path simple (`enqueue` is
        called from the dedup-guarded handler, but an additional guard
        here costs nothing and surfaces the invariant)."""
        if not action_id:
            raise ValueError("action_id must be non-empty")
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
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
                enqueued_at=_now(),
            )
            self._by_id[action_id] = rule
            self._pending_by_tenant.setdefault(tenant_id, []).append(action_id)
            return rule

    def approve(
        self, action_id: str, *, decided_by: str | None = None
    ) -> ApprovalRule:
        """Transition a pending row to `approved`. Raises if absent or
        already resolved."""
        return self._resolve(
            action_id, target_state="approved", decided_by=decided_by, note=None
        )

    def reject(
        self,
        action_id: str,
        *,
        reason: str = "",
        decided_by: str | None = None,
    ) -> ApprovalRule:
        """Transition a pending row to `rejected`. Raises if absent or
        already resolved. `reason` is the audit note."""
        return self._resolve(
            action_id,
            target_state="rejected",
            decided_by=decided_by,
            note=reason or None,
        )

    def _resolve(
        self,
        action_id: str,
        *,
        target_state: str,
        decided_by: str | None,
        note: str | None,
    ) -> ApprovalRule:
        with self._lock:
            rule = self._by_id.get(action_id)
            if rule is None:
                raise ApprovalNotFoundError(
                    f"no approval row for action_id={action_id!r}"
                )
            if rule.state != "pending":
                raise ApprovalStateError(
                    f"approval for action_id={action_id!r} is already "
                    f"{rule.state!r}; cannot transition to {target_state!r}"
                )

            # Pydantic v2 models default to mutable on `extra="forbid"`
            # unless `frozen=True` is set (it isn't for ApprovalRule).
            # `model_copy(update=...)` is the recommended pattern.
            resolved = rule.model_copy(
                update={
                    "state": target_state,
                    "decided_at": _now(),
                    "decided_by": decided_by,
                    "decision_note": note,
                    "row_version": rule.row_version + 1,
                }
            )
            self._by_id[action_id] = resolved
            pending = self._pending_by_tenant.get(rule.tenant_id)
            if pending is not None:
                # Drop the action_id from the tenant's pending list. We
                # use list.remove (O(N)) over a set because per-tenant
                # queue sizes are bounded by approval throughput
                # (hundreds, not millions) and a list preserves
                # enqueue order for FIFO listing — which the admin UI
                # depends on for "oldest first" sorting.
                try:
                    pending.remove(action_id)
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
