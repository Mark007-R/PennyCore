"""Tests for the Day-10 declarative policy engine.

Coverage:
  1. Exact action_type lookup.
  2. Wildcard `*` fallback.
  3. `default` fallback.
  4. Unknown action_type, no wildcard → safe default = approval_required.
  5. Unknown tenant → safe default.
  6. Both ActionType enum and raw string lookup.
  7. Tenant isolation (configuring tenant A doesn't reach tenant B).
  8. Re-set replaces (not merges) the tenant's table.
  9. Invalid decision string raises.
 10. Non-string decision value raises.
 11. Empty tenant_id rejected on set_policies.
 12. Introspection helpers (`tenants`, `policies_for`, `clear`).
"""

from __future__ import annotations

import pytest

from contracts.actions import ActionType
from contracts.policies import PolicyDecision
from orchestrator.policy import (
    DeclarativePolicyEngine,
    UnknownPolicyDecisionError,
)


def test_exact_action_type_lookup() -> None:
    engine = DeclarativePolicyEngine()
    engine.set_policies(
        "tenant-a",
        {"send_borrower_message": "auto", "notify_loan_officer": "approval_required"},
    )
    assert engine.decide("tenant-a", "send_borrower_message") is PolicyDecision.AUTO
    assert (
        engine.decide("tenant-a", "notify_loan_officer")
        is PolicyDecision.APPROVAL_REQUIRED
    )


def test_wildcard_fallback() -> None:
    engine = DeclarativePolicyEngine()
    engine.set_policies("tenant-strict", {"*": "approval_required"})
    # Any action_type lands on the wildcard.
    assert (
        engine.decide("tenant-strict", "send_borrower_message")
        is PolicyDecision.APPROVAL_REQUIRED
    )
    assert (
        engine.decide("tenant-strict", "notify_loan_officer")
        is PolicyDecision.APPROVAL_REQUIRED
    )
    assert (
        engine.decide("tenant-strict", "no_op")
        is PolicyDecision.APPROVAL_REQUIRED
    )


def test_default_fallback() -> None:
    engine = DeclarativePolicyEngine()
    engine.set_policies(
        "tenant-mix",
        {"send_borrower_message": "auto", "default": "approval_required"},
    )
    assert engine.decide("tenant-mix", "send_borrower_message") is PolicyDecision.AUTO
    # Anything else takes default.
    assert engine.decide("tenant-mix", "schedule_call") is PolicyDecision.APPROVAL_REQUIRED


def test_exact_beats_default_beats_wildcard() -> None:
    """Resolution order: exact match wins, then `default`, then `*`."""
    engine = DeclarativePolicyEngine()
    engine.set_policies(
        "tenant-order",
        {
            "send_borrower_message": "auto",
            "default": "approval_required",
            "*": "reject",
        },
    )
    assert engine.decide("tenant-order", "send_borrower_message") is PolicyDecision.AUTO
    assert engine.decide("tenant-order", "no_op") is PolicyDecision.APPROVAL_REQUIRED
    # If we drop default, * should win.
    engine.set_policies(
        "tenant-order2",
        {"send_borrower_message": "auto", "*": "reject"},
    )
    assert engine.decide("tenant-order2", "no_op") is PolicyDecision.REJECT


def test_unknown_action_type_safe_default() -> None:
    engine = DeclarativePolicyEngine()
    engine.set_policies("tenant-bare", {"send_borrower_message": "auto"})
    # No default, no *, action_type not in table → approval_required.
    assert (
        engine.decide("tenant-bare", "anything_else") is PolicyDecision.APPROVAL_REQUIRED
    )


def test_unknown_tenant_safe_default() -> None:
    engine = DeclarativePolicyEngine()
    # No tenant configured at all → safe default.
    assert (
        engine.decide("unknown-tenant", ActionType.SEND_BORROWER_MESSAGE)
        is PolicyDecision.APPROVAL_REQUIRED
    )


def test_action_type_enum_or_string_both_work() -> None:
    engine = DeclarativePolicyEngine()
    engine.set_policies("tenant-x", {"send_borrower_message": "auto"})
    assert engine.decide("tenant-x", "send_borrower_message") is PolicyDecision.AUTO
    assert (
        engine.decide("tenant-x", ActionType.SEND_BORROWER_MESSAGE)
        is PolicyDecision.AUTO
    )


def test_tenant_isolation() -> None:
    engine = DeclarativePolicyEngine()
    engine.set_policies("alpha", {"send_borrower_message": "auto"})
    engine.set_policies(
        "beta", {"send_borrower_message": "approval_required"}
    )
    assert engine.decide("alpha", "send_borrower_message") is PolicyDecision.AUTO
    assert (
        engine.decide("beta", "send_borrower_message")
        is PolicyDecision.APPROVAL_REQUIRED
    )


def test_set_policies_replaces_not_merges() -> None:
    engine = DeclarativePolicyEngine()
    engine.set_policies(
        "tenant-r",
        {"send_borrower_message": "auto", "notify_loan_officer": "auto"},
    )
    # Replace with a smaller table — old entries must vanish.
    engine.set_policies("tenant-r", {"send_borrower_message": "approval_required"})
    assert (
        engine.decide("tenant-r", "send_borrower_message")
        is PolicyDecision.APPROVAL_REQUIRED
    )
    # The previously-configured `notify_loan_officer` is gone → safe default.
    assert (
        engine.decide("tenant-r", "notify_loan_officer")
        is PolicyDecision.APPROVAL_REQUIRED
    )


def test_unknown_decision_value_raises() -> None:
    engine = DeclarativePolicyEngine()
    with pytest.raises(UnknownPolicyDecisionError, match="auto-approve"):
        engine.set_policies("t", {"send_borrower_message": "auto-approve"})


def test_non_string_decision_raises() -> None:
    engine = DeclarativePolicyEngine()
    with pytest.raises(UnknownPolicyDecisionError, match="must be a string"):
        engine.set_policies("t", {"send_borrower_message": 1})  # type: ignore[dict-item]


def test_empty_tenant_id_rejected() -> None:
    engine = DeclarativePolicyEngine()
    with pytest.raises(ValueError, match="tenant_id"):
        engine.set_policies("", {"send_borrower_message": "auto"})


def test_introspection_helpers() -> None:
    engine = DeclarativePolicyEngine()
    assert engine.tenants() == []
    engine.set_policies("alpha", {"send_borrower_message": "auto"})
    engine.set_policies("beta", {"notify_loan_officer": "reject"})
    assert engine.tenants() == ["alpha", "beta"]
    assert engine.policies_for("alpha") == {"send_borrower_message": "auto"}
    assert engine.policies_for("beta") == {"notify_loan_officer": "reject"}
    assert engine.policies_for("unknown") == {}
    engine.clear()
    assert engine.tenants() == []
