"""Brief + BriefSegment contracts.

Mirrors the `briefs` table (cached briefs) in
migrations/versions/0001_initial_schema.sql, and gives the retrieval layer
its in-memory `BriefSegment` shape — the chunks that retrieval modules emit
and the brief assembler packs into a token-budgeted brief
(SYSTEM_DESIGN §6.1).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RetrievalStrategy(str, Enum):
    RECENCY = "recency"
    SEMANTIC = "semantic"
    SUMMARIZED = "summarized"
    HYBRID = "hybrid"


class SegmentSource(str, Enum):
    """What corner of the customer history this segment was pulled from.

    Recorded so the assembler can audit "30% of the brief came from semantic
    matches, 50% from recency, 20% from summary" — a key Phase 3 stat.
    """

    RECENT_MESSAGE = "recent_message"
    SEMANTIC_MATCH = "semantic_match"
    SUMMARY = "summary"
    PRIOR_ACTION = "prior_action"


class BriefSegment(BaseModel):
    """One chunk of brief content with metadata.

    Retrieval modules emit a list of these; the brief assembler sorts by
    `priority` (higher = more important) and greedily packs until the
    next segment would exceed `token_budget` (SYSTEM_DESIGN §5.4).
    """

    model_config = ConfigDict(extra="forbid")

    source: SegmentSource
    body: str = Field(min_length=1)
    token_estimate: int = Field(ge=0)
    priority: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class Brief(BaseModel):
    """A persisted assembled brief (cache row).

    Invariant: `token_count <= token_budget` always — the assembler's
    contract. Mirrored as a CHECK constraint in the DDL.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(min_length=1, max_length=64)
    query_hash: str = Field(min_length=1, max_length=128)
    token_budget: int = Field(gt=0)
    token_count: int = Field(ge=0)
    strategy: RetrievalStrategy
    body: str
    segments: list[BriefSegment] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None

    @field_validator("created_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return v

    @field_validator("expires_at")
    @classmethod
    def _ensure_aware_optional(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware (UTC)")
        return v

    @model_validator(mode="after")
    def _budget_honored(self) -> Brief:
        if self.token_count > self.token_budget:
            raise ValueError(
                f"token_count ({self.token_count}) exceeds token_budget "
                f"({self.token_budget}) — brief assembly invariant violated"
            )
        return self
