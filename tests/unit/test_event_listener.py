"""Tests for the Day-8 orchestrator event listener.

Three surfaces under test:

  1. `RecentEventsBuffer` — bounded, tenant-scoped ring buffer that the
     `/events/recent` endpoint serves. Multi-tenant isolation here is the
     same property the Day-20 isolation tests will assert at the data
     layer; we lock it in early.

  2. `InMemoryEventListener` — synchronous in-process bridge between
     `InMemoryEventBus` publishes and a handler. Used by the Day-11
     end-to-end scenario.

  3. `RedisEventListener` — pump thread that decodes JSON envelopes from
     a Pub/Sub channel and invokes the handler. Tested with a fake
     pubsub client so no live broker is needed.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from context_engine.event_bus import InMemoryEventBus, channel_for, envelope_for
from contracts import ChannelType, Event, EventType
from datetime import datetime, timezone

from orchestrator.event_listener import (
    InMemoryEventListener,
    RecentEventsBuffer,
    RedisEventListener,
    make_buffered_handler,
)


# ----------------------------------------------------------------------------
# RecentEventsBuffer
# ----------------------------------------------------------------------------


def _envelope(event_id: str, tenant_id: str = "acme") -> dict[str, Any]:
    return {
        "event_id": event_id,
        "tenant_id": tenant_id,
        "customer_id": "cust_x",
        "channel_code": "email",
        "event_type": "message_received",
        "received_at": "2026-05-11T12:00:00+00:00",
    }


def test_recent_buffer_returns_most_recent_first() -> None:
    buf = RecentEventsBuffer(max_per_tenant=10)
    for i in range(5):
        buf.append(_envelope(f"evt_{i}"))
    out = buf.recent("acme", limit=3)
    assert [e["event_id"] for e in out] == ["evt_4", "evt_3", "evt_2"]


def test_recent_buffer_caps_per_tenant_independently() -> None:
    buf = RecentEventsBuffer(max_per_tenant=2)
    for i in range(5):
        buf.append(_envelope(f"evt_a_{i}", tenant_id="tenantA"))
        buf.append(_envelope(f"evt_b_{i}", tenant_id="tenantB"))
    a = buf.recent("tenantA", limit=10)
    b = buf.recent("tenantB", limit=10)
    assert [e["event_id"] for e in a] == ["evt_a_4", "evt_a_3"]
    assert [e["event_id"] for e in b] == ["evt_b_4", "evt_b_3"]


def test_recent_buffer_isolates_tenants() -> None:
    """Buffer is keyed by tenant — querying tenantA never returns
    tenantB's events even when both are present."""
    buf = RecentEventsBuffer()
    buf.append(_envelope("evt_a", tenant_id="tenantA"))
    buf.append(_envelope("evt_b", tenant_id="tenantB"))

    a = buf.recent("tenantA", limit=10)
    b = buf.recent("tenantB", limit=10)
    assert [e["event_id"] for e in a] == ["evt_a"]
    assert [e["event_id"] for e in b] == ["evt_b"]


def test_recent_buffer_drops_envelope_without_tenant_id() -> None:
    buf = RecentEventsBuffer()
    buf.append({"event_id": "evt_orphan"})  # no tenant_id
    assert buf.recent("acme") == []


def test_recent_buffer_empty_for_unknown_tenant() -> None:
    buf = RecentEventsBuffer()
    assert buf.recent("never-published") == []


# ----------------------------------------------------------------------------
# InMemoryEventListener
# ----------------------------------------------------------------------------


def _build_event(tenant_id: str = "acme", event_id: str = "evt_t8_il_001") -> Event:
    return Event(
        id=event_id,
        tenant_id=tenant_id,
        customer_id="cust_t8_001",
        channel_code=ChannelType.EMAIL,
        event_type=EventType.MESSAGE_RECEIVED,
        idempotency_key=f"idem-{event_id}",
        payload={"subject": "hi"},
        received_at=datetime(2026, 5, 11, 12, 0, 0, tzinfo=timezone.utc),
    )


def test_in_memory_listener_routes_publishes_to_handler() -> None:
    bus = InMemoryEventBus()
    buf = RecentEventsBuffer()
    listener = InMemoryEventListener(bus)
    listener.start(make_buffered_handler(buf))

    event = _build_event()
    bus.publish(channel_for(event.tenant_id), envelope_for(event))

    out = buf.recent("acme")
    assert len(out) == 1
    assert out[0]["event_id"] == "evt_t8_il_001"
    listener.stop()


def test_in_memory_listener_stop_unsubscribes() -> None:
    bus = InMemoryEventBus()
    buf = RecentEventsBuffer()
    listener = InMemoryEventListener(bus)
    listener.start(make_buffered_handler(buf))
    listener.stop()

    event = _build_event(event_id="evt_after_stop")
    bus.publish(channel_for(event.tenant_id), envelope_for(event))

    assert buf.recent("acme") == []


def test_in_memory_listener_double_start_is_idempotent() -> None:
    """Re-arming the same listener must not fan out duplicate handler calls."""
    bus = InMemoryEventBus()
    buf = RecentEventsBuffer()
    listener = InMemoryEventListener(bus)
    listener.start(make_buffered_handler(buf))
    listener.start(make_buffered_handler(buf))  # second call should no-op

    event = _build_event()
    bus.publish(channel_for(event.tenant_id), envelope_for(event))

    out = buf.recent("acme")
    assert len(out) == 1
    listener.stop()


def test_in_memory_listener_handler_failure_does_not_break_publish() -> None:
    """A buggy handler must not poison the publisher — the publish itself
    still records and other subscribers (none here) still run."""
    bus = InMemoryEventBus()
    listener = InMemoryEventListener(bus)

    def boom(_envelope: dict[str, Any]) -> None:
        raise RuntimeError("handler is angry")

    listener.start(boom)

    event = _build_event()
    # No exception bubbles out of publish.
    bus.publish(channel_for(event.tenant_id), envelope_for(event))

    assert len(bus.published) == 1
    listener.stop()


# ----------------------------------------------------------------------------
# RedisEventListener — fake pubsub client
# ----------------------------------------------------------------------------


class _FakePubSub:
    """In-memory queue with the small subset of `redis.client.PubSub` we use."""

    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []
        self._subscribed_patterns: list[str] = []
        self._lock = threading.Lock()
        self.closed = False

    def psubscribe(self, pattern: str) -> None:
        self._subscribed_patterns.append(pattern)

    def punsubscribe(self, _pattern: str) -> None:
        self._subscribed_patterns.clear()

    def push(self, channel: str, data: str) -> None:
        with self._lock:
            self._messages.append(
                {"type": "pmessage", "channel": channel, "data": data}
            )

    def get_message(
        self,
        ignore_subscribe_messages: bool = True,  # noqa: ARG002
        timeout: float = 0.5,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        with self._lock:
            if not self._messages:
                return None
            return self._messages.pop(0)

    def close(self) -> None:
        self.closed = True


class _FakeRedisWithPubSub:
    def __init__(self) -> None:
        self.pubsub_obj = _FakePubSub()

    def pubsub(self) -> _FakePubSub:
        return self.pubsub_obj


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_redis_listener_decodes_and_dispatches() -> None:
    fake_redis = _FakeRedisWithPubSub()
    buf = RecentEventsBuffer()
    listener = RedisEventListener(
        client=fake_redis, pattern="events.*", poll_timeout=0.05
    )
    listener.start(make_buffered_handler(buf))

    envelope = _envelope("evt_redis_001", tenant_id="acme")
    fake_redis.pubsub_obj.push("events.acme", json.dumps(envelope))

    assert _wait_until(lambda: len(buf.recent("acme")) == 1), (
        "listener thread never delivered the envelope"
    )
    out = buf.recent("acme")
    assert out[0]["event_id"] == "evt_redis_001"

    listener.stop()
    assert fake_redis.pubsub_obj.closed is True


def test_redis_listener_drops_malformed_json() -> None:
    fake_redis = _FakeRedisWithPubSub()
    buf = RecentEventsBuffer()
    listener = RedisEventListener(
        client=fake_redis, pattern="events.*", poll_timeout=0.05
    )
    listener.start(make_buffered_handler(buf))

    fake_redis.pubsub_obj.push("events.acme", "not-json{{{")
    fake_redis.pubsub_obj.push(
        "events.acme",
        json.dumps(_envelope("evt_after_bad", tenant_id="acme")),
    )

    assert _wait_until(lambda: len(buf.recent("acme")) == 1), (
        "good envelope after malformed one should still arrive"
    )
    listener.stop()


def test_redis_listener_isolates_tenants_at_buffer_layer() -> None:
    fake_redis = _FakeRedisWithPubSub()
    buf = RecentEventsBuffer()
    listener = RedisEventListener(
        client=fake_redis, pattern="events.*", poll_timeout=0.05
    )
    listener.start(make_buffered_handler(buf))

    fake_redis.pubsub_obj.push(
        "events.tenantA", json.dumps(_envelope("evt_a", tenant_id="tenantA"))
    )
    fake_redis.pubsub_obj.push(
        "events.tenantB", json.dumps(_envelope("evt_b", tenant_id="tenantB"))
    )

    assert _wait_until(
        lambda: len(buf.recent("tenantA")) == 1 and len(buf.recent("tenantB")) == 1
    )
    a_ids = [e["event_id"] for e in buf.recent("tenantA")]
    b_ids = [e["event_id"] for e in buf.recent("tenantB")]
    assert a_ids == ["evt_a"]
    assert b_ids == ["evt_b"]

    listener.stop()


def test_redis_listener_handler_failure_does_not_kill_pump() -> None:
    fake_redis = _FakeRedisWithPubSub()
    seen: list[str] = []

    def flaky(envelope: dict[str, Any]) -> None:
        if envelope["event_id"] == "evt_bad":
            raise RuntimeError("boom")
        seen.append(envelope["event_id"])

    listener = RedisEventListener(
        client=fake_redis, pattern="events.*", poll_timeout=0.05
    )
    listener.start(flaky)

    fake_redis.pubsub_obj.push(
        "events.acme", json.dumps(_envelope("evt_bad", tenant_id="acme"))
    )
    fake_redis.pubsub_obj.push(
        "events.acme", json.dumps(_envelope("evt_good", tenant_id="acme"))
    )

    assert _wait_until(lambda: seen == ["evt_good"])
    listener.stop()


def test_redis_listener_stop_is_idempotent() -> None:
    fake_redis = _FakeRedisWithPubSub()
    listener = RedisEventListener(client=fake_redis, poll_timeout=0.05)
    listener.start(lambda _e: None)
    listener.stop()
    listener.stop()  # second stop should not raise
