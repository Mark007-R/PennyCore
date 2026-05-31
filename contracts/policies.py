"""Policy + Tenant + ApprovalRule contracts.

Mirrors `tenants`, `policies`, and `approval_queue` in
migrations/versions/0001_initial_schema.sql.

A `Policy` row is interpreted by whichever policy engine the orchestrator
is running (declarative YAML / Python rules / LLM-judge / naive). All four
engines read the same `body` JSONB; the engine-specific schema lives in
the engine module (orchestrator/policy/*.py). Phase 3 picks the champion.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from contracts.actions import ActionType


class PolicyDecision(str, Enum):
    AUTO = "auto"
    APPROVAL_REQUIRED = "approval_required"
    REJECT = "reject"


class Tenant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return v


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    action_type: ActionType
    decision: PolicyDecision
    body: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("created_at", "updated_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (UTC)")
        return v


class ApprovalRule(BaseModel):
    """A row in `approval_queue` — one pending action awaiting human review.

    ## N-of-M quorum (Day 26, Phase 5)

    A row can require multiple distinct approvers before it resolves.
    `required_approvals` (N) is the quorum threshold; `eligible_approvers`
    (the M pool) constrains *who* may vote (``None`` = any approver).
    `approvals` accumulates the distinct approver IDs that have voted so
    far — the row stays `pending` until ``len(approvals) >= required_approvals``,
    at which point the queue transitions it to `approved`.

    The default `required_approvals=1` / `eligible_approvers=None` reproduces
    the single-approver Day-10 behavior exactly, so existing callers (the
    takehome adapter, the Phase-2 pipeline) are unchanged.

    Rejection is a *veto*: a single eligible rejection resolves the row to
    `rejected` regardless of how many approvals have accumulated. This is
    the safe default for a compliance surface — one reviewer who spots a
    problem can stop the action even if others already signed off.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    action_id: str = Field(min_length=1, max_length=64)
    state: str = Field(default="pending")
    assigned_to: str | None = Field(default=None, max_length=128)
    required_approvals: int = Field(default=1, ge=1)
    eligible_approvers: list[str] | None = Field(default=None)
    approvals: list[str] = Field(default_factory=list)
    enqueued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None
    decided_by: str | None = Field(default=None, max_length=128)
    decision_note: str | None = Field(default=None, max_length=2048)
    row_version: int = Field(default=1, ge=1)

    @property
    def approvals_remaining(self) -> int:
        """How many more distinct approvals are needed to reach quorum.

        Clamped at zero so a resolved row never reports a negative
        remaining count."""
        return max(0, self.required_approvals - len(self.approvals))

    @field_validator("state")
    @classmethod
    def _check_state(cls, v: str) -> str:
        allowed = {"pending", "approved", "rejected", "expired"}
        if v not in allowed:
            raise ValueError(f"state must be one of {sorted(allowed)}")
        return v

    @field_validator("enqueued_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("enqueued_at must be timezone-aware (UTC)")
        return v

    @field_validator("decided_at")
    @classmethod
    def _ensure_aware_optional(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("decided_at must be timezone-aware (UTC)")
        return v
