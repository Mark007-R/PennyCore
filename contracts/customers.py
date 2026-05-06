"""Customer + CustomerIdentity contracts.

Mirrors the `customers` and `customer_identities` tables in
migrations/versions/0001_initial_schema.sql.

`CustomerIdentity` is the cross-channel linker: one customer can have many
identities (email, phone, external_id, chat_handle), and the UNIQUE
(tenant_id, identity_kind, identity_value) constraint is the join oracle
the linker walks on every ingest (SYSTEM_DESIGN §2.1, §6.1).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IdentityKind(str, Enum):
    EMAIL = "email"
    PHONE = "phone"
    EXTERNAL_ID = "external_id"
    CHAT_HANDLE = "chat_handle"


class Customer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=128)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (UTC)")
        return v


class CustomerIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    identity_kind: IdentityKind
    identity_value: str = Field(min_length=1, max_length=256)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("created_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return v

    @field_validator("identity_value")
    @classmethod
    def _normalize_value(cls, v: str, info) -> str:
        # Email and chat_handle: lowercase + strip. Phone/external_id: strip only
        # (phone normalization is the linker's job — keep contract honest).
        kind = info.data.get("identity_kind")
        v = v.strip()
        if not v:
            raise ValueError("identity_value cannot be blank after strip")
        if kind in (IdentityKind.EMAIL, IdentityKind.CHAT_HANDLE):
            v = v.lower()
        return v
