"""Defer-queue for operations that trip a `with_deadline` cap
(Day 23 — Phase 4 hardening, backfill).

The SKILL Day-23 line says: **Postgres slow → timeout + queue**.
`context_engine/timeouts.py` shipped the *timeout* half — when a slow
downstream call exceeds the configured cap, `with_deadline` returns a
caller-supplied fallback so the request thread can move on. That keeps
the request hot path safe but leaves the work itself dropped on the
floor.

This module is the *queue* half. When a deadline trips, the caller can
push a `DeferredCall(operation, payload, reason, deferred_at)` onto a
bounded per-tenant ring buffer; a background reconciler (Phase 6
durability work, or a manual operator pass) can drain the queue later
when the downstream is healthy again.

Multi-tenant invariant (rule 15): records are scoped by tenant_id;
the public `pending()` accessor REQUIRES tenant_id. Same isolation
properties as `RecentEventsBuffer` (Day 8), `ProposalsBuffer` (Day 9),
and `QuarantineBuffer` (Day 23 today) — RLock-guarded, per-tenant
ring buffer, no global accessor.

Why a SEPARATE module from the quarantine buffer:

  * **Different lifecycle.** Quarantine entries are "we couldn't even
    parse this — operator triage required". Defer queue entries are
    "we parsed this fine, but a downstream took too long — retry
    when it's healthy". An operator dashboard shows them in
    different panes.
  * **Different reason taxonomy.** `QuarantineReason` is about
    *input* failures (schema, missing key, unknown customer);
    `DeferReason` is about *downstream* failures (deadline exceeded,
    transient error from the callable).
  * **Different retention policy.** Phase 6 will likely drain the
    defer queue automatically (a reconciler tries the operation
    again every N seconds); quarantine entries persist until an
    operator looks at them. Keeping the modules separate lets each
    grow its own policy independently.

Why this is in-memory: same rationale as `QuarantineBuffer` — Day 23
ships the IN-PROCESS shape because the failure mode it addresses is
"the work disappeared", not "the work was lost in a crash". Phase 6
(Day 29-32) replaces the ring with a Postgres-backed `deferred_calls`
table without changing this API.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any, Callable

from context_engine.timeouts import DeadlineResult, with_deadline


class DeferReason(str, Enum):
    """Why the call was deferred. Coarse-grained — the verbose detail
    lives in `DeferredCall.detail`."""

    DEADLINE_EXCEEDED = "deadline_exceeded"
    TRANSIENT_ERROR = "transient_error"
    EXPLICIT_DEFER = "explicit_defer"  # caller deferred without trying


@dataclass(frozen=True)
class DeferredCall:
    """One row in the defer-queue ring buffer.

    `payload` is whatever the caller wants the reconciler to see —
    typically the kwargs needed to retry the operation. Storing it as
    a `dict` (not a closure / Future) keeps the buffer
    pickle-portable for Phase 6's durable-store swap.
    """

    id: str
    tenant_id: str
    operation: str
    reason: DeferReason
    detail: str
    payload: dict[str, Any]
    deferred_at: datetime
    elapsed_ms: float = 0.0
    context: dict[str, Any] = field(default_factory=dict)


def _new_defer_id() -> str:
    return f"def_{uuid.uuid4().hex[:24]}"


class SlowCallQueue:
    """Bounded per-tenant ring buffer for deferred operations.

    Same isolation properties as `QuarantineBuffer` and friends. The
    public surface is intentionally tiny — `defer()` pushes, `pending()`
    reads, `drain()` removes-and-returns for reconciler runs, and
    `clear()` is test-only.
    """

    def __init__(self, max_per_tenant: int = 500) -> None:
        if max_per_tenant <= 0:
            raise ValueError(
                f"max_per_tenant must be positive, got {max_per_tenant}"
            )
        self._max = max_per_tenant
        self._lock = RLock()
        self._by_tenant: dict[str, list[DeferredCall]] = {}

    def defer(
        self,
        *,
        tenant_id: str,
        operation: str,
        reason: DeferReason,
        detail: str,
        payload: dict[str, Any] | None = None,
        elapsed_ms: float = 0.0,
        context: dict[str, Any] | None = None,
        id_factory: Callable[[], str] = _new_defer_id,
    ) -> DeferredCall:
        if not tenant_id:
            raise ValueError("tenant_id required (use a sentinel if unknown)")
        entry = DeferredCall(
            id=id_factory(),
            tenant_id=tenant_id,
            operation=operation,
            reason=reason,
            detail=detail,
            payload=dict(payload or {}),
            deferred_at=datetime.now(timezone.utc),
            elapsed_ms=elapsed_ms,
            context=dict(context or {}),
        )
        with self._lock:
            buf = self._by_tenant.setdefault(tenant_id, [])
            buf.append(entry)
            if len(buf) > self._max:
                del buf[: len(buf) - self._max]
        return entry

    def pending(self, tenant_id: str, limit: int = 100) -> list[DeferredCall]:
        """Newest-first view of pending deferred calls for ONE tenant.
        Returns a copy so the caller can iterate safely."""
        if limit <= 0:
            return []
        with self._lock:
            buf = self._by_tenant.get(tenant_id, [])
            return list(reversed(buf[-limit:]))

    def drain(self, tenant_id: str, limit: int = 100) -> list[DeferredCall]:
        """Pop up to `limit` oldest entries for the reconciler to retry.

        FIFO order (oldest first) — the operation that has been waiting
        longest gets the retry budget first. Caller is responsible for
        re-deferring on retry failure.
        """
        if limit <= 0:
            return []
        with self._lock:
            buf = self._by_tenant.get(tenant_id, [])
            take = buf[:limit]
            self._by_tenant[tenant_id] = buf[limit:]
            return take

    def count(self, tenant_id: str) -> int:
        with self._lock:
            return len(self._by_tenant.get(tenant_id, []))

    def clear(self) -> None:
        """Test-only — production never clears the defer queue
        wholesale; the reconciler always drains."""
        with self._lock:
            self._by_tenant.clear()


# Module-level singleton, same pattern as `get_quarantine()`.
_default_queue = SlowCallQueue()


def get_defer_queue() -> SlowCallQueue:
    return _default_queue


# ---------------------------------------------------------------------------
# Convenience wrapper — `with_deadline` + auto-defer on degraded result.
# ---------------------------------------------------------------------------


def with_deadline_and_defer(
    func: Callable[[], Any],
    *,
    timeout_ms: int,
    fallback: Any,
    operation: str,
    tenant_id: str,
    defer_queue: SlowCallQueue | None = None,
    defer_payload: dict[str, Any] | None = None,
    defer_context: dict[str, Any] | None = None,
) -> DeadlineResult[Any]:
    """Run `func` under a wall-clock cap; on degraded outcome, push
    the operation onto the per-tenant defer queue for later retry.

    This is the canonical "timeout + queue" primitive the SKILL §Day-23
    line item calls for. Callers that want only the timeout half use
    `context_engine.timeouts.with_deadline` directly; callers that
    want both pass `defer_payload` (the args needed to retry) and the
    `tenant_id` for routing.

    The deferral happens AFTER the deadline result is computed, so the
    request thread is already free — no caller-visible latency cost
    beyond the failed call itself. Errors from the deferral path
    (queue full, lock contention) are swallowed; the helper's job is
    graceful degradation, not propagation.
    """
    result = with_deadline(
        func, timeout_ms=timeout_ms, fallback=fallback, operation=operation
    )
    if result.degraded:
        queue = defer_queue if defer_queue is not None else get_defer_queue()
        # Distinguish the two degraded-modes the deadline helper
        # surfaces: a `DeadlineExceeded` error means we cut it off, any
        # other exception means the callable itself raised. Phase 6
        # reconciler behaviour likely differs for the two (retry the
        # deadline-exceeded case eagerly, hold the transient-error case
        # behind a backoff).
        from context_engine.timeouts import DeadlineExceeded  # local to avoid cycle

        reason = (
            DeferReason.DEADLINE_EXCEEDED
            if isinstance(result.error, DeadlineExceeded)
            else DeferReason.TRANSIENT_ERROR
        )
        detail = (
            f"{type(result.error).__name__}: {result.error}"
            if result.error is not None
            else f"{operation} returned degraded result without explicit error"
        )
        try:
            queue.defer(
                tenant_id=tenant_id,
                operation=operation,
                reason=reason,
                detail=detail,
                payload=dict(defer_payload or {}),
                elapsed_ms=result.elapsed_ms,
                context=dict(defer_context or {}),
            )
        except Exception:  # pylint: disable=broad-except
            # The defer helper itself must never raise — the request
            # already has a degraded-but-safe value to return.
            pass
    return result


__all__ = [
    "DeferReason",
    "DeferredCall",
    "SlowCallQueue",
    "get_defer_queue",
    "with_deadline_and_defer",
]
