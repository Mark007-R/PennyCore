"""End-to-end-ish test: publish on the context-engine bus → orchestrator
listener fires → `/events/recent` returns the envelope.

This is the smallest possible Day-8 integration check. The Day-11 wrap
test will exercise the full POST /events → ingest → publish → orchestrator
handler chain through both FastAPI apps.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from context_engine.event_bus import InMemoryEventBus, channel_for, envelope_for
from contracts import ChannelType, Event, EventType
import orchestrator.api as orch_api


@pytest.fixture
def shared_bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture
def attached_client(shared_bus: InMemoryEventBus) -> TestClient:
    """Orchestrator TestClient with an in-memory listener bound to the
    shared bus. Cleans up between tests so buffer state doesn't leak."""
    orch_api._reset_listener_for_tests()
    orch_api.attach_in_memory_bus(shared_bus)
    client = TestClient(orch_api.app)
    yield client
    orch_api._reset_listener_for_tests()


def _emit(bus: InMemoryEventBus, *, tenant_id: str, event_id: str) -> None:
    event = Event(
        id=event_id,
        tenant_id=tenant_id,
        customer_id="cust_t8_endpoint",
        channel_code=ChannelType.EMAIL,
        event_type=EventType.MESSAGE_RECEIVED,
        idempotency_key=f"idem-{event_id}",
        payload={"subject": "hi"},
        received_at=datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc),
    )
    bus.publish(channel_for(tenant_id), envelope_for(event))


def test_recent_returns_published_event(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    _emit(shared_bus, tenant_id="acme", event_id="evt_t8_e2e_001")

    r = attached_client.get("/events/recent", params={"tenant_id": "acme"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["events"][0]["event_id"] == "evt_t8_e2e_001"
    assert body["listener_mode"] == "in-memory"


def test_recent_isolates_tenants(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    _emit(shared_bus, tenant_id="tenantA", event_id="evt_a")
    _emit(shared_bus, tenant_id="tenantB", event_id="evt_b")

    a = attached_client.get("/events/recent", params={"tenant_id": "tenantA"}).json()
    b = attached_client.get("/events/recent", params={"tenant_id": "tenantB"}).json()
    assert [e["event_id"] for e in a["events"]] == ["evt_a"]
    assert [e["event_id"] for e in b["events"]] == ["evt_b"]


def test_recent_requires_tenant_id(attached_client: TestClient) -> None:
    """Multi-tenant invariant: there is no implicit "all tenants" view."""
    r = attached_client.get("/events/recent")
    assert r.status_code == 422


def test_recent_returns_empty_when_no_listener_attached() -> None:
    """No bus attached → endpoint returns an empty page, not 503."""
    orch_api._reset_listener_for_tests()
    client = TestClient(orch_api.app)
    r = client.get("/events/recent", params={"tenant_id": "acme"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["events"] == []
    assert body["listener_mode"] == "unattached"


def test_root_reports_listener_mode_after_attach(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    body = attached_client.get("/").json()
    assert body["listener_mode"] == "in-memory"


def test_recent_limit_clamps(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    for i in range(10):
        _emit(shared_bus, tenant_id="acme", event_id=f"evt_{i:02d}")
    body = attached_client.get(
        "/events/recent", params={"tenant_id": "acme", "limit": 3}
    ).json()
    assert body["count"] == 3
    # Most recent first.
    assert [e["event_id"] for e in body["events"]] == ["evt_09", "evt_08", "evt_07"]
