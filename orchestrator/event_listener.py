"""orchestrator event listener (Day 8, Phase 2).

Subscribes to the `events.<tenant_id>` channels published by the
context-engine ingestion handler. Two implementations satisfy the same
shape:

  * `InMemoryEventListener` — registers a callback on a shared
    `InMemoryEventBus`. Used by unit tests and the in-process Day-11
    end-to-end scenario where both apps run under one TestClient.

  * `RedisEventListener` — opens a Redis Pub/Sub pattern subscription
    (`events.*`) and runs a worker thread that decodes envelopes and
    invokes the handler. Used in containerized dev (docker-compose) and
    in production.

The handler signature is `(envelope: dict[str, Any]) -> None`. The
default handler in this module appends to a tenant-scoped ring buffer
that the `/events/recent` endpoint serves; Day 9 swaps in the LLM
planner; Day 10 layers the policy + queue + audit pipeline on top.

Multi-tenant invariant: the orchestrator listens across all tenants
(pattern `events.*`) because it serves all of them, but every downstream
consumer scopes by `envelope["tenant_id"]`. The `RecentEventsBuffer`
indexes by tenant so a `/events/recent?tenant_id=X` query CANNOT see
tenant Y's traffic — same shape Day 5 already enforced on the publish
side.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict, deque
from typing import Any, Callable, Protocol, runtime_checkable

from context_engine.event_bus import InMemoryEventBus

EventHandler = Callable[[dict[str, Any]], None]

# Pattern subscribed by the Redis listener. Must match
# `context_engine.event_bus.EVENT_CHANNEL_PREFIX` plus the per-tenant
# suffix the publisher uses (`events.<tenant_id>`).
DEFAULT_EVENT_PATTERN = "events.*"


@runtime_checkable
class EventListener(Protocol):
    """Lifecycle contract every listener implementation honors.

    `start(handler)` arms the subscription and routes every received
    envelope to `handler`. `stop()` releases all resources (threads,
    sockets, callback registrations) so the FastAPI lifespan can shut
    down cleanly under SIGTERM.
    """

    def start(self, handler: EventHandler) -> None: ...

    def stop(self) -> None: ...


class RecentEventsBuffer:
    """Tenant-scoped ring buffer for the `/events/recent` diagnostic endpoint.

    The buffer is bounded per-tenant (default 50) so a noisy tenant cannot
    push another tenant's traffic out. This is the same isolation property
    the Day-20 multi-tenant tests will assert at the data layer; building
    it in here means the diagnostic surface inherits the invariant for free.
    """

    def __init__(self, max_per_tenant: int = 50) -> None:
        self._max = max_per_tenant
        self._lock = threading.RLock()
        self._by_tenant: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=self._max)
        )

    def append(self, envelope: dict[str, Any]) -> None:
        tenant_id = envelope.get("tenant_id")
        if not tenant_id:
            # Malformed envelope — Day-23 hardening adds a quarantine
            # queue. For Day 8 we drop silently; the test suite asserts
            # well-formed envelopes from the publisher side.
            return
        with self._lock:
            self._by_tenant[tenant_id].append(envelope)

    def recent(self, tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            buf = self._by_tenant.get(tenant_id)
            if not buf:
                return []
            # Most recent first, capped at `limit`.
            items = list(buf)
            items.reverse()
            return items[:limit]

    def clear(self) -> None:
        with self._lock:
            self._by_tenant.clear()


class InMemoryEventListener:
    """Routes `InMemoryEventBus` publishes directly into the handler.

    No threads, no sockets — the handler runs on the caller's thread the
    moment ingestion publishes. That makes assertions in unit tests
    trivially deterministic ("publish then immediately inspect the
    buffer"). The Day-11 end-to-end scenario relies on this synchronous
    behavior to assemble the brief inside the same TestClient call stack.
    """

    def __init__(self, bus: InMemoryEventBus) -> None:
        self._bus = bus
        self._callback: Callable[[str, dict[str, Any]], None] | None = None
        self._handler: EventHandler | None = None

    def start(self, handler: EventHandler) -> None:
        if self._callback is not None:
            # Idempotent on double-start — re-arming would just register
            # the callback twice and cause duplicate handler invocations.
            return
        self._handler = handler

        def _adapter(_channel: str, envelope: dict[str, Any]) -> None:
            assert self._handler is not None
            self._handler(envelope)

        self._callback = _adapter
        self._bus.subscribe(_adapter)

    def stop(self) -> None:
        if self._callback is None:
            return
        self._bus.unsubscribe(self._callback)
        self._callback = None
        self._handler = None


class RedisEventListener:
    """Pattern-subscribes to `events.*` on a Redis client and pumps
    envelopes into the handler from a daemon worker thread.

    The worker uses `pubsub.get_message(timeout=...)` rather than the
    SDK's `listen()` blocking iterator so `stop()` can interrupt the loop
    within `<= timeout` seconds without needing to inject a poison-pill
    message. The thread is daemonized as a defense in depth — if a future
    bug makes `stop()` unreachable, process exit still works.

    The injected `client` is anything satisfying the small Pub/Sub surface
    we exercise (real `redis.Redis` in production; `FakeRedisClient` in
    tests). Keeping the dependency on a Protocol means tests don't need a
    live broker.
    """

    def __init__(
        self,
        client: Any,
        pattern: str = DEFAULT_EVENT_PATTERN,
        poll_timeout: float = 0.5,
    ) -> None:
        self._client = client
        self._pattern = pattern
        self._poll_timeout = poll_timeout
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pubsub: Any | None = None

    def start(self, handler: EventHandler) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        pubsub = self._client.pubsub()
        pubsub.psubscribe(self._pattern)
        self._pubsub = pubsub

        def _pump() -> None:
            while not self._stop_event.is_set():
                try:
                    msg = pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=self._poll_timeout,
                    )
                except Exception:
                    # A transient SDK error shouldn't kill the worker.
                    # The Day-23 hardening pass adds backoff + structured
                    # logging; for Day 8 we simply continue.
                    continue
                if not msg:
                    continue
                data = msg.get("data")
                if data is None:
                    continue
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                try:
                    envelope = json.loads(data)
                except json.JSONDecodeError:
                    # Drop malformed messages — they cannot have come from
                    # our own publisher (which JSON-encodes via the bus).
                    # Day-23 quarantine queue lands here.
                    continue
                if not isinstance(envelope, dict):
                    continue
                try:
                    handler(envelope)
                except Exception:
                    # Handler bugs must not stop the listener — stay up
                    # so the next message has a chance to land.
                    continue

        self._thread = threading.Thread(
            target=_pump,
            name="orchestrator-event-listener",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_timeout * 4)
            self._thread = None
        if self._pubsub is not None:
            try:
                self._pubsub.punsubscribe(self._pattern)
            except Exception:
                pass
            try:
                self._pubsub.close()
            except Exception:
                pass
            self._pubsub = None


def make_buffered_handler(buffer: RecentEventsBuffer) -> EventHandler:
    """Compose the default Day-8 handler: append to the diagnostic buffer.

    Day 9 wraps this with the LLM planner; Day 10 wraps that with the
    policy + queue + audit pipeline. Keeping the Day-8 surface this small
    means tomorrow's planner work is purely additive.
    """

    def _handle(envelope: dict[str, Any]) -> None:
        buffer.append(envelope)

    return _handle
