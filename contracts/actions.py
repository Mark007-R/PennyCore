"""Action + ActionProposal contracts.

Mirrors `action_proposals` and `actions` in
migrations/versions/0001_initial_schema.sql.

State machine for Action.status (enforced by the orchestrator's policy +
executor modules in Phase 2):

    pending_policy ─┬─▶ pending_exec      (policy=auto)
                    ├─▶ pending_approval  (policy=approval_required)
                    └─▶ rejected          (policy=reject)

    pending_approval ─┬─▶ pending_exec    (human approved)
                      └─▶ rejected        (human rejected)

    pending_exec ─┬─▶ executed
                  └─▶ execution_failed
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ActionType(str, Enum):
    """Open-ish enum: the demo's mortgage scenario uses these. New
    action_types are added here when they're added to the SQL CHECK constraint.
    Kept as a string enum (not free-form) so the policy table can join on it.
    """

    SEND_BORROWER_MESSAGE = "send_borrower_message"
    NOTIFY_LOAN_OFFICER = "notify_loan_officer"
    SCHEDULE_CALL = "schedule_call"
    REQUEST_DOCUMENT = "request_document"
    UPDATE_STATUS = "update_status"
    NO_OP = "no_op"


class ActionStatus(str, Enum):
    PENDING_POLICY = "pending_policy"
    PENDING_APPROVAL = "pending_approval"
    PENDING_EXEC = "pending_exec"
    EXECUTED = "executed"
    EXECUTION_FAILED = "execution_failed"
    REJECTED = "rejected"


class ProposedBy(str, Enum):
    LLM = "llm"
    FALLBACK = "fallback"


class ActionProposal(BaseModel):
    """The LLM's pre-policy proposal.

    `proposed_by=fallback` means the LLM call failed and the planner used
    its hardcoded rule table (SYSTEM_DESIGN §5.5). Audit log + downstream
    consumers can see the degradation explicitly.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    event_id: str = Field(min_length=1, max_length=64)
    customer_id: str | None = Field(default=None, max_length=64)
    action_type: ActionType
    proposed_by: ProposedBy
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("created_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        return v


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=64)
    proposal_id: str = Field(min_length=1, max_length=64)
    event_id: str = Field(min_length=1, max_length=64)
    customer_id: str | None = Field(default=None, max_length=64)
    action_type: ActionType
    status: ActionStatus
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    executed_at: datetime | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (UTC)")
        return v

    @field_validator("executed_at")
    @classmethod
    def _ensure_aware_optional(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("executed_at must be timezone-aware (UTC)")
        return v

    @model_validator(mode="after")
    def _executed_at_consistent(self) -> Action:
        if self.status == ActionStatus.EXECUTED and self.executed_at is None:
            raise ValueError("executed_at must be set when status=executed")
        if self.status != ActionStatus.EXECUTED and self.executed_at is not None:
            raise ValueError("executed_at must be None unless status=executed")
        return self
