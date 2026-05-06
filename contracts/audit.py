"""Audit log entry contract.

Mirrors the `audit_log` table in
migrations/versions/0001_initial_schema.sql.

Append-only at the database layer (trigger lands Day 4). At the contract
layer, the row is frozen — the audit log is immutable by construction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AuditKind(str, Enum):
    PROPOSAL = "proposal"
    DECISION = "decision"
    APPROVAL = "approval"
    REJECTION = "rejection"
    EXECUTION = "execution"
    EXECUTION_FAILED = "execution_failed"
    POLICY_CHANGE = "policy_change"


class AuditActorKind(str, Enum):
    SYSTEM = "system"
    LLM = "llm"
    HUMAN = "human"
    FALLBACK = "fallback"


class AuditLogEntry(BaseModel):
    """One row in audit_log. `id` is server-assigned (BIGSERIAL) so it's
    optional at construction time — repository.write() returns the row with
    `id` populated.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: int | None = Field(default=None, ge=1)
    tenant_id: str = Field(min_length=1, max_length=64)
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    kind: AuditKind
    action_id: str | None = Field(default=None, max_length=64)
    caused_by_event_id: str | None = Field(default=None, max_length=64)
    actor_kind: AuditActorKind
    actor_id: str | None = Field(default=None, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("ts")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("ts must be timezone-aware (UTC)")
        return v
