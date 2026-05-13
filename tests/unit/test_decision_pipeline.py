"""Tests for the Day-10 decision pipeline.

The pipeline composes planner → policy → queue/executor → audit. These
tests construct a pipeline + proposal directly (no LLM, no listener)
to exercise the decision logic in isolation. Listener integration is
covered by `test_orchestrator_pipeline_handler.py`.

Coverage:
  1. AUTO decision: proposal → action(status=executed), audit rows
     present for PROPOSAL + DECISION + EXECUTION.
  2. APPROVAL_REQUIRED decision: proposal → action(pending_approval),
     queue row created, no execution yet.
  3. REJECT decision: proposal → action(rejected), no queue row.
  4. approve_action transitions pending_approval → executed and writes
     APPROVAL + EXECUTION audit rows.
  5. reject_action transitions pending_approval → rejected and writes
     REJECTION audit row.
  6. Double-approve raises ApprovalStateError.
  7. Approving a never-queued action raises ApprovalNotFoundError.
  8. Idempotency: handle_proposal twice with same event_id returns the
     same action list.
  9. Tenant isolation: action IDs and queue listings stay disjoint.
 10. Executor failure → action moves to execution_failed, audit row
     EXECUTION_FAILED written, no exception leaks.
 11. Pipeline.clear resets every backend.
 12. list_pending_actions returns the canonical Action rows in FIFO.
"""

from __future__ import annotations

from typing import Any

import pytest

from contracts.actions import (
    ActionProposal,
    ActionStatus,
    ActionType,
    ProposedBy,
)
from contracts.audit import AuditKind
from orchestrator.approval_queue import (
    ApprovalNotFoundError,
    ApprovalStateError,
    InMemoryApprovalQueue,
)
from orchestrator.audit import InMemoryAuditLog
from orchestrator.decision_pipeline import (
    ActionNotFoundError,
    DecisionPipeline,
    make_default_pipeline,
)
from orchestrator.executor import ActionExecutor, DEFAULT_EXECUTORS
from orchestrator.policy import DeclarativePolicyEngine


def _proposal(
    *,
    tenant_id: str = "tenant-a",
    event_id: str = "evt_001",
    action_type: ActionType = ActionType.SEND_BORROWER_MESSAGE,
    proposal_id: str = "prop_001",
    reasoning: str = "test reasoning",
    proposed_by: ProposedBy = ProposedBy.LLM,
) -> ActionProposal:
    return ActionProposal(
        id=proposal_id,
        tenant_id=tenant_id,
        event_id=event_id,
        customer_id=None,
        action_type=action_type,
        proposed_by=proposed_by,
        payload={"_planner_reasoning": reasoning},
    )


# ----------------------------------------------------------------------------
# AUTO path
# ----------------------------------------------------------------------------


def test_auto_decision_executes_immediately() -> None:
    pipeline = make_default_pipeline()
    pipeline.policy.set_policies("tenant-a", {"*": "auto"})
    actions = pipeline.handle_proposal(_proposal())
    assert len(actions) == 1
    a = actions[0]
    assert a.status is ActionStatus.EXECUTED
    assert a.executed_at is not None
    # Audit kinds emitted: PROPOSAL, DECISION, EXECUTION.
    kinds = [e.kind for e in pipeline.audit.entries_for_action(a.id)]
    assert AuditKind.PROPOSAL in kinds
    assert AuditKind.DECISION in kinds
    assert AuditKind.EXECUTION in kinds


# ----------------------------------------------------------------------------
# APPROVAL_REQUIRED path
# ----------------------------------------------------------------------------


def test_approval_required_enqueues_and_waits() -> None:
    pipeline = make_default_pipeline()
    pipeline.policy.set_policies("tenant-a", {"*": "approval_required"})
    actions = pipeline.handle_proposal(_proposal())
    a = actions[0]
    assert a.status is ActionStatus.PENDING_APPROVAL
    assert a.executed_at is None
    # Queue row exists.
    rule = pipeline.queue.get(a.id)
    assert rule is not None
    assert rule.state == "pending"
    # No EXECUTION audit row yet.
    kinds = [e.kind for e in pipeline.audit.entries_for_action(a.id)]
    assert AuditKind.EXECUTION not in kinds


def test_approve_action_executes_and_writes_audit() -> None:
    pipeline = make_default_pipeline()
    pipeline.policy.set_policies("tenant-a", {"*": "approval_required"})
    [a] = pipeline.handle_proposal(_proposal())
    executed = pipeline.approve_action(a.id, decided_by="reviewer-1")
    assert executed.status is ActionStatus.EXECUTED
    assert executed.id == a.id
    kinds = [e.kind for e in pipeline.audit.entries_for_action(a.id)]
    assert AuditKind.APPROVAL in kinds
    assert AuditKind.EXECUTION in kinds


def test_reject_action_writes_audit_and_marks_rejected() -> None:
    pipeline = make_default_pipeline()
    pipeline.policy.set_policies("tenant-a", {"*": "approval_required"})
    [a] = pipeline.handle_proposal(_proposal())
    rejected = pipeline.reject_action(a.id, reason="not appropriate")
    assert rejected.status is ActionStatus.REJECTED
    kinds = [e.kind for e in pipeline.audit.entries_for_action(a.id)]
    assert AuditKind.REJECTION in kinds


def test_double_approve_raises() -> None:
    pipeline = make_default_pipeline()
    pipeline.policy.set_policies("tenant-a", {"*": "approval_required"})
    [a] = pipeline.handle_proposal(_proposal())
    pipeline.approve_action(a.id)
    with pytest.raises(ApprovalStateError):
        pipeline.approve_action(a.id)


def test_approve_never_queued_raises_not_found() -> None:
    pipeline = make_default_pipeline()
    pipeline.policy.set_policies("tenant-a", {"*": "auto"})
    [a] = pipeline.handle_proposal(_proposal())
    # Auto path means no queue row was ever created.
    with pytest.raises(ApprovalNotFoundError):
        pipeline.approve_action(a.id)


def test_approve_unknown_action_id_raises_not_found() -> None:
    pipeline = make_default_pipeline()
    with pytest.raises(ActionNotFoundError):
        pipeline.approve_action("act_missing")


# ----------------------------------------------------------------------------
# REJECT path
# ----------------------------------------------------------------------------


def test_reject_decision_marks_action_rejected_no_queue() -> None:
    pipeline = make_default_pipeline()
    pipeline.policy.set_policies("tenant-a", {"*": "reject"})
    [a] = pipeline.handle_proposal(_proposal())
    assert a.status is ActionStatus.REJECTED
    # No queue row (reject at policy time skips the queue entirely).
    assert pipeline.queue.get(a.id) is None
    # Audit shows a REJECTION row from the system actor.
    kinds = [e.kind for e in pipeline.audit.entries_for_action(a.id)]
    assert AuditKind.REJECTION in kinds


# ----------------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------------


def test_duplicate_event_returns_same_actions() -> None:
    pipeline = make_default_pipeline()
    pipeline.policy.set_policies("tenant-a", {"*": "approval_required"})
    p = _proposal(event_id="evt_dupe")
    first = pipeline.handle_proposal(p)
    second = pipeline.handle_proposal(p)
    assert [a.id for a in first] == [a.id for a in second]
    # Pending queue still has exactly one row.
    pending = pipeline.queue.list_pending("tenant-a")
    assert len(pending) == 1


# ----------------------------------------------------------------------------
# Multi-tenant isolation
# ----------------------------------------------------------------------------


def test_tenant_isolation_in_queue_and_actions() -> None:
    pipeline = make_default_pipeline()
    pipeline.policy.set_policies("alpha", {"*": "approval_required"})
    pipeline.policy.set_policies("beta", {"*": "approval_required"})
    [a_alpha] = pipeline.handle_proposal(
        _proposal(tenant_id="alpha", event_id="evt_alpha", proposal_id="prop_alpha")
    )
    [a_beta] = pipeline.handle_proposal(
        _proposal(tenant_id="beta", event_id="evt_beta", proposal_id="prop_beta")
    )
    assert a_alpha.id != a_beta.id
    assert {r.action_id for r in pipeline.queue.list_pending("alpha")} == {
        a_alpha.id
    }
    assert {r.action_id for r in pipeline.queue.list_pending("beta")} == {
        a_beta.id
    }


# ----------------------------------------------------------------------------
# Executor failure path
# ----------------------------------------------------------------------------


def test_executor_failure_writes_execution_failed_no_raise() -> None:
    def bad(_action: Any) -> dict[str, Any]:
        raise RuntimeError("downstream unavailable")

    table = dict(DEFAULT_EXECUTORS)
    table[ActionType.SEND_BORROWER_MESSAGE] = bad
    pipeline = DecisionPipeline(
        policy=DeclarativePolicyEngine(),
        queue=InMemoryApprovalQueue(),
        audit_log=InMemoryAuditLog(),
        executor=ActionExecutor(executor_table=table),
    )
    pipeline.policy.set_policies("tenant-a", {"*": "auto"})
    [a] = pipeline.handle_proposal(_proposal())
    assert a.status is ActionStatus.EXECUTION_FAILED
    kinds = [e.kind for e in pipeline.audit.entries_for_action(a.id)]
    assert AuditKind.EXECUTION_FAILED in kinds
    # Original error captured in audit payload.
    failed_entries = [
        e
        for e in pipeline.audit.entries_for_action(a.id)
        if e.kind is AuditKind.EXECUTION_FAILED
    ]
    assert failed_entries[0].payload["error_message"] == "downstream unavailable"


# ----------------------------------------------------------------------------
# Listing + clear
# ----------------------------------------------------------------------------


def test_list_pending_actions_returns_canonical_rows_in_fifo() -> None:
    pipeline = make_default_pipeline()
    pipeline.policy.set_policies("tenant-a", {"*": "approval_required"})
    ids: list[str] = []
    for i in range(3):
        [a] = pipeline.handle_proposal(
            _proposal(event_id=f"evt_{i}", proposal_id=f"prop_{i}")
        )
        ids.append(a.id)
    pending = pipeline.list_pending_actions("tenant-a")
    assert [a.id for a in pending] == ids


def test_clear_resets_all_state() -> None:
    pipeline = make_default_pipeline()
    pipeline.policy.set_policies("tenant-a", {"*": "approval_required"})
    pipeline.handle_proposal(_proposal())
    pipeline.clear()
    assert pipeline.list_pending_actions("tenant-a") == []
    # Idempotency index also cleared — same event_id can be reused.
    [a] = pipeline.handle_proposal(_proposal())
    assert a.status is ActionStatus.PENDING_APPROVAL


def test_audit_for_action_returns_entries_in_order() -> None:
    pipeline = make_default_pipeline()
    pipeline.policy.set_policies("tenant-a", {"*": "approval_required"})
    [a] = pipeline.handle_proposal(_proposal())
    pipeline.approve_action(a.id)
    entries = pipeline.audit_for_action(a.id)
    kinds = [e.kind for e in entries]
    # PROPOSAL precedes DECISION, then APPROVAL precedes EXECUTION.
    assert kinds.index(AuditKind.PROPOSAL) < kinds.index(AuditKind.DECISION)
    assert kinds.index(AuditKind.APPROVAL) < kinds.index(AuditKind.EXECUTION)
