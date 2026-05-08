"""Event-store repository (Day 5, Phase 2).

Defines the `EventRepository` protocol that the ingestion handler depends
on, plus an in-memory implementation that satisfies it. The Postgres-backed
implementation lands Day 6+ when the linker needs the customer_identities
join (we wait until then to avoid building two DB layers); the protocol is
deliberately narrow today so swapping backends later is a one-line change in
`context_engine/api.py`.

The idempotency oracle is `(tenant_id, idempotency_key)`. The DDL in
`migrations/versions/0001_initial_schema.sql` enforces this with a UNIQUE
constraint (§5.1); the in-memory implementation enforces it with a dict
keyed on the same tuple. Both raise no exception on dup — they return
`(existing_event, created=False)` so the ingestion handler can return 200
(replay) vs 202 (created) without try/except plumbing.
"""

from __future__ import annotations

from threading import RLock
from typing import Protocol, runtime_checkable

from contracts import Event


@runtime_checkable
class EventRepository(Protocol):
    """Narrow port for event persistence. The Postgres adapter (Day 6+)
    satisfies this same shape; tests use the in-memory adapter below."""

    def upsert_event(self, event: Event) -> tuple[Event, bool]:
        """Insert `event` if `(tenant_id, idempotency_key)` is unseen.

        Returns `(stored_event, created)` where `created=True` means a new
        row was inserted and `created=False` means an existing row with the
        same idempotency key was returned unchanged (replay).
        """

    def get_event_by_idempotency(
        self, *, tenant_id: str, idempotency_key: str
    ) -> Event | None: ...

    def get_event_by_id(self, *, tenant_id: str, event_id: str) -> Event | None: ...

    def count(self, *, tenant_id: str | None = None) -> int: ...


class InMemoryEventRepository:
    """Process-local event store used by tests and `MOCK_LLM` mode.

    Thread-safe (FastAPI's TestClient runs handlers on a thread pool, and
    the load-test harness in Phase 4 hits the same instance concurrently).
    Multi-tenancy is enforced by tenant-scoped keys — a get with the wrong
    `tenant_id` cannot see another tenant's events even if the event_id is
    known.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._by_id: dict[tuple[str, str], Event] = {}  # (tenant_id, event_id) -> Event
        self._by_idem: dict[tuple[str, str], str] = {}  # (tenant_id, idem_key) -> event_id

    def upsert_event(self, event: Event) -> tuple[Event, bool]:
        with self._lock:
            idem_key = (event.tenant_id, event.idempotency_key)
            existing_event_id = self._by_idem.get(idem_key)
            if existing_event_id is not None:
                # Replay — return the original event unchanged. Returning the
                # stored row (not the inbound one) is deliberate: callers
                # treat the response as the canonical record, and we don't
                # want a replay to silently mutate stored payload bytes.
                stored = self._by_id[(event.tenant_id, existing_event_id)]
                return stored, False

            self._by_id[(event.tenant_id, event.id)] = event
            self._by_idem[idem_key] = event.id
            return event, True

    def get_event_by_idempotency(
        self, *, tenant_id: str, idempotency_key: str
    ) -> Event | None:
        with self._lock:
            event_id = self._by_idem.get((tenant_id, idempotency_key))
            if event_id is None:
                return None
            return self._by_id.get((tenant_id, event_id))

    def get_event_by_id(self, *, tenant_id: str, event_id: str) -> Event | None:
        with self._lock:
            return self._by_id.get((tenant_id, event_id))

    def count(self, *, tenant_id: str | None = None) -> int:
        with self._lock:
            if tenant_id is None:
                return len(self._by_id)
            return sum(1 for (t, _eid) in self._by_id if t == tenant_id)

    def clear(self) -> None:
        """Test-only helper. Not on the Protocol — production callers should
        not be able to wipe the store."""
        with self._lock:
            self._by_id.clear()
            self._by_idem.clear()
