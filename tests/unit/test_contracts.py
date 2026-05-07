"""Day 3 — contracts validation tests.

Goal: prove that every Pydantic model in contracts/ accepts a canonical
happy-path payload and rejects the obvious violations. These tests
double as live documentation of the field shapes.

Coverage target on contracts/*: 100% on model construction paths.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from contracts import (
    Action,
    ActionProposal,
    ActionStatus,
    ActionType,
    ApprovalRule,
    AuditActorKind,
    AuditKind,
    AuditLogEntry,
    Brief,
    BriefSegment,
    Channel,
    ChannelType,
    Customer,
    CustomerIdentity,
    Event,
    EventType,
    IdentityKind,
    Message,
    MessageDirection,
    Policy,
    PolicyDecision,
    ProposedBy,
    RetrievalStrategy,
    SegmentSource,
    Tenant,
)

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# events.py
# ---------------------------------------------------------------------------

class TestChannel:
    def test_happy_path(self) -> None:
        c = Channel(code=ChannelType.SMS, display_name="SMS")
        assert c.code == ChannelType.SMS
        assert c.display_name == "SMS"

    def test_frozen(self) -> None:
        c = Channel(code=ChannelType.EMAIL, display_name="Email")
        with pytest.raises(ValidationError):
            c.display_name = "Different"  # type: ignore[misc]


class TestEvent:
    def _kwargs(self, **overrides):
        base = dict(
            id="evt_01",
            tenant_id="acme-bank",
            channel_code=ChannelType.SMS,
            event_type=EventType.MESSAGE_RECEIVED,
            idempotency_key="msg-abc-123",
            payload={"text": "hello"},
            received_at=NOW,
        )
        base.update(overrides)
        return base

    def test_happy_path(self) -> None:
        e = Event(**self._kwargs())
        assert e.id == "evt_01"
        assert e.customer_id is None  # not yet linked

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Event(**self._kwargs(unknown_field="no"))

    def test_invalid_channel_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Event(**self._kwargs(channel_code="telegram"))

    def test_invalid_event_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Event(**self._kwargs(event_type="invented_event"))

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Event(**self._kwargs(received_at=datetime.now()))

    def test_idempotency_key_required(self) -> None:
        with pytest.raises(ValidationError):
            Event(**self._kwargs(idempotency_key=""))


class TestMessage:
    def _kwargs(self, **overrides):
        base = dict(
            id="msg_01",
            tenant_id="acme-bank",
            customer_id="cust-7",
            event_id="evt_01",
            channel_code=ChannelType.SMS,
            direction=MessageDirection.INBOUND,
            body="Hey, did my W-2 go through?",
            sent_at=NOW,
        )
        base.update(overrides)
        return base

    def test_happy_path_no_embedding(self) -> None:
        m = Message(**self._kwargs())
        assert m.embedding is None

    def test_happy_path_with_embedding(self) -> None:
        m = Message(**self._kwargs(embedding=[0.0] * 384))
        assert m.embedding is not None
        assert len(m.embedding) == 384

    def test_embedding_dim_capped(self) -> None:
        with pytest.raises(ValidationError):
            Message(**self._kwargs(embedding=[0.0] * 385))

    def test_blank_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Message(**self._kwargs(body=""))


# ---------------------------------------------------------------------------
# customers.py
# ---------------------------------------------------------------------------

class TestCustomer:
    def test_happy_path(self) -> None:
        c = Customer(id="cust-7", tenant_id="acme-bank", display_name="Jane Doe")
        assert c.metadata == {}


class TestCustomerIdentity:
    def test_happy_path(self) -> None:
        idn = CustomerIdentity(
            id="cid_01",
            tenant_id="acme-bank",
            customer_id="cust-7",
            identity_kind=IdentityKind.EMAIL,
            identity_value="Jane.Doe@example.com",
        )
        # Email gets lowercased.
        assert idn.identity_value == "jane.doe@example.com"

    def test_phone_not_lowercased(self) -> None:
        idn = CustomerIdentity(
            id="cid_02",
            tenant_id="acme-bank",
            customer_id="cust-7",
            identity_kind=IdentityKind.PHONE,
            identity_value="+1-555-0100",
        )
        assert idn.identity_value == "+1-555-0100"

    def test_blank_identity_value_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CustomerIdentity(
                id="cid_03",
                tenant_id="acme-bank",
                customer_id="cust-7",
                identity_kind=IdentityKind.EMAIL,
                identity_value="   ",
            )


# ---------------------------------------------------------------------------
# actions.py
# ---------------------------------------------------------------------------

class TestActionProposal:
    def test_happy_path_llm(self) -> None:
        p = ActionProposal(
            id="prop_01",
            tenant_id="acme-bank",
            event_id="evt_01",
            customer_id="cust-7",
            action_type=ActionType.SEND_BORROWER_MESSAGE,
            proposed_by=ProposedBy.LLM,
            payload={"body": "Yes, received Tuesday."},
        )
        assert p.proposed_by == ProposedBy.LLM

    def test_happy_path_fallback(self) -> None:
        p = ActionProposal(
            id="prop_02",
            tenant_id="acme-bank",
            event_id="evt_01",
            action_type=ActionType.NOTIFY_LOAN_OFFICER,
            proposed_by=ProposedBy.FALLBACK,
        )
        assert p.proposed_by == ProposedBy.FALLBACK


class TestAction:
    def _kwargs(self, **overrides):
        base = dict(
            id="act_01",
            tenant_id="acme-bank",
            proposal_id="prop_01",
            event_id="evt_01",
            customer_id="cust-7",
            action_type=ActionType.SEND_BORROWER_MESSAGE,
            status=ActionStatus.PENDING_POLICY,
        )
        base.update(overrides)
        return base

    def test_happy_path(self) -> None:
        a = Action(**self._kwargs())
        assert a.executed_at is None

    def test_executed_requires_executed_at(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Action(**self._kwargs(status=ActionStatus.EXECUTED))
        assert "executed_at must be set" in str(exc.value)

    def test_executed_at_without_status_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Action(**self._kwargs(executed_at=NOW))

    def test_executed_round_trip(self) -> None:
        a = Action(**self._kwargs(status=ActionStatus.EXECUTED, executed_at=NOW))
        assert a.executed_at == NOW


# ---------------------------------------------------------------------------
# policies.py
# ---------------------------------------------------------------------------

class TestTenant:
    def test_happy_path(self) -> None:
        t = Tenant(id="t_01", name="Acme Bank", slug="acme-bank")
        assert t.slug == "acme-bank"

    def test_invalid_slug_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Tenant(id="t_02", name="Bad Co", slug="Bad Slug!")


class TestPolicy:
    def test_happy_path(self) -> None:
        p = Policy(
            id="pol_01",
            tenant_id="acme-bank",
            action_type=ActionType.NOTIFY_LOAN_OFFICER,
            decision=PolicyDecision.APPROVAL_REQUIRED,
            body={"approvers": ["loan-officer-team"]},
        )
        assert p.decision == PolicyDecision.APPROVAL_REQUIRED

    def test_version_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            Policy(
                id="pol_02",
                tenant_id="acme-bank",
                action_type=ActionType.SEND_BORROWER_MESSAGE,
                decision=PolicyDecision.AUTO,
                version=0,
            )


class TestApprovalRule:
    def test_happy_path(self) -> None:
        r = ApprovalRule(
            id="aq_01",
            tenant_id="acme-bank",
            action_id="act_01",
        )
        assert r.state == "pending"

    def test_invalid_state_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ApprovalRule(
                id="aq_02",
                tenant_id="acme-bank",
                action_id="act_01",
                state="maybe",
            )


# ---------------------------------------------------------------------------
# audit.py
# ---------------------------------------------------------------------------

class TestAuditLogEntry:
    def test_happy_path(self) -> None:
        e = AuditLogEntry(
            tenant_id="acme-bank",
            kind=AuditKind.PROPOSAL,
            action_id="act_01",
            caused_by_event_id="evt_01",
            actor_kind=AuditActorKind.LLM,
            actor_id="claude-sonnet-4-6",
            payload={"action_type": "send_borrower_message"},
        )
        assert e.id is None  # server-assigned
        assert e.kind == AuditKind.PROPOSAL

    def test_frozen(self) -> None:
        e = AuditLogEntry(
            tenant_id="acme-bank",
            kind=AuditKind.EXECUTION,
            actor_kind=AuditActorKind.SYSTEM,
        )
        with pytest.raises(ValidationError):
            e.kind = AuditKind.REJECTION  # type: ignore[misc]

    def test_invalid_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AuditLogEntry(
                tenant_id="acme-bank",
                kind="reorg",
                actor_kind=AuditActorKind.SYSTEM,
            )


# ---------------------------------------------------------------------------
# briefs.py
# ---------------------------------------------------------------------------

class TestBriefSegment:
    def test_happy_path(self) -> None:
        s = BriefSegment(
            source=SegmentSource.RECENT_MESSAGE,
            body="Customer asked about W-2 status.",
            token_estimate=8,
            priority=0.9,
        )
        assert s.token_estimate == 8


class TestBrief:
    def _kwargs(self, **overrides):
        base = dict(
            id="brief_01",
            tenant_id="acme-bank",
            customer_id="cust-7",
            query_hash="sha256:" + "a" * 32,
            token_budget=8000,
            token_count=1234,
            strategy=RetrievalStrategy.HYBRID,
            body="Customer history brief...",
            segments=[
                BriefSegment(
                    source=SegmentSource.RECENT_MESSAGE,
                    body="Hey, did my W-2 go through?",
                    token_estimate=8,
                )
            ],
        )
        base.update(overrides)
        return base

    def test_happy_path(self) -> None:
        b = Brief(**self._kwargs())
        assert b.token_count <= b.token_budget

    def test_overspent_brief_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Brief(**self._kwargs(token_budget=100, token_count=200))
        assert "exceeds token_budget" in str(exc.value)

    def test_invalid_strategy_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Brief(**self._kwargs(strategy="vector_db_top_k"))

    def test_zero_budget_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Brief(**self._kwargs(token_budget=0, token_count=0))

    def test_expires_at_must_be_aware(self) -> None:
        with pytest.raises(ValidationError):
            Brief(**self._kwargs(expires_at=datetime.now() + timedelta(hours=1)))
