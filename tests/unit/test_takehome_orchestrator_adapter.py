"""Tests for the Day-10 takehome orchestrator adapter.

The adapter is the surface the external evaluator grades. These tests
exercise each `AgentOrchestrator` method through the adapter's async
interface — they are a Python-side reproduction of the evaluator's
scenarios, used to catch regressions without re-running the full
takehome harness in CI.

Coverage:
  1. handle_event returns valid action dicts (action_id/type/status).
  2. configure_policies + handle_event auto path → executed status.
  3. configure_policies + handle_event approval path → pending_approval.
  4. get_pending_actions returns adapter's tenant-scoped pending queue.
  5. approve_action transitions to executed.
  6. reject_action transitions to rejected.
  7. Double-approve raises.
  8. Duplicate event_id returns same action_ids.
  9. Multi-tenant isolation (action_ids disjoint).
 10. Approving alpha doesn't touch beta's queue.
 11. Action dict includes reasoning + audit_trail + approved_at after approve.
 12. Subclass of AgentOrchestrator (issubclass check the evaluator runs).
"""

from __future__ import annotations

import asyncio

import pytest

from takehome.orchestrator.orchestrator_impl import (
    AgentOrchestrator,
    PennyCoreOrchestrator,
)


def _run(coro):
    """Tiny sync wrapper so pytest doesn't need pytest-asyncio just for
    this module."""
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def orch() -> PennyCoreOrchestrator:
    # Each test gets a fresh adapter — same isolation guarantee the
    # evaluator's scenarios rely on (different tenant_id per scenario,
    # but a fresh adapter is even cleaner for unit tests).
    return PennyCoreOrchestrator()


def test_subclass_of_agent_orchestrator() -> None:
    """Evaluator runs `issubclass(cls, AgentOrchestrator)` — assert that
    invariant directly so the adapter's subclass relationship is part of
    the test surface."""
    assert issubclass(PennyCoreOrchestrator, AgentOrchestrator)


def test_handle_event_returns_valid_action_dict(orch: PennyCoreOrchestrator) -> None:
    actions = _run(
        orch.handle_event({
            "event_type": "document_uploaded",
            "tenant_id": "tenant-basic",
            "loan_id": "loan-001",
            "payload": {"document_type": "pay_stub"},
        })
    )
    assert isinstance(actions, list)
    assert len(actions) == 1
    a = actions[0]
    assert isinstance(a["action_id"], str) and a["action_id"]
    assert isinstance(a["action_type"], str) and a["action_type"]
    assert isinstance(a["status"], str) and a["status"]


def test_auto_policy_executes(orch: PennyCoreOrchestrator) -> None:
    _run(
        orch.configure_policies("tenant-auto", {"*": "auto", "default": "auto"})
    )
    actions = _run(
        orch.handle_event({
            "event_type": "message_received",
            "tenant_id": "tenant-auto",
            "payload": {"content": "hi"},
        })
    )
    assert actions[0]["status"] == "executed"
    assert actions[0]["executed_at"] is not None


def test_approval_policy_holds_pending(orch: PennyCoreOrchestrator) -> None:
    _run(orch.configure_policies("tenant-hold", {"*": "approval_required"}))
    actions = _run(
        orch.handle_event({
            "event_type": "message_received",
            "tenant_id": "tenant-hold",
            "payload": {},
        })
    )
    assert actions[0]["status"] == "pending_approval"
    pending = _run(orch.get_pending_actions("tenant-hold"))
    assert len(pending) == 1


def test_approve_action_transitions_to_executed(
    orch: PennyCoreOrchestrator,
) -> None:
    _run(orch.configure_policies("tenant-x", {"*": "approval_required"}))
    actions = _run(
        orch.handle_event({
            "event_type": "message_received",
            "tenant_id": "tenant-x",
            "payload": {},
        })
    )
    approved = _run(orch.approve_action(actions[0]["action_id"]))
    assert approved["status"] == "executed"
    assert approved["approved_at"] is not None
    # Pending queue empty after approve.
    assert _run(orch.get_pending_actions("tenant-x")) == []


def test_reject_action_transitions_to_rejected(
    orch: PennyCoreOrchestrator,
) -> None:
    _run(orch.configure_policies("tenant-x", {"*": "approval_required"}))
    actions = _run(
        orch.handle_event({
            "event_type": "message_received",
            "tenant_id": "tenant-x",
            "payload": {},
        })
    )
    rejected = _run(
        orch.reject_action(actions[0]["action_id"], reason="not appropriate")
    )
    assert rejected["status"] == "rejected"
    assert rejected["rejected_at"] is not None


def test_double_approve_raises(orch: PennyCoreOrchestrator) -> None:
    _run(orch.configure_policies("tenant-x", {"*": "approval_required"}))
    actions = _run(
        orch.handle_event({
            "event_type": "message_received",
            "tenant_id": "tenant-x",
            "payload": {},
        })
    )
    aid = actions[0]["action_id"]
    _run(orch.approve_action(aid))
    with pytest.raises(Exception):
        _run(orch.approve_action(aid))


def test_duplicate_event_id_returns_same_actions(
    orch: PennyCoreOrchestrator,
) -> None:
    _run(orch.configure_policies("tenant-d", {"*": "approval_required"}))
    event = {
        "event_id": "evt-dupe-001",
        "event_type": "condition_flagged",
        "tenant_id": "tenant-d",
        "payload": {"condition": "missing_w2"},
    }
    first = _run(orch.handle_event(event))
    second = _run(orch.handle_event(event))
    assert {a["action_id"] for a in first} == {a["action_id"] for a in second}
    # No duplicate in pending queue.
    pending = _run(orch.get_pending_actions("tenant-d"))
    pending_ids = [p["action_id"] for p in pending]
    assert len(pending_ids) == len(set(pending_ids))


def test_multi_tenant_isolation(orch: PennyCoreOrchestrator) -> None:
    _run(orch.configure_policies("alpha", {"*": "approval_required"}))
    _run(orch.configure_policies("beta", {"*": "approval_required"}))
    _run(
        orch.handle_event({
            "event_type": "document_uploaded",
            "tenant_id": "alpha",
            "payload": {},
        })
    )
    _run(
        orch.handle_event({
            "event_type": "message_received",
            "tenant_id": "beta",
            "payload": {},
        })
    )
    alpha = _run(orch.get_pending_actions("alpha"))
    beta = _run(orch.get_pending_actions("beta"))
    assert alpha and beta
    alpha_ids = {a["action_id"] for a in alpha}
    beta_ids = {b["action_id"] for b in beta}
    assert alpha_ids & beta_ids == set()
    # Approve alpha — beta unaffected.
    _run(orch.approve_action(next(iter(alpha_ids))))
    beta_after = _run(orch.get_pending_actions("beta"))
    assert len(beta_after) == len(beta)


def test_action_dict_includes_reasoning_and_audit_trail(
    orch: PennyCoreOrchestrator,
) -> None:
    _run(orch.configure_policies("tenant-audit", {"*": "approval_required"}))
    actions = _run(
        orch.handle_event({
            "event_type": "loan_status_changed",
            "tenant_id": "tenant-audit",
            "payload": {"old_status": "in_review", "new_status": "approved"},
        })
    )
    a = actions[0]
    assert isinstance(a.get("reasoning"), str) and len(a["reasoning"]) > 0
    # Pending action: audit_trail has proposal + decision rows.
    kinds = {e["kind"] for e in a["audit_trail"]}
    assert "proposal" in kinds
    assert "decision" in kinds

    approved = _run(orch.approve_action(a["action_id"]))
    approved_kinds = {e["kind"] for e in approved["audit_trail"]}
    assert "approval" in approved_kinds
    assert "execution" in approved_kinds
    assert approved.get("approved_at") is not None
    assert approved.get("updated_at") is not None


def test_handle_event_synthesizes_event_id_when_absent(
    orch: PennyCoreOrchestrator,
) -> None:
    """The evaluator's scenarios 1, 2, 3, 5, 6 don't supply event_id —
    the adapter must synthesize one so two consecutive calls produce
    two distinct actions (not deduped accidentally)."""
    _run(orch.configure_policies("tenant-s", {"*": "approval_required"}))
    a1 = _run(
        orch.handle_event({
            "event_type": "message_received",
            "tenant_id": "tenant-s",
            "payload": {},
        })
    )
    a2 = _run(
        orch.handle_event({
            "event_type": "message_received",
            "tenant_id": "tenant-s",
            "payload": {},
        })
    )
    assert a1[0]["action_id"] != a2[0]["action_id"]
    pending = _run(orch.get_pending_actions("tenant-s"))
    assert len(pending) == 2
