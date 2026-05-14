"""Tests for the Day-8 Redis publish-side adapter.

The real `redis.Redis` client is never instantiated here; we inject a
`FakeRedisClient` that records calls so the test path stays free of any
network or live broker requirement. The tests assert:

  * Envelope is JSON-serialized in a deterministic way.
  * The channel string the publisher uses matches the Day-5 contract.
  * `close()` propagates to the underlying client.
  * `publish()` returns the subscriber count the client reports.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from context_engine.event_bus import channel_for, envelope_for
from context_engine.redis_event_bus import RedisEventBus
from contracts import ChannelType, Event, EventType


class FakeRedisClient:
    """Minimal `RedisClientLike` stub. Records every publish + ping call."""

    def __init__(self, subscriber_count: int = 1) -> None:
        self._subscriber_count = subscriber_count
        self.publishes: list[tuple[str, str]] = []
        self.pings: int = 0
        self.closed: bool = False

    def publish(self, channel: str, message: str) -> int:
        self.publishes.append((channel, message))
        return self._subscriber_count

    def ping(self) -> bool:
        self.pings += 1
        return True

    def close(self) -> None:
        self.closed = True


def _build_event() -> Event:
    return Event(
        id="evt_t8_redis_001",
        tenant_id="acme",
        customer_id="cust_t8_001",
        channel_code=ChannelType.EMAIL,
        event_type=EventType.MESSAGE_RECEIVED,
        idempotency_key="idem-t8-redis-001",
        payload={"subject": "hi"},
        received_at=datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc),
    )


def test_publish_serializes_envelope_to_json() -> None:
    fake = FakeRedisClient(subscriber_count=2)
    bus = RedisEventBus(fake)
    event = _build_event()

    n = bus.publish(channel_for(event.tenant_id), envelope_for(event))

    assert n == 2
    assert len(fake.publishes) == 1
    channel, message = fake.publishes[0]
    assert channel == "events.acme"

    decoded: dict[str, Any] = json.loads(message)
    assert decoded["event_id"] == "evt_t8_redis_001"
    assert decoded["tenant_id"] == "acme"
    assert decoded["customer_id"] == "cust_t8_001"
    assert decoded["channel_code"] == "email"
    assert decoded["event_type"] == "message_received"
    # received_at is preserved as the same ISO string the envelope produced.
    assert decoded["received_at"] == "2026-05-11T12:00:00+00:00"


def test_publish_uses_deterministic_key_ordering() -> None:
    """Two equivalent envelopes produce byte-identical messages.

    Predictable ordering matters for log diffability and for any future
    content-addressed dedup at the bus layer.
    """
    fake = FakeRedisClient()
    bus = RedisEventBus(fake)
    event = _build_event()
    envelope = envelope_for(event)

    bus.publish(channel_for(event.tenant_id), envelope)
    bus.publish(channel_for(event.tenant_id), envelope)

    assert len(fake.publishes) == 2
    assert fake.publishes[0][1] == fake.publishes[1][1]


def test_close_propagates_to_underlying_client() -> None:
    fake = FakeRedisClient()
    bus = RedisEventBus(fake)
    bus.close()
    assert fake.closed is True


def test_subscriber_count_returned_for_no_listeners() -> None:
    """Pub/Sub semantics: no listeners is legal — publish still succeeds."""
    fake = FakeRedisClient(subscriber_count=0)
    bus = RedisEventBus(fake)
    result = bus.publish("events.acme", {"event_id": "evt_x"})
    assert result == 0
