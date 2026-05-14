"""Event bus port (Day 5, Phase 2; subscribe hook added Day 8).

Day 5 shipped the in-memory bus — sufficient for unit tests, the
TestClient-driven happy-path, and the Day-7 brief-assembler tests that
don't depend on the orchestrator running. Day 8 adds the Redis-backed
implementation in `context_engine/redis_event_bus.py`, plus an in-process
`subscribe(callback)` hook on the in-memory bus so the orchestrator's
`InMemoryEventListener` can attach without a network round-trip.

Both implementations satisfy the same `EventBus` Protocol so the ingestion
handler is bus-agnostic. The wire payload shape (a small dict of event
metadata, NOT the full Event) is locked down here as the contract every
subscriber decodes against.
"""

from __future__ import annotations

from threading import RLock
from typing import Any, Callable, Protocol, runtime_checkable

from contracts import Event

# Type alias for in-process subscribers: a callable that receives
# (channel, envelope). Redis subscribers go through the wire path and use
# the equivalent shape after JSON-decoding `data`.
EventBusSubscriber = Callable[[str, dict[str, Any]], None]

# Channel naming convention: `events.<tenant_id>` so subscribers can either
# listen to a single tenant or pattern-subscribe to `events.*`. This keeps
# the multi-tenant invariant (rule 15) visible at the wire level — a Redis
# subscription that forgets to scope by tenant_id is detectable in code review.
EVENT_CHANNEL_PREFIX = "events"


def channel_for(tenant_id: str) -> str:
    return f"{EVENT_CHANNEL_PREFIX}.{tenant_id}"


def envelope_for(event: Event) -> dict[str, Any]:
    """Build the on-the-wire envelope for an event.

    Deliberately small — orchestrator subscribers re-fetch the full event
    from the repository if they need the payload. This keeps the bus cheap
    and prevents accidental PII leakage through Redis logs.
    """
    return {
        "event_id": event.id,
        "tenant_id": event.tenant_id,
        "customer_id": event.customer_id,
        "channel_code": event.channel_code.value,
        "event_type": event.event_type.value,
        "received_at": event.received_at.isoformat(),
    }


@runtime_checkable
class EventBus(Protocol):
    def publish(self, channel: str, payload: dict[str, Any]) -> None: ...


class InMemoryEventBus:
    """Records every publish in an internal list AND fans out to subscribers.

    Use cases:
      * Unit tests: assert that ingestion published exactly once (`published`
        list).
      * Day 8 in-process orchestrator wiring: register a subscriber callback
        so the listener fires synchronously when ingestion publishes — no
        Redis required for the same-process happy-path test.
      * Day 11 end-to-end: both apps share one bus instance via the
        `attach_in_memory_bus` hook in the orchestrator's lifespan.

    Subscriber failures are isolated: if one subscriber raises, the other
    subscribers still run and the publish itself is treated as successful.
    The publisher must not see a downstream consumer's bug.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self.published: list[tuple[str, dict[str, Any]]] = []
        self._subscribers: list[EventBusSubscriber] = []

    def publish(self, channel: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.published.append((channel, payload))
            subscribers = list(self._subscribers)  # snapshot under lock
        for cb in subscribers:
            try:
                cb(channel, payload)
            except Exception:
                # Per the docstring: subscriber bugs do not poison the
                # publisher. The Day-23 hardening pass adds structured
                # logging here; for Day 8 we swallow silently.
                pass

    def subscribe(self, callback: EventBusSubscriber) -> None:
        """Register an in-process callback. Idempotent on identity — the
        same callback registered twice still runs once."""
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: EventBusSubscriber) -> None:
        with self._lock:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

    def clear(self) -> None:
        with self._lock:
            self.published.clear()
