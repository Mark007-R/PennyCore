"""Day 23 backfill — defer-queue tests.

The SKILL Day-23 line item is **Postgres slow → timeout + queue**.
The Day-23 main commit shipped the timeout half (`with_deadline`);
this file covers the queue half — `SlowCallQueue` and the
`with_deadline_and_defer` convenience wrapper that pushes onto the
queue when a deadline trips.

Coverage:
  * Buffer mechanics (defer / pending / drain / count / clear) —
    tenant isolation, FIFO eviction, RLock safety on concurrent
    writers.
  * Wrapper semantics — happy path doesn't defer; timeout path defers
    with `DEADLINE_EXCEEDED`; raise path defers with `TRANSIENT_ERROR`;
    defer-helper failure swallowed.
  * API surface — `GET /deferred/pending` returns the right shape,
    rejects missing tenant_id, enforces the limit bound.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from context_engine.api import (
    app,
    get_bus,
    get_customer_repo,
    get_defer_queue,
    get_quarantine,
    get_repo,
)
from context_engine.customer_repository import InMemoryCustomerRepository
from context_engine.event_bus import InMemoryEventBus
from context_engine.quarantine import QuarantineBuffer
from context_engine.repository import InMemoryEventRepository
from context_engine.slow_call_queue import (
    DeferReason,
    SlowCallQueue,
    with_deadline_and_defer,
)


# ============================================================================
# Buffer mechanics
# ============================================================================


class TestSlowCallQueueUnit:
    def test_defer_returns_entry_with_server_stamped_fields(self) -> None:
        q = SlowCallQueue()
        entry = q.defer(
            tenant_id="bank-a",
            operation="brief_assembly.load_history",
            reason=DeferReason.DEADLINE_EXCEEDED,
            detail="exceeded 250ms",
            payload={"customer_id": "cust_001"},
            elapsed_ms=270.0,
        )
        assert entry.id.startswith("def_")
        assert entry.tenant_id == "bank-a"
        assert entry.operation == "brief_assembly.load_history"
        assert entry.reason is DeferReason.DEADLINE_EXCEEDED
        assert entry.payload == {"customer_id": "cust_001"}
        assert entry.elapsed_ms == 270.0
        assert q.count("bank-a") == 1

    def test_tenant_isolation_on_pending(self) -> None:
        q = SlowCallQueue()
        q.defer(
            tenant_id="bank-a",
            operation="op",
            reason=DeferReason.DEADLINE_EXCEEDED,
            detail="a",
        )
        q.defer(
            tenant_id="bank-b",
            operation="op",
            reason=DeferReason.DEADLINE_EXCEEDED,
            detail="b",
        )
        assert len(q.pending("bank-a")) == 1
        assert len(q.pending("bank-b")) == 1
        assert q.pending("bank-c") == []

    def test_ring_buffer_evicts_oldest(self) -> None:
        q = SlowCallQueue(max_per_tenant=3)
        for i in range(5):
            q.defer(
                tenant_id="t",
                operation="op",
                reason=DeferReason.TRANSIENT_ERROR,
                detail=str(i),
                payload={"i": i},
            )
        kept = q.pending("t")
        # Newest-first ordering — i=4 is freshest, oldest two evicted
        assert [e.payload["i"] for e in kept] == [4, 3, 2]
        assert q.count("t") == 3

    def test_drain_pops_oldest_first(self) -> None:
        q = SlowCallQueue()
        for i in range(5):
            q.defer(
                tenant_id="t",
                operation="op",
                reason=DeferReason.EXPLICIT_DEFER,
                detail=str(i),
                payload={"i": i},
            )
        drained = q.drain("t", limit=3)
        assert [e.payload["i"] for e in drained] == [0, 1, 2]
        # Remaining 3,4 still in queue
        remaining = q.pending("t")
        assert [e.payload["i"] for e in remaining] == [4, 3]

    def test_drain_zero_limit_is_noop(self) -> None:
        q = SlowCallQueue()
        q.defer(
            tenant_id="t",
            operation="op",
            reason=DeferReason.EXPLICIT_DEFER,
            detail="x",
        )
        assert q.drain("t", limit=0) == []
        assert q.count("t") == 1

    def test_empty_tenant_id_rejected(self) -> None:
        q = SlowCallQueue()
        with pytest.raises(ValueError, match="tenant_id required"):
            q.defer(
                tenant_id="",
                operation="op",
                reason=DeferReason.EXPLICIT_DEFER,
                detail="x",
            )

    def test_invalid_max_per_tenant_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_per_tenant must be positive"):
            SlowCallQueue(max_per_tenant=0)

    def test_concurrent_writers_dont_lose_entries(self) -> None:
        """RLock invariant — N threads defer simultaneously and all
        N entries land in the queue."""
        q = SlowCallQueue(max_per_tenant=200)
        n = 50

        def writer(i: int) -> None:
            q.defer(
                tenant_id="t",
                operation="op",
                reason=DeferReason.TRANSIENT_ERROR,
                detail=str(i),
                payload={"i": i},
            )

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert q.count("t") == n


# ============================================================================
# with_deadline_and_defer — the canonical "timeout + queue" surface
# ============================================================================


class TestDeadlineAndDefer:
    def test_happy_path_does_not_defer(self) -> None:
        q = SlowCallQueue()
        result = with_deadline_and_defer(
            lambda: "ok",
            timeout_ms=200,
            fallback="DEGRADED",
            operation="unit.fast",
            tenant_id="t",
            defer_queue=q,
            defer_payload={"k": "v"},
        )
        assert result.degraded is False
        assert result.value == "ok"
        assert q.count("t") == 0

    def test_timeout_path_defers_with_deadline_exceeded(self) -> None:
        q = SlowCallQueue()

        def slow() -> str:
            time.sleep(0.4)
            return "should-not-see"

        result = with_deadline_and_defer(
            slow,
            timeout_ms=80,
            fallback=[],
            operation="unit.slow",
            tenant_id="bank-a",
            defer_queue=q,
            defer_payload={"customer_id": "cust_001"},
        )
        assert result.degraded is True
        assert result.value == []
        deferred = q.pending("bank-a")
        assert len(deferred) == 1
        assert deferred[0].reason is DeferReason.DEADLINE_EXCEEDED
        assert deferred[0].operation == "unit.slow"
        assert deferred[0].payload == {"customer_id": "cust_001"}
        assert deferred[0].elapsed_ms >= 80

    def test_raising_callable_defers_with_transient_error(self) -> None:
        q = SlowCallQueue()

        def boom() -> str:
            raise ConnectionError("upstream down")

        result = with_deadline_and_defer(
            boom,
            timeout_ms=200,
            fallback="DEGRADED",
            operation="unit.boom",
            tenant_id="bank-b",
            defer_queue=q,
        )
        assert result.degraded is True
        deferred = q.pending("bank-b")
        assert len(deferred) == 1
        assert deferred[0].reason is DeferReason.TRANSIENT_ERROR
        assert "ConnectionError" in deferred[0].detail
        assert "upstream down" in deferred[0].detail

    def test_defer_helper_failure_does_not_propagate(self) -> None:
        """If the queue's defer() itself raises (e.g. invalid args, full
        disk in a future durable-store implementation), the wrapper
        must still return the fallback so the request thread is safe.
        """

        class _ExplodingQueue:
            def defer(self, **_kwargs: Any) -> Any:
                raise RuntimeError("queue is broken")

        def slow() -> str:
            time.sleep(0.3)
            return "x"

        # Cast through Any since we're intentionally violating the type
        result = with_deadline_and_defer(
            slow,
            timeout_ms=50,
            fallback="DEGRADED",
            operation="unit.broken-defer",
            tenant_id="t",
            defer_queue=_ExplodingQueue(),  # type: ignore[arg-type]
        )
        assert result.degraded is True
        assert result.value == "DEGRADED"

    def test_module_singleton_default(self) -> None:
        """When no defer_queue is supplied, the wrapper uses the
        module-level singleton (same pattern as get_quarantine)."""
        from context_engine.slow_call_queue import get_defer_queue

        q = get_defer_queue()
        q.clear()
        before = q.count("singleton-tenant")
        with_deadline_and_defer(
            lambda: (_ for _ in ()).throw(ValueError("x")),
            timeout_ms=100,
            fallback="DEGRADED",
            operation="unit.singleton",
            tenant_id="singleton-tenant",
        )
        assert q.count("singleton-tenant") == before + 1
        q.clear()


# ============================================================================
# FastAPI surface — GET /deferred/pending
# ============================================================================


@pytest.fixture
def client_with_defer_queue() -> tuple[TestClient, SlowCallQueue]:
    repo = InMemoryEventRepository()
    bus = InMemoryEventBus()
    crepo = InMemoryCustomerRepository()
    q = QuarantineBuffer()
    dq = SlowCallQueue()
    app.dependency_overrides[get_repo] = lambda: repo
    app.dependency_overrides[get_bus] = lambda: bus
    app.dependency_overrides[get_customer_repo] = lambda: crepo
    app.dependency_overrides[get_quarantine] = lambda: q
    app.dependency_overrides[get_defer_queue] = lambda: dq
    try:
        yield TestClient(app), dq
    finally:
        app.dependency_overrides.clear()


class TestDeferredPendingEndpoint:
    def test_returns_records_for_tenant(
        self, client_with_defer_queue: tuple[TestClient, SlowCallQueue]
    ) -> None:
        client, dq = client_with_defer_queue
        dq.defer(
            tenant_id="bank-a",
            operation="brief_assembly.load_history",
            reason=DeferReason.DEADLINE_EXCEEDED,
            detail="seeded for test",
            payload={"customer_id": "cust_001"},
            elapsed_ms=270.0,
        )
        resp = client.get("/deferred/pending?tenant_id=bank-a")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["tenant_id"] == "bank-a"
        assert body["count"] == 1
        entry = body["entries"][0]
        assert entry["reason"] == "deadline_exceeded"
        assert entry["operation"] == "brief_assembly.load_history"
        assert entry["payload"] == {"customer_id": "cust_001"}

    def test_requires_tenant_id(
        self, client_with_defer_queue: tuple[TestClient, SlowCallQueue]
    ) -> None:
        client, _ = client_with_defer_queue
        resp = client.get("/deferred/pending")
        assert resp.status_code in (400, 422)

    def test_limit_bounds_enforced(
        self, client_with_defer_queue: tuple[TestClient, SlowCallQueue]
    ) -> None:
        client, _ = client_with_defer_queue
        resp = client.get("/deferred/pending?tenant_id=t&limit=10000")
        assert resp.status_code == 400

    def test_tenant_isolation_on_endpoint(
        self, client_with_defer_queue: tuple[TestClient, SlowCallQueue]
    ) -> None:
        client, dq = client_with_defer_queue
        dq.defer(
            tenant_id="bank-a",
            operation="op",
            reason=DeferReason.DEADLINE_EXCEEDED,
            detail="a",
        )
        dq.defer(
            tenant_id="bank-b",
            operation="op",
            reason=DeferReason.DEADLINE_EXCEEDED,
            detail="b",
        )
        a = client.get("/deferred/pending?tenant_id=bank-a").json()
        b = client.get("/deferred/pending?tenant_id=bank-b").json()
        assert a["count"] == 1 and a["entries"][0]["detail"] == "a"
        assert b["count"] == 1 and b["entries"][0]["detail"] == "b"
