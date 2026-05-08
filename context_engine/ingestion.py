"""Event ingestion pipeline (Day 5, Phase 2).

Two functions live here:

  * `IngestionRequest` — the inbound DTO clients POST to `/events`. It is
    deliberately narrower than the `Event` contract: server stamps `id`
    and `received_at`, so callers don't get to forge them.

  * `ingest_event` — the pure business logic. Takes a constructed `Event`
    plus a repository and a bus, performs the idempotent upsert, and emits
    a publish on the bus iff the event was newly created. This is the
    function tests should target directly when they don't need the HTTP
    surface.

Multi-tenant invariant (rule 15): every `Event` carries `tenant_id`, every
repository call is tenant-scoped, every bus channel is tenant-scoped. There
is no cross-tenant code path through this module by construction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from context_engine.event_bus import EventBus, channel_for, envelope_for
from context_engine.repository import EventRepository
from contracts import ChannelType, Event, EventType


def new_event_id() -> str:
    """ULID-shaped event id. Sortable enough for log debugging; unique
    enough for a 35-day project. Real ULIDs land if/when we adopt the
    `python-ulid` package post-MVP."""
    return f"evt_{uuid.uuid4().hex[:24]}"


class IngestionRequest(BaseModel):
    """Inbound shape for `POST /events`.

    Strict: extra fields rejected (`extra='forbid'`) so a future client
    sending a typo'd field gets a 422 immediately rather than silently
    dropping data into the void. Idempotency key may also be supplied via
    the `X-Idempotency-Key` header — the API layer reconciles the two.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=64)
    customer_id: str | None = Field(default=None, max_length=64)
    channel_code: ChannelType
    event_type: EventType
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class IngestionResult:
    event: Event
    created: bool  # False = idempotent replay; same key seen before


def build_event_from_request(
    request: IngestionRequest,
    *,
    idempotency_key: str,
) -> Event:
    """Construct the canonical `Event` from a request + resolved idempotency key.

    Server-stamped fields (`id`, `received_at`) are set here, NEVER trusted
    from the client. This is the only function in the codebase that
    fabricates event ids — keep it that way.
    """
    return Event(
        id=new_event_id(),
        tenant_id=request.tenant_id,
        customer_id=request.customer_id,
        channel_code=request.channel_code,
        event_type=request.event_type,
        idempotency_key=idempotency_key,
        payload=request.payload,
        received_at=datetime.now(timezone.utc),
    )


def ingest_event(
    event: Event,
    *,
    repo: EventRepository,
    bus: EventBus,
) -> IngestionResult:
    """Persist + announce. The two-step:

    1. `repo.upsert_event` — idempotent insert keyed on
       `(tenant_id, idempotency_key)`. Returns the canonical stored event
       and a `created` flag.
    2. If `created`, publish a small envelope on
       `events.<tenant_id>`. Replays don't republish — orchestrator
       subscribers must not see the same event id twice (audit invariant,
       rule 16).

    Failure semantics: if the bus publish raises, the event is already
    stored. Day-23 hardening adds a quarantine / outbox pattern; for Day-5
    MVP we accept that a bus failure shows up as an inconsistency the
    Day-19 idempotency tests will exercise (re-POSTing the same key
    re-publishes nothing because the event is already stored).
    """
    stored, created = repo.upsert_event(event)
    if created:
        bus.publish(channel_for(stored.tenant_id), envelope_for(stored))
    return IngestionResult(event=stored, created=created)
