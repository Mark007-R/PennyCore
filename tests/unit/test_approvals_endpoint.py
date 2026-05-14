"""HTTP-layer tests for the Day-10 `/approvals` + `/actions/{id}` surface.

The pipeline lives at module-scope on `orchestrator.api`; this test
exercises the live endpoints over `TestClient` rather than mocking the
pipeline. The `_reset_listener_for_tests` helper resets the buffer +
pipeline between scenarios so each test sees a clean slate.

Coverage:
  1. GET /approvals returns an empty list for unconfigured tenants.
  2. Publishing an event with `approval_required` policy enqueues a row
     visible via /approvals.
  3. POST /approvals/{id}/approve transitions to executed and returns
     the audit-trail-bearing action view.
  4. POST /approvals/{id}/reject transitions to rejected.
  5. Double-approve returns 409.
  6. Unknown action_id returns 404.
  7. /actions/{id} returns 404 for unknown actions.
  8. tenant_id query is mandatory on /approvals.
  9. /approvals only lists the queried tenant's rows.
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
def attached_client(shared_bus: InMemoryEventBus):
    """Reset pipeline + buffers + listener, attach a fresh bus."""
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
        customer_id="cust_d10",
        channel_code=ChannelType.EMAIL,
        event_type=event_type,
        idempotency_key=f"idem-{event_id}",
        payload={"subject": "hi"},
        received_at=datetime(2026, 5, 13, 12, 0, 0, tzinfo=timezone.utc),
    )
    bus.publish(channel_for(tenant_id), envelope_for(event))


def _configure_policy(tenant_id: str, decision: str = "approval_required") -> None:
    pipeline = orch_api._get_pipeline_for_tests()
    pipeline.policy.set_policies(tenant_id, {"*": decision})


def test_approvals_empty_for_unconfigured_tenant(attached_client: TestClient) -> None:
    body = attached_client.get(
        "/approvals", params={"tenant_id": "nobody"}
    ).json()
    assert body == {"tenant_id": "nobody", "count": 0, "approvals": []}


def test_publishing_event_creates_approval_row(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    _configure_policy("acme", "approval_required")
    _emit(shared_bus, tenant_id="acme", event_id="evt_d10_001")
    body = attached_client.get(
        "/approvals", params={"tenant_id": "acme"}
    ).json()
    assert body["count"] == 1
    a = body["approvals"][0]
    assert a["status"] == "pending_approval"
    assert a["event_id"] == "evt_d10_001"
    assert a["action_type"] == "send_borrower_message"
    assert a["reasoning"]  # planner reasoning surfaced
    # Audit trail has at least PROPOSAL + DECISION rows.
    kinds = [e["kind"] for e in a["audit_trail"]]
    assert "proposal" in kinds
    assert "decision" in kinds


def test_approve_transitions_to_executed(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    _configure_policy("acme", "approval_required")
    _emit(shared_bus, tenant_id="acme", event_id="evt_d10_002")
    pending = attached_client.get(
        "/approvals", params={"tenant_id": "acme"}
    ).json()["approvals"]
    action_id = pending[0]["action_id"]

    r = attached_client.post(
        f"/approvals/{action_id}/approve", params={"decided_by": "tester"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "executed"
    assert body["executed_at"] is not None
    assert body["executed_payload"] is not None
    audit_kinds = [e["kind"] for e in body["audit_trail"]]
    assert "approval" in audit_kinds
    assert "execution" in audit_kinds

    # No longer pending.
    after = attached_client.get(
        "/approvals", params={"tenant_id": "acme"}
    ).json()
    assert after["count"] == 0


def test_reject_transitions_to_rejected(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    _configure_policy("acme", "approval_required")
    _emit(shared_bus, tenant_id="acme", event_id="evt_d10_003")
    pending = attached_client.get(
        "/approvals", params={"tenant_id": "acme"}
    ).json()["approvals"]
    action_id = pending[0]["action_id"]

    r = attached_client.post(
        f"/approvals/{action_id}/reject",
        params={"reason": "test rejection", "decided_by": "tester"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rejected"
    audit_kinds = [e["kind"] for e in body["audit_trail"]]
    assert "rejection" in audit_kinds


def test_double_approve_returns_conflict(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    _configure_policy("acme", "approval_required")
    _emit(shared_bus, tenant_id="acme", event_id="evt_d10_004")
    pending = attached_client.get(
        "/approvals", params={"tenant_id": "acme"}
    ).json()["approvals"]
    action_id = pending[0]["action_id"]

    first = attached_client.post(f"/approvals/{action_id}/approve")
    assert first.status_code == 200
    second = attached_client.post(f"/approvals/{action_id}/approve")
    assert second.status_code == 409


def test_unknown_action_id_returns_404(attached_client: TestClient) -> None:
    r = attached_client.post("/approvals/act_does_not_exist/approve")
    assert r.status_code == 404
    r2 = attached_client.post("/approvals/act_does_not_exist/reject")
    assert r2.status_code == 404
    r3 = attached_client.get("/actions/act_does_not_exist")
    assert r3.status_code == 404


def test_approvals_requires_tenant_id(attached_client: TestClient) -> None:
    r = attached_client.get("/approvals")
    assert r.status_code == 422


def test_approvals_isolates_tenants(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    _configure_policy("alpha", "approval_required")
    _configure_policy("beta", "approval_required")
    _emit(shared_bus, tenant_id="alpha", event_id="evt_alpha")
    _emit(shared_bus, tenant_id="beta", event_id="evt_beta")

    alpha = attached_client.get(
        "/approvals", params={"tenant_id": "alpha"}
    ).json()
    beta = attached_client.get(
        "/approvals", params={"tenant_id": "beta"}
    ).json()

    assert {a["event_id"] for a in alpha["approvals"]} == {"evt_alpha"}
    assert {a["event_id"] for a in beta["approvals"]} == {"evt_beta"}

    # Approve alpha — beta queue unaffected.
    alpha_id = alpha["approvals"][0]["action_id"]
    attached_client.post(f"/approvals/{alpha_id}/approve")
    beta_after = attached_client.get(
        "/approvals", params={"tenant_id": "beta"}
    ).json()
    assert beta_after["count"] == 1


def test_actions_endpoint_returns_action_with_audit(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    _configure_policy("acme", "approval_required")
    _emit(shared_bus, tenant_id="acme", event_id="evt_d10_005")
    pending = attached_client.get(
        "/approvals", params={"tenant_id": "acme"}
    ).json()["approvals"]
    action_id = pending[0]["action_id"]

    r = attached_client.get(f"/actions/{action_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["action_id"] == action_id
    assert body["status"] == "pending_approval"
    assert len(body["audit_trail"]) >= 2  # proposal + decision


def test_auto_policy_executes_without_queue(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    _configure_policy("acme", "auto")
    _emit(shared_bus, tenant_id="acme", event_id="evt_d10_006")
    pending = attached_client.get(
        "/approvals", params={"tenant_id": "acme"}
    ).json()
    # Auto path skips the queue entirely.
    assert pending["count"] == 0
