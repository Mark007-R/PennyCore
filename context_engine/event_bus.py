"""Event bus port (Day 5, Phase 2).

Day 5 ships only the in-memory bus — sufficient for unit tests, the
TestClient-driven happy-path, and the Day-7 brief-assembler tests that
don't depend on the orchestrator running. The Redis-backed implementation
lands Day 8 when the orchestrator needs to subscribe (`event_listener.py`).

Both implementations satisfy the same `EventBus` Protocol so the ingestion
handler is bus-agnostic. The wire payload shape (a small dict of event
metadata, NOT the full Event) is locked down here as the contract Day 8
will subscribe against.
"""

from __future__ import annotations

from threading import RLock
from typing import Any, Protocol, runtime_checkable

from contracts import Event

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
    """Records every publish in an internal list.

    Use cases:
      * Unit tests: assert that ingestion published exactly once.
      * Day 8 development: drop in before Redis is wired so handlers can be
        exercised end-to-end on a single host.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, channel: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self.published.append((channel, payload))

    def clear(self) -> None:
        with self._lock:
            self.published.clear()
