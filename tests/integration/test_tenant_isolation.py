"""Multi-tenant isolation hardening tests (Day 20, Phase 4).

PennyCore's multi-tenant invariant (rule 15, SYSTEM_DESIGN §3.3):
**no API path, query, queue read, or audit lookup may return one
tenant's data to a caller acting as a different tenant** — even when
the caller knows an opaque ID (event_id, customer_id, action_id) that
exists under the other tenant.

The 15 tests group by surface:

  A. EventRepository                  tests 1-3
  B. CustomerRepository + linker      tests 4-6
  C. InMemoryActionStore              test  7
  D. ApprovalQueue                    tests 8-9
  E. AuditLog                         tests 10-11
  F. HTTP surface (orchestrator)      tests 12-15

Tests are deterministic — no real LLM calls, no Postgres, no Redis.
The HTTP tests use FastAPI's `TestClient` against the live
`orchestrator.api.app` with `_reset_listener_for_tests` between
scenarios to keep cross-test state clean. They exercise the Day-20
`tenant_id` query parameter added to `/actions/{id}` and
`/approvals/{id}/{approve,reject}` — supplying a foreign tenant_id
when looking up another tenant's action must yield a plain 404 with
no leak that the action exists elsewhere.

Day 19 covered idempotency; Day 21 will cover concurrent races. This
file is the tenant-isolation pillar of Phase 4 hardening.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import orchestrator.api as orch_api
from context_engine.customer_repository import InMemoryCustomerRepository
from context_engine.event_bus import InMemoryEventBus, channel_for, envelope_for
from context_engine.ingestion import (
    IngestionRequest,
    build_event_from_request,
)
from context_engine.linking import resolve_customer
from context_engine.repository import InMemoryEventRepository
from contracts import ChannelType, Event, EventType, IdentityKind
from contracts.actions import (
    Action,
    ActionProposal,
    ActionStatus,
    ActionType,
    ProposedBy,
)
from contracts.audit import AuditActorKind, AuditKind, AuditLogEntry
from orchestrator.approval_queue import InMemoryApprovalQueue
from orchestrator.audit import InMemoryAuditLog
from orchestrator.decision_pipeline import (
    ActionNotFoundError,
    DecisionPipeline,
    InMemoryActionStore,
    make_default_pipeline,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    tenant_id: str,
    idempotency_key: str,
    customer_id: str | None = None,
) -> Event:
    request = IngestionRequest(
        tenant_id=tenant_id,
        channel_code="sms",
        event_type="message_received",
        idempotency_key=idempotency_key,
        payload={"text": "hello"},
    )
    return build_event_from_request(
        request, idempotency_key=idempotency_key, customer_id=customer_id
    )


def _make_action(
    *,
    tenant_id: str,
    action_id: str,
    status: ActionStatus = ActionStatus.PENDING_APPROVAL,
) -> Action:
    return Action(
        id=action_id,
        tenant_id=tenant_id,
        proposal_id=f"prop_{action_id}",
        event_id=f"evt_{action_id}",
        customer_id=None,
        action_type=ActionType.SEND_BORROWER_MESSAGE,
        status=status,
        payload={},
    )


def _make_audit_entry(
    *, tenant_id: str, action_id: str, event_id: str, kind: AuditKind
) -> AuditLogEntry:
    return AuditLogEntry(
        tenant_id=tenant_id,
        kind=kind,
        action_id=action_id,
        caused_by_event_id=event_id,
        actor_kind=AuditActorKind.SYSTEM,
        payload={},
    )


def _proposal(
    *, tenant_id: str, event_id: str, action_type: ActionType = ActionType.SEND_BORROWER_MESSAGE
) -> ActionProposal:
    return ActionProposal(
        id=f"prop_{event_id}",
        tenant_id=tenant_id,
        event_id=event_id,
        customer_id=None,
        action_type=action_type,
        proposed_by=ProposedBy.LLM,
        payload={"_planner_reasoning": "tenant-isolation test"},
    )


# ===========================================================================
# A. EventRepository isolation (3)
# ===========================================================================


class TestEventRepositoryIsolation:
    """`(tenant_id, ...)` is the access key for every event lookup.
    Cross-tenant probing returns None / 0 even when the inner id is
    correct."""

    def test_01_get_event_by_id_rejects_cross_tenant_lookup(self) -> None:
        # Bank A writes an event. Bank B knows its event_id and probes.
        # The id is correct; the tenant_id is wrong; the lookup must miss.
        repo = InMemoryEventRepository()
        event_a = _make_event(tenant_id="bank-a", idempotency_key="iso-1")
        repo.upsert_event(event_a)

        assert repo.get_event_by_id(tenant_id="bank-a", event_id=event_a.id) == event_a
        assert (
            repo.get_event_by_id(tenant_id="bank-b", event_id=event_a.id) is None
        ), "cross-tenant get_event_by_id must return None"

    def test_02_get_event_by_idempotency_is_tenant_scoped(self) -> None:
        # Two tenants pick the same idempotency_key — the repository
        # MUST treat them as two distinct events, and a lookup under
        # one tenant_id can never see the other tenant's row.
        repo = InMemoryEventRepository()
        event_a = _make_event(tenant_id="bank-a", idempotency_key="shared-key")
        event_b = _make_event(tenant_id="bank-b", idempotency_key="shared-key")
        repo.upsert_event(event_a)
        repo.upsert_event(event_b)
        assert event_a.id != event_b.id

        a_row = repo.get_event_by_idempotency(
            tenant_id="bank-a", idempotency_key="shared-key"
        )
        b_row = repo.get_event_by_idempotency(
            tenant_id="bank-b", idempotency_key="shared-key"
        )
        assert a_row is not None and a_row.id == event_a.id
        assert b_row is not None and b_row.id == event_b.id
        assert a_row.id != b_row.id

    def test_03_count_filters_by_tenant(self) -> None:
        # The aggregate read path also honors tenant scoping when
        # asked. The unfiltered count is a metrics-only surface (it
        # spans tenants by design) and must not be exposed through
        # any per-tenant HTTP path — but the option to scope must
        # work for the dashboards that DO want per-tenant numbers.
        repo = InMemoryEventRepository()
        repo.upsert_event(_make_event(tenant_id="bank-a", idempotency_key="a1"))
        repo.upsert_event(_make_event(tenant_id="bank-a", idempotency_key="a2"))
        repo.upsert_event(_make_event(tenant_id="bank-b", idempotency_key="b1"))

        assert repo.count() == 3
        assert repo.count(tenant_id="bank-a") == 2
        assert repo.count(tenant_id="bank-b") == 1
        assert repo.count(tenant_id="bank-c") == 0


# ===========================================================================
# B. CustomerRepository + linker isolation (3)
# ===========================================================================


class TestCustomerRepositoryIsolation:
    """Same identity values can exist under multiple tenants —
    `(tenant_id, kind, value)` is the only UNIQUE oracle. A linker run
    under tenant B must never reach tenant A's customer rows."""

    def test_04_find_customer_by_identity_does_not_cross_tenants(self) -> None:
        repo = InMemoryCustomerRepository()
        # Same email under two tenants — must materialize two distinct
        # customers (no cross-tenant deduping is the right behavior).
        cust_a = repo.create_customer(
            tenant_id="bank-a",
            identities=[(IdentityKind.EMAIL, "jane@example.com")],
            display_name="Jane (A)",
        )
        cust_b = repo.create_customer(
            tenant_id="bank-b",
            identities=[(IdentityKind.EMAIL, "jane@example.com")],
            display_name="Jane (B)",
        )
        assert cust_a.id != cust_b.id

        # Lookup under bank-a returns A's customer.
        found_a = repo.find_customer_by_identity(
            tenant_id="bank-a",
            identity_kind=IdentityKind.EMAIL,
            identity_value="jane@example.com",
        )
        assert found_a is not None and found_a.id == cust_a.id

        # Lookup under bank-b returns B's customer.
        found_b = repo.find_customer_by_identity(
            tenant_id="bank-b",
            identity_kind=IdentityKind.EMAIL,
            identity_value="jane@example.com",
        )
        assert found_b is not None and found_b.id == cust_b.id

        # A third, unknown tenant gets nothing — even though the
        # identity exists in TWO other tenants.
        assert (
            repo.find_customer_by_identity(
                tenant_id="bank-c",
                identity_kind=IdentityKind.EMAIL,
                identity_value="jane@example.com",
            )
            is None
        )

    def test_05_get_customer_does_not_cross_tenants(self) -> None:
        repo = InMemoryCustomerRepository()
        cust_a = repo.create_customer(
            tenant_id="bank-a",
            identities=[(IdentityKind.EXTERNAL_ID, "ext-1")],
        )

        # Correct tenant → hit.
        assert (
            repo.get_customer(tenant_id="bank-a", customer_id=cust_a.id) == cust_a
        )
        # Different tenant + valid customer_id → miss. The id is known
        # to the attacker but the row is invisible.
        assert (
            repo.get_customer(tenant_id="bank-b", customer_id=cust_a.id) is None
        )

    def test_06_linker_does_not_link_across_tenants(self) -> None:
        # Bank A's customer is registered under jane@example.com.
        # An identical event arrives under bank-b. The linker MUST
        # create a NEW customer for bank-b — never reuse bank-a's.
        repo = InMemoryCustomerRepository()
        cust_a = repo.create_customer(
            tenant_id="bank-a",
            identities=[(IdentityKind.EMAIL, "jane@example.com")],
        )

        request_b = IngestionRequest(
            tenant_id="bank-b",
            channel_code="email",
            event_type="message_received",
            idempotency_key="bank-b-msg-1",
            payload={"from_email": "jane@example.com"},
        )
        outcome = resolve_customer(request_b, customer_repo=repo)
        assert outcome.customer_created is True
        assert outcome.customer_id != cust_a.id

        # Bank A's identity index untouched.
        found_a = repo.find_customer_by_identity(
            tenant_id="bank-a",
            identity_kind=IdentityKind.EMAIL,
            identity_value="jane@example.com",
        )
        assert found_a is not None and found_a.id == cust_a.id


# ===========================================================================
# C. ActionStore isolation (1)
# ===========================================================================


class TestActionStoreIsolation:
    """The action store has a global id index (action_ids are UUIDs;
    no tenant prefix is needed). The tenant-scoped LIST path is the
    only one read by the HTTP /approvals + /events surfaces — it must
    never leak across."""

    def test_07_list_for_tenant_returns_only_own_tenant_actions(self) -> None:
        store = InMemoryActionStore()
        a1 = _make_action(tenant_id="bank-a", action_id="act_a_1")
        a2 = _make_action(tenant_id="bank-a", action_id="act_a_2")
        b1 = _make_action(tenant_id="bank-b", action_id="act_b_1")
        store.put(a1)
        store.put(a2)
        store.put(b1)

        a_ids = {a.id for a in store.list_for_tenant("bank-a")}
        b_ids = {a.id for a in store.list_for_tenant("bank-b")}
        assert a_ids == {"act_a_1", "act_a_2"}
        assert b_ids == {"act_b_1"}
        assert store.list_for_tenant("bank-c") == []


# ===========================================================================
# D. ApprovalQueue isolation (2)
# ===========================================================================


class TestApprovalQueueIsolation:
    """`list_pending(tenant_id)` walks the per-tenant index, never the
    global `_by_id` map. A bug that swaps the two would be caught by
    these tests."""

    def test_08_list_pending_excludes_other_tenants(self) -> None:
        queue = InMemoryApprovalQueue()
        queue.enqueue("act_a_1", tenant_id="bank-a")
        queue.enqueue("act_a_2", tenant_id="bank-a")
        queue.enqueue("act_b_1", tenant_id="bank-b")

        a_ids = {r.action_id for r in queue.list_pending("bank-a")}
        b_ids = {r.action_id for r in queue.list_pending("bank-b")}
        assert a_ids == {"act_a_1", "act_a_2"}
        assert b_ids == {"act_b_1"}
        assert queue.list_pending("bank-c") == []

    def test_09_approve_resolves_only_own_tenant_pending_list(self) -> None:
        # Bank B resolving its row must not drain bank-a's pending list,
        # and vice versa. This is the regression guard for a bug where
        # `_resolve` removed the action_id from the WRONG tenant's
        # pending list (would happen if the code looked up the rule but
        # used the calling tenant's pending list instead of the rule's).
        queue = InMemoryApprovalQueue()
        queue.enqueue("act_a_1", tenant_id="bank-a")
        queue.enqueue("act_b_1", tenant_id="bank-b")

        queue.approve("act_b_1", decided_by="reviewer-b")

        # bank-a's queue untouched.
        a_pending = [r.action_id for r in queue.list_pending("bank-a")]
        assert a_pending == ["act_a_1"]
        # bank-b's queue is now empty.
        assert queue.list_pending("bank-b") == []


# ===========================================================================
# E. AuditLog isolation (2)
# ===========================================================================


class TestAuditLogIsolation:
    """`entries_for_tenant` is the tenant-scoped read; the new
    `tenant_id=` filter on `entries_for_action` / `entries_for_event`
    is the defense-in-depth check for surfaces that look up by a
    globally-unique action_id or event_id (Day 20)."""

    def test_10_entries_for_tenant_does_not_leak_across(self) -> None:
        log = InMemoryAuditLog()
        log.write(
            _make_audit_entry(
                tenant_id="bank-a",
                action_id="act_a",
                event_id="evt_a",
                kind=AuditKind.PROPOSAL,
            )
        )
        log.write(
            _make_audit_entry(
                tenant_id="bank-b",
                action_id="act_b",
                event_id="evt_b",
                kind=AuditKind.PROPOSAL,
            )
        )

        a_rows = log.entries_for_tenant("bank-a")
        b_rows = log.entries_for_tenant("bank-b")
        assert {e.action_id for e in a_rows} == {"act_a"}
        assert {e.action_id for e in b_rows} == {"act_b"}
        assert log.entries_for_tenant("bank-c") == []

    def test_11_entries_for_action_tenant_filter_blocks_cross_tenant(
        self,
    ) -> None:
        # If two tenants ever produced audit rows for the same
        # action_id (shouldn't happen by design — action_ids are
        # UUIDs — but the filter is a defense-in-depth guard), the
        # `tenant_id=` filter must keep them apart. We synthesize the
        # collision by writing entries with the same `action_id` and
        # different `tenant_id` directly to the log.
        log = InMemoryAuditLog()
        log.write(
            _make_audit_entry(
                tenant_id="bank-a",
                action_id="act_shared",
                event_id="evt_a",
                kind=AuditKind.PROPOSAL,
            )
        )
        log.write(
            _make_audit_entry(
                tenant_id="bank-b",
                action_id="act_shared",
                event_id="evt_b",
                kind=AuditKind.PROPOSAL,
            )
        )

        # Unfiltered read returns both — that's the legacy global view
        # the takehome adapter relies on (single-tenant per scenario).
        all_rows = log.entries_for_action("act_shared")
        assert len(all_rows) == 2
        assert {r.tenant_id for r in all_rows} == {"bank-a", "bank-b"}

        # Filtered reads scope correctly.
        a_only = log.entries_for_action("act_shared", tenant_id="bank-a")
        b_only = log.entries_for_action("act_shared", tenant_id="bank-b")
        assert len(a_only) == 1 and a_only[0].tenant_id == "bank-a"
        assert len(b_only) == 1 and b_only[0].tenant_id == "bank-b"
        # And the same filter on `entries_for_event` works as the
        # symmetric guard the audit-by-event read path uses.
        assert (
            log.entries_for_event("evt_a", tenant_id="bank-b") == []
        ), "tenant_id filter on entries_for_event must reject cross-tenant"
        a_evt = log.entries_for_event("evt_a", tenant_id="bank-a")
        assert len(a_evt) == 1 and a_evt[0].caused_by_event_id == "evt_a"


# ===========================================================================
# F. HTTP-surface isolation (4)
# ===========================================================================


@pytest.fixture
def shared_bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture
def attached_client(shared_bus: InMemoryEventBus) -> Iterator[TestClient]:
    """Fresh pipeline + listener attached to a clean bus. Mirrors
    `tests/unit/test_approvals_endpoint.py`'s fixture."""
    orch_api._reset_listener_for_tests()
    orch_api.attach_in_memory_bus(shared_bus)
    yield TestClient(orch_api.app)
    orch_api._reset_listener_for_tests()


def _emit(
    bus: InMemoryEventBus,
    *,
    tenant_id: str,
    event_id: str,
) -> None:
    event = Event(
        id=event_id,
        tenant_id=tenant_id,
        customer_id=None,
        channel_code=ChannelType.EMAIL,
        event_type=EventType.MESSAGE_RECEIVED,
        idempotency_key=f"idem-{tenant_id}-{event_id}",
        payload={"subject": "hi"},
        received_at=datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc),
    )
    bus.publish(channel_for(tenant_id), envelope_for(event))


def _configure_approval_policy(tenant_id: str) -> None:
    pipeline = orch_api._get_pipeline_for_tests()
    pipeline.policy.set_policies(tenant_id, {"*": "approval_required"})


class TestHTTPSurfaceIsolation:
    """The Day-20 `tenant_id` query parameter on `/actions/{id}` and
    `/approvals/{id}/{approve,reject}` blocks cross-tenant operations
    on a known action_id. Without the parameter, the legacy
    single-tenant behavior is preserved (the takehome adapter relies
    on this — it instantiates one pipeline per scenario and is
    single-tenant by construction)."""

    def test_12_actions_get_with_foreign_tenant_id_returns_404(
        self,
        shared_bus: InMemoryEventBus,
        attached_client: TestClient,
    ) -> None:
        _configure_approval_policy("bank-a")
        _emit(shared_bus, tenant_id="bank-a", event_id="evt_iso_12")
        pending = attached_client.get(
            "/approvals", params={"tenant_id": "bank-a"}
        ).json()["approvals"]
        action_id = pending[0]["action_id"]

        # bank-b knows the action_id (or guesses it). Even so, the
        # tenant-scoped GET returns 404 — no information leaks about
        # the action's existence under bank-a.
        r = attached_client.get(
            f"/actions/{action_id}", params={"tenant_id": "bank-b"}
        )
        assert r.status_code == 404

        # The right tenant still works.
        r_ok = attached_client.get(
            f"/actions/{action_id}", params={"tenant_id": "bank-a"}
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["tenant_id"] == "bank-a"

        # And the legacy un-scoped call still works (back-compat).
        r_legacy = attached_client.get(f"/actions/{action_id}")
        assert r_legacy.status_code == 200

    def test_13_approve_with_foreign_tenant_id_does_not_mutate(
        self,
        shared_bus: InMemoryEventBus,
        attached_client: TestClient,
    ) -> None:
        _configure_approval_policy("bank-a")
        _emit(shared_bus, tenant_id="bank-a", event_id="evt_iso_13")
        pending = attached_client.get(
            "/approvals", params={"tenant_id": "bank-a"}
        ).json()["approvals"]
        action_id = pending[0]["action_id"]

        # bank-b attempts to approve. Must 404 AND the underlying
        # action must remain `pending_approval` — proves we returned
        # before mutating any state.
        r = attached_client.post(
            f"/approvals/{action_id}/approve",
            params={"tenant_id": "bank-b", "decided_by": "attacker"},
        )
        assert r.status_code == 404

        still_pending = attached_client.get(
            "/approvals", params={"tenant_id": "bank-a"}
        ).json()
        assert still_pending["count"] == 1
        assert still_pending["approvals"][0]["action_id"] == action_id
        assert still_pending["approvals"][0]["status"] == "pending_approval"

        # Audit trail does NOT contain an approval row from the
        # rejected attempt — the cross-tenant guard fires BEFORE the
        # audit write.
        view = attached_client.get(
            f"/actions/{action_id}", params={"tenant_id": "bank-a"}
        ).json()
        kinds = [e["kind"] for e in view["audit_trail"]]
        assert "approval" not in kinds

    def test_14_reject_with_foreign_tenant_id_does_not_mutate(
        self,
        shared_bus: InMemoryEventBus,
        attached_client: TestClient,
    ) -> None:
        _configure_approval_policy("bank-a")
        _emit(shared_bus, tenant_id="bank-a", event_id="evt_iso_14")
        pending = attached_client.get(
            "/approvals", params={"tenant_id": "bank-a"}
        ).json()["approvals"]
        action_id = pending[0]["action_id"]

        r = attached_client.post(
            f"/approvals/{action_id}/reject",
            params={"tenant_id": "bank-b", "reason": "drive-by", "decided_by": "attacker"},
        )
        assert r.status_code == 404

        # State unchanged: still pending under bank-a.
        view = attached_client.get(
            f"/actions/{action_id}", params={"tenant_id": "bank-a"}
        ).json()
        assert view["status"] == "pending_approval"
        kinds = [e["kind"] for e in view["audit_trail"]]
        assert "rejection" not in kinds

    def test_15_per_tenant_listings_isolated_after_concurrent_ingest(
        self,
        shared_bus: InMemoryEventBus,
        attached_client: TestClient,
    ) -> None:
        # Both tenants generate pending actions through the same
        # pipeline. Each tenant's `/approvals` view sees only its
        # own — and approving bank-a's action does not change
        # bank-b's pending count. `/events/recent` and
        # `/proposals/recent` are similarly isolated.
        _configure_approval_policy("bank-a")
        _configure_approval_policy("bank-b")
        _emit(shared_bus, tenant_id="bank-a", event_id="evt_iso_15a_1")
        _emit(shared_bus, tenant_id="bank-a", event_id="evt_iso_15a_2")
        _emit(shared_bus, tenant_id="bank-b", event_id="evt_iso_15b_1")

        a_approvals = attached_client.get(
            "/approvals", params={"tenant_id": "bank-a"}
        ).json()
        b_approvals = attached_client.get(
            "/approvals", params={"tenant_id": "bank-b"}
        ).json()
        assert a_approvals["count"] == 2
        assert b_approvals["count"] == 1
        a_event_ids = {x["event_id"] for x in a_approvals["approvals"]}
        b_event_ids = {x["event_id"] for x in b_approvals["approvals"]}
        assert a_event_ids == {"evt_iso_15a_1", "evt_iso_15a_2"}
        assert b_event_ids == {"evt_iso_15b_1"}
        assert a_event_ids.isdisjoint(b_event_ids)

        # /events/recent — bounded per-tenant ring buffers.
        a_recent = attached_client.get(
            "/events/recent", params={"tenant_id": "bank-a"}
        ).json()
        b_recent = attached_client.get(
            "/events/recent", params={"tenant_id": "bank-b"}
        ).json()
        a_recent_ids = {e["event_id"] for e in a_recent["events"]}
        b_recent_ids = {e["event_id"] for e in b_recent["events"]}
        assert a_recent_ids == {"evt_iso_15a_1", "evt_iso_15a_2"}
        assert b_recent_ids == {"evt_iso_15b_1"}

        # /proposals/recent — same isolation guarantee.
        a_proposals = attached_client.get(
            "/proposals/recent", params={"tenant_id": "bank-a"}
        ).json()
        b_proposals = attached_client.get(
            "/proposals/recent", params={"tenant_id": "bank-b"}
        ).json()
        assert a_proposals["count"] == 2
        assert b_proposals["count"] == 1
        a_prop_tenants = {p["tenant_id"] for p in a_proposals["proposals"]}
        b_prop_tenants = {p["tenant_id"] for p in b_proposals["proposals"]}
        assert a_prop_tenants == {"bank-a"}
        assert b_prop_tenants == {"bank-b"}

        # Approving bank-a's first action does not drain bank-b.
        a_first_id = a_approvals["approvals"][0]["action_id"]
        approve = attached_client.post(
            f"/approvals/{a_first_id}/approve",
            params={"tenant_id": "bank-a"},
        )
        assert approve.status_code == 200

        b_after = attached_client.get(
            "/approvals", params={"tenant_id": "bank-b"}
        ).json()
        assert b_after["count"] == 1
        assert b_after["approvals"][0]["event_id"] == "evt_iso_15b_1"
