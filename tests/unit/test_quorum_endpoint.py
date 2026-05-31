"""HTTP-layer tests for the Day-26 N-of-M approval surface.

Exercises the live `/approvals` endpoints over `TestClient` with a quorum
configured on the module-scope pipeline, covering the new `approval_progress`
view block and the 403 (ineligible) / 409 (duplicate) error mappings.

Coverage:
  1. approval_progress is surfaced on the pending-list view.
  2. A partial approval returns 200 + keeps the action pending.
  3. The quorum-completing approval executes the action.
  4. An ineligible approver gets 403; the action stays pending.
  5. A duplicate approver gets 409.
  6. A single-approver action still exposes approval_progress (required=1).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import orchestrator.api as orch_api
from context_engine.event_bus import InMemoryEventBus, channel_for, envelope_for
from contracts import ChannelType, Event, EventType
from contracts.actions import ActionType


@pytest.fixture
def shared_bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture
def attached_client(shared_bus: InMemoryEventBus):
    orch_api._reset_listener_for_tests()
    orch_api.attach_in_memory_bus(shared_bus)
    client = TestClient(orch_api.app)
    yield client
    orch_api._reset_listener_for_tests()


def _emit(bus: InMemoryEventBus, *, tenant_id: str, event_id: str) -> None:
    event = Event(
        id=event_id,
        tenant_id=tenant_id,
        customer_id="cust_q",
        channel_code=ChannelType.EMAIL,
        event_type=EventType.MESSAGE_RECEIVED,
        idempotency_key=f"idem-{event_id}",
        payload={"subject": "review me"},
        received_at=datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc),
    )
    bus.publish(channel_for(tenant_id), envelope_for(event))


def _arm(tenant_id: str, *, required_approvals: int, eligible=None) -> None:
    pipeline = orch_api._get_pipeline_for_tests()
    pipeline.policy.set_policies(tenant_id, {"*": "approval_required"})
    pipeline.quorum.set_rule(
        tenant_id,
        ActionType.SEND_BORROWER_MESSAGE,
        required_approvals=required_approvals,
        eligible_approvers=eligible,
    )


def _first_action_id(client: TestClient, tenant_id: str) -> str:
    pending = client.get("/approvals", params={"tenant_id": tenant_id}).json()
    return pending["approvals"][0]["action_id"]


def test_approval_progress_surfaced(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    _arm("acme", required_approvals=2, eligible=["alice", "bob", "carol"])
    _emit(shared_bus, tenant_id="acme", event_id="evt_q1")
    pending = attached_client.get(
        "/approvals", params={"tenant_id": "acme"}
    ).json()["approvals"][0]
    prog = pending["approval_progress"]
    assert prog["required_approvals"] == 2
    assert prog["approvals_remaining"] == 2
    assert prog["approvers"] == []
    assert prog["eligible_approvers"] == ["alice", "bob", "carol"]


def test_partial_then_quorum_completes(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    _arm("acme", required_approvals=2, eligible=["alice", "bob"])
    _emit(shared_bus, tenant_id="acme", event_id="evt_q2")
    action_id = _first_action_id(attached_client, "acme")

    first = attached_client.post(
        f"/approvals/{action_id}/approve", params={"decided_by": "alice"}
    )
    assert first.status_code == 200
    body = first.json()
    assert body["status"] == "pending_approval"
    assert body["approval_progress"]["approvals_recorded"] == 1
    assert body["approval_progress"]["approvers"] == ["alice"]

    second = attached_client.post(
        f"/approvals/{action_id}/approve", params={"decided_by": "bob"}
    )
    assert second.status_code == 200
    assert second.json()["status"] == "executed"


def test_ineligible_approver_403(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    _arm("acme", required_approvals=2, eligible=["alice", "bob"])
    _emit(shared_bus, tenant_id="acme", event_id="evt_q3")
    action_id = _first_action_id(attached_client, "acme")

    r = attached_client.post(
        f"/approvals/{action_id}/approve", params={"decided_by": "mallory"}
    )
    assert r.status_code == 403
    # Still pending after the refused vote.
    after = attached_client.get(
        "/approvals", params={"tenant_id": "acme"}
    ).json()
    assert after["count"] == 1


def test_duplicate_approver_409(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    _arm("acme", required_approvals=2, eligible=["alice", "bob"])
    _emit(shared_bus, tenant_id="acme", event_id="evt_q4")
    action_id = _first_action_id(attached_client, "acme")

    attached_client.post(
        f"/approvals/{action_id}/approve", params={"decided_by": "alice"}
    )
    dup = attached_client.post(
        f"/approvals/{action_id}/approve", params={"decided_by": "alice"}
    )
    assert dup.status_code == 409


def test_single_approver_still_has_progress_block(
    shared_bus: InMemoryEventBus, attached_client: TestClient
) -> None:
    _arm("acme", required_approvals=1)
    _emit(shared_bus, tenant_id="acme", event_id="evt_q5")
    pending = attached_client.get(
        "/approvals", params={"tenant_id": "acme"}
    ).json()["approvals"][0]
    assert pending["approval_progress"]["required_approvals"] == 1
