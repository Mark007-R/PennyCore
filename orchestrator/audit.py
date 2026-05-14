"""Audit log writer (Day 10, Phase 2).

The audit log is the orchestrator's compliance surface. Every decision
the system makes — proposal generation, policy gating, human approval,
execution success/failure — lands here as a single `AuditLogEntry`
row. Auditability rule 16 (every action proposal, approval, rejection,
and execution writes to the audit log) is enforced by the
DecisionPipeline; this module is the storage backend.

## Append-only by construction

The `InMemoryAuditLog` only exposes `write` and read helpers — there is
no `update` or `delete` method on the public surface. The Postgres-backed
implementation will rely on the trigger from migration 0002
(`BEFORE UPDATE/DELETE`) to enforce append-only at the storage layer.
Both paths land at the same invariant: rows can be inserted, never
modified.

## Why a separate module rather than living on the queue

The audit log is read by callers who don't care about the approval
queue (the admin UI's audit explorer, the Day-22 load tester's "show
me the last 100 decisions" trace endpoint). Coupling it to the queue
would force those callers to know about pending state. Keeping it
free-standing matches the Postgres schema (table 12, `audit_log`) — no
foreign key cycles, no transactional coupling beyond the per-decision
batch.

## Multi-tenant invariant

`_by_tenant: dict[tenant_id, list[AuditLogEntry]]`. Reads always go
through the tenant-scoped index — the global `_entries` list is for
ID assignment and aggregate metrics only. A `tenant_id` mismatch never
returns another tenant's rows.
"""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable

from contracts.audit import AuditLogEntry


@runtime_checkable
class AuditLog(Protocol):
    """The shape the DecisionPipeline + approval API depend on."""

    def write(self, entry: AuditLogEntry) -> AuditLogEntry: ...

    def entries_for_tenant(self, tenant_id: str) -> list[AuditLogEntry]: ...

    def entries_for_action(self, action_id: str) -> list[AuditLogEntry]: ...

    def entries_for_event(self, event_id: str) -> list[AuditLogEntry]: ...


class InMemoryAuditLog:
    """Append-only log of `AuditLogEntry`. Thread-safe.

    `id` on the contract is `int | None` because the Postgres-backed
    impl assigns it via `BIGSERIAL`. In-memory mode synthesizes the
    same monotonically-increasing ID from a counter so callers and
    tests can rely on `entry.id` after `write` returns.

    The contract model is `frozen=True`, so we `model_copy(update=...)`
    to add the assigned ID rather than mutating the supplied entry —
    that keeps the "audit log entries are immutable" invariant honest
    at the Python layer too.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: list[AuditLogEntry] = []
        self._by_tenant: dict[str, list[AuditLogEntry]] = {}
        self._by_action: dict[str, list[AuditLogEntry]] = {}
        self._by_event: dict[str, list[AuditLogEntry]] = {}
        self._next_id: int = 1

    def write(self, entry: AuditLogEntry) -> AuditLogEntry:
        """Append `entry` to the log, return it with an assigned ID."""
        with self._lock:
            assigned = entry.model_copy(update={"id": self._next_id})
            self._next_id += 1
            self._entries.append(assigned)
            self._by_tenant.setdefault(assigned.tenant_id, []).append(assigned)
            if assigned.action_id is not None:
                self._by_action.setdefault(assigned.action_id, []).append(assigned)
            if assigned.caused_by_event_id is not None:
                self._by_event.setdefault(
                    assigned.caused_by_event_id, []
                ).append(assigned)
            return assigned

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    def entries_for_tenant(self, tenant_id: str) -> list[AuditLogEntry]:
        with self._lock:
            return list(self._by_tenant.get(tenant_id, []))

    def entries_for_action(self, action_id: str) -> list[AuditLogEntry]:
        with self._lock:
            return list(self._by_action.get(action_id, []))

    def entries_for_event(self, event_id: str) -> list[AuditLogEntry]:
        with self._lock:
            return list(self._by_event.get(event_id, []))

    def all_entries(self) -> list[AuditLogEntry]:
        """Aggregate view — used by metrics + Phase-3 benchmarking
        only. Production read paths must go through the tenant-scoped
        helper above."""
        with self._lock:
            return list(self._entries)

    def clear(self) -> None:
        """Reset every index. Used by tests."""
        with self._lock:
            self._entries.clear()
            self._by_tenant.clear()
            self._by_action.clear()
            self._by_event.clear()
            self._next_id = 1
