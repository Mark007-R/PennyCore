"""Tests for the Day-17 PythonRulesPolicyEngine.

Mirrors the declarative-engine coverage so any divergence shows up.
The two engines deliberately share resolution semantics — this suite
asserts the contract holds.
"""

from __future__ import annotations

import pytest

from contracts.actions import ActionType
from contracts.policies import PolicyDecision
from orchestrator.policy import PythonRulesPolicyEngine


def test_exact_action_type_lookup() -> None:
    engine = PythonRulesPolicyEngine()
    engine.set_policies(
        "tenant-a",
        {
            "send_borrower_message": "auto",
            "notify_loan_officer": "approval_required",
        },
    )
    assert engine.decide("tenant-a", "send_borrower_message") is PolicyDecision.AUTO
    assert (
        engine.decide("tenant-a", "notify_loan_officer")
        is PolicyDecision.APPROVAL_REQUIRED
    )


def test_default_fallback() -> None:
    engine = PythonRulesPolicyEngine()
    engine.set_policies(
        "tenant-mix",
        {"send_borrower_message": "auto", "default": "approval_required"},
    )
    assert engine.decide("tenant-mix", "send_borrower_message") is PolicyDecision.AUTO
    assert (
        engine.decide("tenant-mix", "schedule_call")
        is PolicyDecision.APPROVAL_REQUIRED
    )


def test_wildcard_fallback() -> None:
    engine = PythonRulesPolicyEngine()
    engine.set_policies("tenant-strict", {"*": "reject"})
    assert engine.decide("tenant-strict", "no_op") is PolicyDecision.REJECT
    assert (
        engine.decide("tenant-strict", "send_borrower_message")
        is PolicyDecision.REJECT
    )


def test_unknown_tenant_safe_default() -> None:
    engine = PythonRulesPolicyEngine()
    # No tenant configured anywhere.
    assert (
        engine.decide("ghost-tenant", "send_borrower_message")
        is PolicyDecision.APPROVAL_REQUIRED
    )


def test_actiontype_enum_lookup() -> None:
    """Both ActionType.X and raw string keys must resolve identically."""
    engine = PythonRulesPolicyEngine()
    engine.set_policies(
        "tenant-x", {"send_borrower_message": "auto", "default": "reject"}
    )
    assert (
        engine.decide("tenant-x", ActionType.SEND_BORROWER_MESSAGE)
        is PolicyDecision.AUTO
    )
    assert engine.decide("tenant-x", ActionType.NO_OP) is PolicyDecision.REJECT


def test_tenant_isolation() -> None:
    engine = PythonRulesPolicyEngine()
    engine.set_policies("tenant-a", {"send_borrower_message": "auto"})
    engine.set_policies(
        "tenant-b", {"send_borrower_message": "approval_required"}
    )
    assert engine.decide("tenant-a", "send_borrower_message") is PolicyDecision.AUTO
    assert (
        engine.decide("tenant-b", "send_borrower_message")
        is PolicyDecision.APPROVAL_REQUIRED
    )


def test_reset_replaces_table() -> None:
    engine = PythonRulesPolicyEngine()
    engine.set_policies(
        "tenant-a", {"send_borrower_message": "auto", "notify_loan_officer": "reject"}
    )
    # Re-set with a narrower table; old keys must NOT survive.
    engine.set_policies("tenant-a", {"send_borrower_message": "approval_required"})
    assert (
        engine.decide("tenant-a", "send_borrower_message")
        is PolicyDecision.APPROVAL_REQUIRED
    )
    # notify_loan_officer falls through to the safe default now.
    assert (
        engine.decide("tenant-a", "notify_loan_officer")
        is PolicyDecision.APPROVAL_REQUIRED
    )


def test_invalid_decision_raises() -> None:
    engine = PythonRulesPolicyEngine()
    with pytest.raises(Exception):  # noqa: PT011 — surfaces ValueError or subclass
        engine.set_policies("tenant-a", {"send_borrower_message": "auto-approve"})


def test_introspection_helpers() -> None:
    engine = PythonRulesPolicyEngine()
    engine.set_policies("tenant-a", {"send_borrower_message": "auto"})
    engine.set_policies("tenant-b", {"*": "reject"})
    assert engine.tenants() == ["tenant-a", "tenant-b"]
    assert engine.policies_for("tenant-a") == {"send_borrower_message": "auto"}
    assert engine.policies_for("unknown") == {}
    engine.clear()
    assert engine.tenants() == []


def test_event_context_passed_but_ignored_day17() -> None:
    """Day 17's python_rules engine accepts but ignores event_context.

    A future per-event-time rule could read it; today the contract is
    "the engine takes the kwarg without erroring, decision depends on
    action_type only".
    """
    engine = PythonRulesPolicyEngine()
    engine.set_policies("tenant-a", {"send_borrower_message": "auto"})
    decision_a = engine.decide(
        "tenant-a",
        "send_borrower_message",
        event_context={"event_type": "message_received"},
    )
    decision_b = engine.decide(
        "tenant-a",
        "send_borrower_message",
        event_context={"event_type": "system_event"},
    )
    assert decision_a is decision_b is PolicyDecision.AUTO
