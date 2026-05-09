"""Day 5 — context-engine ingestion tests.

Coverage:
  * `ingest_event` pure-logic happy path + idempotent replay (no HTTP).
  * `POST /events` HTTP surface: 202 on create, 200 on replay, 400 on
    missing-key, 422 on invalid enums, 422 on extra body fields.
  * Multi-tenant: same idempotency_key under different tenants is two
    distinct events (the (tenant_id, idempotency_key) UNIQUE invariant).
  * Bus emission: exactly one publish on create, zero on replay.
  * Header vs body idempotency-key reconciliation.

The HTTP tests use FastAPI's `app.dependency_overrides` to inject fresh
in-memory repo + bus per test — without this, tests would share state via
the module-level singletons and cross-pollute.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from context_engine.api import app, get_bus, get_customer_repo, get_repo
from context_engine.customer_repository import InMemoryCustomerRepository
from context_engine.event_bus import InMemoryEventBus, channel_for, envelope_for
from context_engine.ingestion import (
    IngestionRequest,
    build_event_from_request,
    ingest_event,
)
from context_engine.repository import InMemoryEventRepository
from contracts import ChannelType, Event, EventType, IdentityKind

# ---------------------------------------------------------------------------
# Fixtures: per-test fresh repo + bus, injected via dependency_overrides.
# ---------------------------------------------------------------------------


@pytest.fixture
def repo() -> InMemoryEventRepository:
    return InMemoryEventRepository()


@pytest.fixture
def bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture
def customer_repo() -> InMemoryCustomerRepository:
    return InMemoryCustomerRepository()


@pytest.fixture
def client(
    repo: InMemoryEventRepository,
    bus: InMemoryEventBus,
    customer_repo: InMemoryCustomerRepository,
) -> Iterator[TestClient]:
    app.dependency_overrides[get_repo] = lambda: repo
    app.dependency_overrides[get_bus] = lambda: bus
    app.dependency_overrides[get_customer_repo] = lambda: customer_repo
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _body(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tenant_id": "acme-bank",
        "channel_code": "sms",
        "event_type": "message_received",
        "idempotency_key": "msg-abc-123",
        "payload": {"text": "Hi, did my W-2 go through?"},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Pure-logic tests (no HTTP)
# ---------------------------------------------------------------------------


class TestIngestEventPureLogic:
    def _make_event(self, **overrides: object) -> Event:
        request = IngestionRequest(**_body(**overrides))  # type: ignore[arg-type]
        return build_event_from_request(request, idempotency_key=request.idempotency_key or "k")

    def test_happy_path_creates_and_publishes(
        self, repo: InMemoryEventRepository, bus: InMemoryEventBus
    ) -> None:
        event = self._make_event()
        result = ingest_event(event, repo=repo, bus=bus)
        assert result.created is True
        assert result.event.id == event.id
        assert repo.count() == 1
        assert len(bus.published) == 1
        channel, envelope = bus.published[0]
        assert channel == channel_for("acme-bank")
        assert envelope == envelope_for(event)

    def test_replay_does_not_republish(
        self, repo: InMemoryEventRepository, bus: InMemoryEventBus
    ) -> None:
        event_a = self._make_event()
        ingest_event(event_a, repo=repo, bus=bus)

        # Same idempotency_key → second ingest returns the FIRST event,
        # does NOT publish again.
        event_b = self._make_event()
        result_b = ingest_event(event_b, repo=repo, bus=bus)
        assert result_b.created is False
        assert result_b.event.id == event_a.id  # original wins
        assert result_b.event.id != event_b.id
        assert repo.count() == 1
        assert len(bus.published) == 1  # still just the first publish

    def test_different_tenants_same_idem_key_are_distinct(
        self, repo: InMemoryEventRepository, bus: InMemoryEventBus
    ) -> None:
        # The UNIQUE oracle is (tenant_id, idempotency_key) — Bank A and
        # Bank B can both have an event keyed "msg-abc-123" without collision.
        event_a = self._make_event(tenant_id="bank-a")
        event_b = self._make_event(tenant_id="bank-b")
        result_a = ingest_event(event_a, repo=repo, bus=bus)
        result_b = ingest_event(event_b, repo=repo, bus=bus)
        assert result_a.created is True
        assert result_b.created is True
        assert result_a.event.id != result_b.event.id
        assert repo.count() == 2
        assert {ch for ch, _ in bus.published} == {
            channel_for("bank-a"),
            channel_for("bank-b"),
        }


# ---------------------------------------------------------------------------
# HTTP surface tests
# ---------------------------------------------------------------------------


class TestPostEventHTTP:
    def test_happy_path_returns_202(
        self, client: TestClient, repo: InMemoryEventRepository
    ) -> None:
        r = client.post("/events", json=_body())
        assert r.status_code == 202
        body = r.json()
        assert body["created"] is True
        assert body["deduped"] is False
        assert body["tenant_id"] == "acme-bank"
        assert body["channel_code"] == "sms"
        assert body["event_type"] == "message_received"
        assert body["idempotency_key"] == "msg-abc-123"
        assert body["event_id"].startswith("evt_")
        # received_at must round-trip as a tz-aware ISO string.
        parsed = datetime.fromisoformat(body["received_at"])
        assert parsed.tzinfo is not None
        # Sanity: received_at is recent.
        assert (datetime.now(timezone.utc) - parsed).total_seconds() < 5
        assert repo.count() == 1

    def test_replay_returns_200(
        self, client: TestClient, repo: InMemoryEventRepository, bus: InMemoryEventBus
    ) -> None:
        first = client.post("/events", json=_body())
        assert first.status_code == 202
        original_id = first.json()["event_id"]

        replay = client.post("/events", json=_body())
        assert replay.status_code == 200
        body = replay.json()
        assert body["created"] is False
        assert body["deduped"] is True
        assert body["event_id"] == original_id  # same canonical row
        assert repo.count() == 1
        assert len(bus.published) == 1  # no second publish

    def test_idempotency_via_header(
        self, client: TestClient, repo: InMemoryEventRepository
    ) -> None:
        # Body omits idempotency_key; header supplies it.
        body = _body()
        body.pop("idempotency_key")
        r = client.post(
            "/events",
            json=body,
            headers={"X-Idempotency-Key": "header-key-xyz"},
        )
        assert r.status_code == 202
        assert r.json()["idempotency_key"] == "header-key-xyz"
        assert repo.count() == 1

    def test_idempotency_header_wins_when_matching_body(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/events",
            json=_body(idempotency_key="same-key"),
            headers={"X-Idempotency-Key": "same-key"},
        )
        assert r.status_code == 202

    def test_idempotency_header_body_disagree_returns_400(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/events",
            json=_body(idempotency_key="body-key"),
            headers={"X-Idempotency-Key": "header-key"},
        )
        assert r.status_code == 400
        assert "disagree" in r.json()["detail"].lower()

    def test_no_idempotency_key_anywhere_returns_400(
        self, client: TestClient
    ) -> None:
        body = _body()
        body.pop("idempotency_key")
        r = client.post("/events", json=body)
        assert r.status_code == 400
        assert "idempotency_key" in r.json()["detail"]

    def test_invalid_channel_returns_422(self, client: TestClient) -> None:
        r = client.post("/events", json=_body(channel_code="telegram"))
        assert r.status_code == 422

    def test_invalid_event_type_returns_422(self, client: TestClient) -> None:
        r = client.post("/events", json=_body(event_type="invented_event"))
        assert r.status_code == 422

    def test_extra_field_rejected(self, client: TestClient) -> None:
        r = client.post("/events", json=_body(spurious="value"))
        assert r.status_code == 422

    def test_missing_tenant_id_returns_422(self, client: TestClient) -> None:
        body = _body()
        body.pop("tenant_id")
        r = client.post("/events", json=body)
        assert r.status_code == 422

    def test_supports_all_channel_types(self, client: TestClient) -> None:
        # One event per channel — sanity check the enum is wired through.
        for i, channel in enumerate(ChannelType):
            r = client.post(
                "/events",
                json=_body(
                    channel_code=channel.value,
                    idempotency_key=f"per-channel-{i}",
                ),
            )
            assert r.status_code == 202, (channel, r.text)

    def test_supports_all_event_types(self, client: TestClient) -> None:
        for i, etype in enumerate(EventType):
            r = client.post(
                "/events",
                json=_body(
                    event_type=etype.value,
                    idempotency_key=f"per-type-{i}",
                ),
            )
            assert r.status_code == 202, (etype, r.text)

    def test_publish_envelope_has_no_payload(
        self, client: TestClient, bus: InMemoryEventBus
    ) -> None:
        # The envelope is a small metadata dict — it must NOT contain the
        # raw payload (PII protection at the wire level, SKILL §5.2).
        client.post("/events", json=_body(payload={"text": "secret-account-123"}))
        assert len(bus.published) == 1
        _, envelope = bus.published[0]
        assert "payload" not in envelope
        # And no payload string snuck into envelope values.
        assert "secret-account-123" not in str(envelope)


# ---------------------------------------------------------------------------
# Customer linking — Day 6 wiring
# ---------------------------------------------------------------------------


class TestPostEventCustomerLinking:
    """End-to-end HTTP tests for the linker integrated into POST /events.

    Pure-logic linker behavior is exercised in `test_linking.py`; this
    suite verifies the API surface honors it correctly.
    """

    def test_first_event_creates_customer(
        self, client: TestClient, customer_repo: InMemoryCustomerRepository
    ) -> None:
        r = client.post(
            "/events",
            json=_body(payload={"from_email": "jane@acme.com", "text": "Hi"}),
        )
        assert r.status_code == 202
        body = r.json()
        assert body["customer_id"].startswith("cust_")
        assert body["customer_created"] is True
        assert body["matched_identity"] is None
        assert customer_repo.count(tenant_id="acme-bank") == 1

    def test_second_event_same_email_links_existing(
        self, client: TestClient, customer_repo: InMemoryCustomerRepository
    ) -> None:
        first = client.post(
            "/events",
            json=_body(
                idempotency_key="msg-1",
                payload={"from_email": "jane@acme.com"},
            ),
        )
        first_cid = first.json()["customer_id"]

        second = client.post(
            "/events",
            json=_body(
                idempotency_key="msg-2",
                payload={"from_email": "JANE@acme.com", "text": "again"},
            ),
        )
        assert second.status_code == 202  # different idempotency key, new event
        body = second.json()
        assert body["customer_id"] == first_cid
        assert body["customer_created"] is False
        assert body["matched_identity"] == {
            "kind": "email",
            "value": "jane@acme.com",
        }
        assert customer_repo.count(tenant_id="acme-bank") == 1

    def test_cross_channel_link_via_external_id(
        self, client: TestClient, customer_repo: InMemoryCustomerRepository
    ) -> None:
        # SMS first (phone + external_id)…
        sms = client.post(
            "/events",
            json=_body(
                channel_code="sms",
                idempotency_key="sms-1",
                payload={
                    "from_phone": "+15550100100",
                    "external_id": "CRM-001",
                    "text": "hi",
                },
            ),
        )
        assert sms.status_code == 202
        sms_cid = sms.json()["customer_id"]

        # …then email arrives with same external_id but new email.
        email = client.post(
            "/events",
            json=_body(
                channel_code="email",
                idempotency_key="email-1",
                payload={
                    "external_id": "CRM-001",
                    "from_email": "jane@acme.com",
                    "subject": "Mortgage q",
                },
            ),
        )
        assert email.status_code == 202
        body = email.json()
        assert body["customer_id"] == sms_cid
        assert body["customer_created"] is False
        # external_id beats email in priority.
        assert body["matched_identity"]["kind"] == "external_id"
        assert body["matched_identity"]["value"] == "CRM-001"
        # The email is now also attached to that customer.
        idents = customer_repo.list_identities(
            tenant_id="acme-bank", customer_id=sms_cid
        )
        assert {i.identity_kind for i in idents} == {
            IdentityKind.EMAIL,
            IdentityKind.PHONE,
            IdentityKind.EXTERNAL_ID,
        }

    def test_replay_returns_same_customer_no_link_run(
        self,
        client: TestClient,
        customer_repo: InMemoryCustomerRepository,
        bus: InMemoryEventBus,
    ) -> None:
        first = client.post(
            "/events",
            json=_body(payload={"from_email": "jane@acme.com"}),
        )
        first_cid = first.json()["customer_id"]
        # Sanity: one customer, one identity row.
        assert customer_repo.count(tenant_id="acme-bank") == 1

        # Replay with TAMPERED payload — added a new fake email. The
        # replay short-circuit must return the original event with the
        # original customer_id and MUST NOT touch the customer store.
        replay = client.post(
            "/events",
            json=_body(payload={"from_email": "JANE@ACME.com", "from_phone": "+15550100100"}),
        )
        assert replay.status_code == 200
        body = replay.json()
        assert body["customer_id"] == first_cid
        assert body["customer_created"] is False
        assert body["matched_identity"] is None
        assert body["deduped"] is True
        # Customer store unchanged — no new identity from the tampered payload.
        idents = customer_repo.list_identities(
            tenant_id="acme-bank", customer_id=first_cid
        )
        assert {i.identity_value for i in idents} == {"jane@acme.com"}
        assert len(bus.published) == 1  # still no second publish

    def test_explicit_unknown_customer_id_returns_404(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/events",
            json=_body(customer_id="cust_made_up_123"),
        )
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()

    def test_multi_tenant_isolation_creates_two_customers(
        self, client: TestClient, customer_repo: InMemoryCustomerRepository
    ) -> None:
        # Same email under two tenants → two distinct customers.
        r_a = client.post(
            "/events",
            json=_body(
                tenant_id="bank-a",
                idempotency_key="k-a",
                payload={"from_email": "jane@acme.com"},
            ),
        )
        r_b = client.post(
            "/events",
            json=_body(
                tenant_id="bank-b",
                idempotency_key="k-b",
                payload={"from_email": "jane@acme.com"},
            ),
        )
        assert r_a.status_code == 202
        assert r_b.status_code == 202
        cid_a = r_a.json()["customer_id"]
        cid_b = r_b.json()["customer_id"]
        assert cid_a != cid_b
        assert customer_repo.count(tenant_id="bank-a") == 1
        assert customer_repo.count(tenant_id="bank-b") == 1
        # Tenant A cannot see tenant B's customer by id.
        assert (
            customer_repo.get_customer(tenant_id="bank-a", customer_id=cid_b)
            is None
        )

    def test_no_hints_no_explicit_id_creates_anonymous_customer(
        self, client: TestClient, customer_repo: InMemoryCustomerRepository
    ) -> None:
        # Channels can produce events with no identity hints (e.g. an
        # `anomaly_detected` system event). We still need to attribute it.
        r = client.post(
            "/events",
            json=_body(
                event_type="anomaly_detected",
                payload={"reason": "rate-limit-spike"},
            ),
        )
        assert r.status_code == 202
        body = r.json()
        assert body["customer_id"].startswith("cust_")
        assert body["customer_created"] is True
        idents = customer_repo.list_identities(
            tenant_id="acme-bank", customer_id=body["customer_id"]
        )
        assert idents == []  # anonymous — no identities

    def test_explicit_customer_id_path_attaches_payload_identities(
        self, client: TestClient, customer_repo: InMemoryCustomerRepository
    ) -> None:
        # Pre-create the customer with an email; client passes that id
        # explicitly along with a phone in the payload — phone should be
        # attached as a new identity.
        existing = customer_repo.create_customer(
            tenant_id="acme-bank",
            identities=[(IdentityKind.EMAIL, "jane@acme.com")],
        )
        r = client.post(
            "/events",
            json=_body(
                customer_id=existing.id,
                payload={"from_phone": "+15550100100"},
            ),
        )
        assert r.status_code == 202
        body = r.json()
        assert body["customer_id"] == existing.id
        assert body["customer_created"] is False
        idents = customer_repo.list_identities(
            tenant_id="acme-bank", customer_id=existing.id
        )
        assert {i.identity_kind for i in idents} == {
            IdentityKind.EMAIL,
            IdentityKind.PHONE,
        }
