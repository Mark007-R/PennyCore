"""Event + Channel + Message contracts.

Mirrors the `events`, `channels`, and `messages` tables in
migrations/versions/0001_initial_schema.sql.

`Event` is the single inbound shape the context-engine accepts on
`POST /events`. `Message` is materialized from events at ingest time
when `event_type == 'message_received'` (so retrieval queries don't pay
the parsing cost — see SYSTEM_DESIGN §4 note).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChannelType(str, Enum):
    CHAT = "chat"
    EMAIL = "email"
    SMS = "sms"
    VOICE = "voice"
    API = "api"


class EventType(str, Enum):
    MESSAGE_RECEIVED = "message_received"
    DOCUMENT_UPLOADED = "document_uploaded"
    STATUS_CHANGED = "status_changed"
    ANOMALY_DETECTED = "anomaly_detected"
    SYSTEM_EVENT = "system_event"


class MessageDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Channel(BaseModel):
    """Lookup row from the `channels` table."""

    model_config = ConfigDict(frozen=True)

    code: ChannelType
    display_name: str = Field(min_length=1, max_length=64)


class Event(BaseModel):
    """A normalized inbound event from any channel.

    Idempotency: callers MUST supply `idempotency_key`. The DB UNIQUE on
    (tenant_id, idempotency_key) is the dedup oracle (SYSTEM_DESIGN §5.1).
    Linking happens AFTER validation but BEFORE acknowledgement, so
    `customer_id` is optional at construction and stamped on ingest.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    customer_id: str | None = Field(default=None, max_length=64)
    channel_code: ChannelType
    event_type: EventType
    idempotency_key: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("received_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("received_at must be timezone-aware (UTC)")
        return v


class Message(BaseModel):
    """Conversational text — the searchable subset of events.

    Materialized from `Event` at ingest time when the event carries text.
    `embedding` is optional at construction; populated lazily by the
    semantic-retrieval path in Phase 3.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    event_id: str = Field(min_length=1, max_length=64)
    channel_code: ChannelType
    direction: MessageDirection
    body: str = Field(min_length=1)
    sent_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    embedding: list[float] | None = Field(default=None, max_length=384)

    @field_validator("sent_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("sent_at must be timezone-aware (UTC)")
        return v
