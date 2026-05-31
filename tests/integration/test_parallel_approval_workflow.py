"""End-to-end parallel-approval (N-of-M) workflow tests (Day 26, Phase 5).

These drive the full DecisionPipeline — proposal → policy(approval_required)
→ quorum-armed queue row → multiple approve/reject votes → execution + audit
— rather than the queue in isolation (`test_quorum_approval_queue.py`).

The pipeline is constructed via `make_default_pipeline()` and the tenant's
quorum is configured through `pipeline.quorum.set_rule(...)`, which is the
same surface the admin UI / config layer uses.

Coverage:
  1. 2-of-3 happy path: first approval keeps the action pending, the second
     executes it; audit trail shows both votes + execution.
  2. Each partial vote writes an APPROVAL audit row tagging quorum progress.
  3. A single rejection vetoes a partially-approved action.
  4. An ineligible approver is refused and the action stays pending.
  5. The same approver cannot fill two quorum seats.
  6. A tenant with no quorum rule keeps the single-approver behavior.
  7. approval_progress (via approval_rule) reflects the live tally.
  8. Quorum approvals stay tenant-isolated.
"""

from __future__ import annotations

import pytest

from contracts.actions import (
    ActionProposal,
    ActionStatus,
    ActionType,
    ProposedBy,
)
from contracts.audit import AuditKind
from orchestrator.approval_queue import (
    ApproverNotEligibleError,
    DuplicateApproverError,
)
from orchestrator.decision_pipeline import make_default_pipeline


def _proposal(
    *,
    tenant_id: str = "tenant-a",
    event_id: str = "evt_001",
    proposal_id: str = "prop_001",
    action_type: ActionType = ActionType.SEND_BORROWER_MESSAGE,
) -> ActionProposal:
    return ActionProposal(
        id=proposal_id,
        tenant_id=tenant_id,
        event_id=event_id,
        customer_id="cust_1",
        action_type=action_type,
        proposed_by=ProposedBy.LLM,
        payload={"_planner_reasoning": "needs review"},
    )


def _pipeline_requiring(tenant_id: str, **quorum_kwargs):
    """A pipeline whose `tenant_id` routes the demo action_type to
    approval_required and arms it with the supplied quorum."""
    pipe = make_default_pipeline()
    pipe.policy.set_policies(tenant_id, {"*": "approval_required"})
    if quorum_kwargs:
        pipe.quorum.set_rule(
            tenant_id, ActionType.SEND_BORROWER_MESSAGE, **quorum_kwargs
        )
    return pipe


def test_two_of_three_happy_path_executes_on_second_vote() -> None:
    pipe = _pipeline_requiring("tenant-a", required_approvals=2)
    [action] = pipe.handle_proposal(_proposal())
    assert action.status is ActionStatus.PENDING_APPROVAL

    after_first = pipe.approve_action(action.id, decided_by="alice")
    assert after_first.status is ActionStatus.PENDING_APPROVAL  # still pending
    assert pipe.approval_rule(action.id).approvals == ["alice"]

    after_second = pipe.approve_action(action.id, decided_by="bob")
    assert after_second.status is ActionStatus.EXECUTED
    assert after_second.executed_at is not None


def test_each_partial_vote_writes_audit_with_progress() -> None:
    pipe = _pipeline_requiring("tenant-a", required_approvals=2)
    [action] = pipe.handle_proposal(_proposal())

    pipe.approve_action(action.id, decided_by="alice")
    pipe.approve_action(action.id, decided_by="bob")

    approvals = [
        e for e in pipe.audit_for_action(action.id) if e.kind is AuditKind.APPROVAL
    ]
    assert len(approvals) == 2
    assert approvals[0].actor_id == "alice"
    assert approvals[0].payload["quorum_reached"] is False
    assert approvals[0].payload["approvals_recorded"] == 1
    assert approvals[1].actor_id == "bob"
    assert approvals[1].payload["quorum_reached"] is True
    # Execution audit only after quorum.
    executions = [
        e for e in pipe.audit_for_action(action.id) if e.kind is AuditKind.EXECUTION
    ]
    assert len(executions) == 1


def test_single_rejection_vetoes_partial_approval() -> None:
    pipe = _pipeline_requiring("tenant-a", required_approvals=3)
    [action] = pipe.handle_proposal(_proposal())

    pipe.approve_action(action.id, decided_by="alice")
    rejected = pipe.reject_action(action.id, reason="fraud risk", decided_by="bob")
    assert rejected.status is ActionStatus.REJECTED

    kinds = [e.kind for e in pipe.audit_for_action(action.id)]
    assert AuditKind.REJECTION in kinds
    assert AuditKind.EXECUTION not in kinds


def test_ineligible_approver_refused_action_stays_pending() -> None:
    pipe = _pipeline_requiring(
        "tenant-a",
        required_approvals=2,
        eligible_approvers=["alice", "bob", "carol"],
    )
    [action] = pipe.handle_proposal(_proposal())

    with pytest.raises(ApproverNotEligibleError):
        pipe.approve_action(action.id, decided_by="dave")
    assert pipe.get_action(action.id).status is ActionStatus.PENDING_APPROVAL
    assert pipe.approval_rule(action.id).approvals == []


def test_same_approver_cannot_fill_two_seats() -> None:
    pipe = _pipeline_requiring("tenant-a", required_approvals=2)
    [action] = pipe.handle_proposal(_proposal())

    pipe.approve_action(action.id, decided_by="alice")
    with pytest.raises(DuplicateApproverError):
        pipe.approve_action(action.id, decided_by="alice")
    assert pipe.get_action(action.id).status is ActionStatus.PENDING_APPROVAL
    assert pipe.approval_rule(action.id).approvals == ["alice"]


def test_no_quorum_rule_keeps_single_approver_behavior() -> None:
    # No quorum kwargs → default single-approver.
    pipe = _pipeline_requiring("tenant-a")
    [action] = pipe.handle_proposal(_proposal())
    executed = pipe.approve_action(action.id, decided_by="solo")
    assert executed.status is ActionStatus.EXECUTED


def test_approval_progress_reflects_live_tally() -> None:
    pipe = _pipeline_requiring(
        "tenant-a", required_approvals=3, eligible_approvers=["a", "b", "c"]
    )
    [action] = pipe.handle_proposal(_proposal())
    rule0 = pipe.approval_rule(action.id)
    assert rule0.required_approvals == 3
    assert rule0.approvals_remaining == 3
    assert rule0.eligible_approvers == ["a", "b", "c"]

    pipe.approve_action(action.id, decided_by="a")
    assert pipe.approval_rule(action.id).approvals_remaining == 2


def test_quorum_approvals_stay_tenant_isolated() -> None:
    pipe = make_default_pipeline()
    for t in ("tenant-x", "tenant-y"):
        pipe.policy.set_policies(t, {"*": "approval_required"})
        pipe.quorum.set_rule(
            t, ActionType.SEND_BORROWER_MESSAGE, required_approvals=2
        )

    [ax] = pipe.handle_proposal(_proposal(tenant_id="tenant-x", event_id="ex"))
    [ay] = pipe.handle_proposal(_proposal(tenant_id="tenant-y", event_id="ey"))

    # Completing quorum on X must not touch Y.
    pipe.approve_action(ax.id, decided_by="x1")
    pipe.approve_action(ax.id, decided_by="x2")
    assert pipe.get_action(ax.id).status is ActionStatus.EXECUTED
    assert pipe.get_action(ay.id).status is ActionStatus.PENDING_APPROVAL
    assert [a.id for a in pipe.list_pending_actions("tenant-y")] == [ay.id]
    assert pipe.list_pending_actions("tenant-x") == []
