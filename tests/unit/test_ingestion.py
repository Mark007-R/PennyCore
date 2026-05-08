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

from context_engine.api import app, get_bus, get_repo
from context_engine.event_bus import InMemoryEventBus, channel_for, envelope_for
from context_engine.ingestion import (
    IngestionRequest,
    build_event_from_request,
    ingest_event,
)
from context_engine.repository import InMemoryEventRepository
from contracts import ChannelType, Event, EventType

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
def client(
    repo: InMemoryEventRepository, bus: InMemoryEventBus
) -> Iterator[TestClient]:
    app.dependency_overrides[get_repo] = lambda: repo
    app.dependency_overrides[get_bus] = lambda: bus
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
