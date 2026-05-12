"""End-to-end-ish test: publish on the context-engine bus → orchestrator
listener fires → planner emits proposal → `/proposals/recent` returns it.

Mirrors `test_orchestrator_events_endpoint.py` one-for-one with the Day-9
planner handler chained after the Day-8 buffered handler. Mock-mode LLM
means proposals come from the rule table — deterministic, no API key.

Confirms that the Day-9 wiring is additive over Day 8 (events buffer
still works) and that the per-tenant isolation property carries
through to the proposals surface.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import orchestrator.api as orch_api
from context_engine.event_bus import InMemoryEventBus, channel_for, envelope_for
from contracts import ChannelType, Event, EventType


@pytest.fixture
def shared_bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture
def attached_client(shared_bus: InMemoryEventBus) -> TestClient:
    """Orchestrator TestClient with the Day-9 planner+buffer handler
    chain bound to the shared bus."""
    orch_api._reset_listener_for_tests()
    orch_api.attach_in_memory_bus(shared_bus)
    client = TestClient(orch_api.app)
    yield client
    orch_api._reset_listener_for_tests()


def _emit(
    bus: InMemoryEventBus,
    *,
    tenant_id: str,
    event_id: str,
    event_type: EventType = EventType.MESSAGE_RECEIVED,
) -> None:
    event = Event(
        id=event_id,
        tenant_id=tenant_id,
        customer_id="cust_t9_endpoint",
        channel_code=ChannelType.EMAIL,
        event_type=event_type,
        idempotency_key=f"idem-{event_id}",
        payload={"subject": "hi"},
        received_at=datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc),
    )
    bus.publish(channel_for(tenant_id), envelope_for(event))


def test_proposal_emitted_for_published_event(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    """Day-8 + Day-9 chain: one publish produces one buffered envelope
    AND one proposal."""
    _emit(shared_bus, tenant_id="acme", event_id="evt_t9_e2e_001")

    # Day-8 surface still works.
    events = attached_client.get(
        "/events/recent", params={"tenant_id": "acme"}
    ).json()
    assert events["count"] == 1
    assert events["events"][0]["event_id"] == "evt_t9_e2e_001"

    # Day-9 surface fires.
    body = attached_client.get(
        "/proposals/recent", params={"tenant_id": "acme"}
    ).json()
    assert body["count"] == 1
    p = body["proposals"][0]
    assert p["event_id"] == "evt_t9_e2e_001"
    assert p["tenant_id"] == "acme"
    # Mock-mode → fallback rule table for message_received.
    assert p["proposed_by"] == "fallback"
    assert p["action_type"] == "send_borrower_message"


def test_proposals_recent_isolates_tenants(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    _emit(shared_bus, tenant_id="tenantA", event_id="evt_pa")
    _emit(shared_bus, tenant_id="tenantB", event_id="evt_pb")

    a = attached_client.get(
        "/proposals/recent", params={"tenant_id": "tenantA"}
    ).json()
    b = attached_client.get(
        "/proposals/recent", params={"tenant_id": "tenantB"}
    ).json()
    assert [p["event_id"] for p in a["proposals"]] == ["evt_pa"]
    assert [p["event_id"] for p in b["proposals"]] == ["evt_pb"]


def test_proposals_recent_requires_tenant_id(attached_client: TestClient) -> None:
    """Multi-tenant invariant — same as /events/recent."""
    r = attached_client.get("/proposals/recent")
    assert r.status_code == 422


def test_proposals_recent_returns_empty_when_unattached() -> None:
    orch_api._reset_listener_for_tests()
    client = TestClient(orch_api.app)
    r = client.get("/proposals/recent", params={"tenant_id": "acme"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["proposals"] == []


def test_proposals_recent_limit_clamps(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    for i in range(10):
        _emit(shared_bus, tenant_id="acme", event_id=f"evt_p_{i:02d}")
    body = attached_client.get(
        "/proposals/recent", params={"tenant_id": "acme", "limit": 3}
    ).json()
    assert body["count"] == 3
    assert [p["event_id"] for p in body["proposals"]] == [
        "evt_p_09",
        "evt_p_08",
        "evt_p_07",
    ]


def test_proposal_action_type_routes_by_event_type(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    """Different event_types must produce different fallback action_types
    via the rule table."""
    _emit(
        shared_bus,
        tenant_id="acme",
        event_id="evt_anom",
        event_type=EventType.ANOMALY_DETECTED,
    )
    _emit(
        shared_bus,
        tenant_id="acme",
        event_id="evt_msg",
        event_type=EventType.MESSAGE_RECEIVED,
    )

    body = attached_client.get(
        "/proposals/recent", params={"tenant_id": "acme"}
    ).json()
    by_event = {p["event_id"]: p["action_type"] for p in body["proposals"]}
    assert by_event["evt_anom"] == "notify_loan_officer"
    assert by_event["evt_msg"] == "send_borrower_message"
