"""Tests for the Day-26 N-of-M quorum extension to the approval queue.

The single-approver path is covered by `test_approval_queue.py`; this
suite exercises the parallel-approval behavior layered on top:

  1. Default enqueue is still single-approver (back-compat invariant).
  2. 2-of-3: first vote stays pending, second resolves.
  3. 3-of-3 needs every approver.
  4. Partial vote bumps row_version and records the approver in order.
  5. approvals_remaining tracks the running tally.
  6. Same approver voting twice → DuplicateApproverError.
  7. Quorum row with no named approver → ApproverNotEligibleError.
  8. Voter outside the eligible pool → ApproverNotEligibleError (approve).
  9. Voter outside the eligible pool → ApproverNotEligibleError (reject).
 10. In-pool approver on a single-approver+pool row resolves.
 11. Reject is a veto — one rejection ends a partially-approved row.
 12. Veto preserves the partial approvals on the resolved row.
 13. approve after quorum reached → ApprovalStateError.
 14. approve after veto → ApprovalStateError.
 15. enqueue rejects required_approvals < 1.
 16. enqueue rejects a quorum larger than the eligible pool.
 17. enqueue rejects duplicate names in the eligible pool.
 18. Partial-quorum rows stay in list_pending; resolved ones leave.
 19. Open-pool quorum counts any distinct named voters.
 20. Quorum rows stay tenant-isolated.
"""

from __future__ import annotations

import pytest

from orchestrator.approval_queue import (
    ApprovalStateError,
    ApproverNotEligibleError,
    DuplicateApproverError,
    InMemoryApprovalQueue,
)


def test_default_enqueue_is_single_approver() -> None:
    q = InMemoryApprovalQueue()
    rule = q.enqueue("act_1", "t")
    assert rule.required_approvals == 1
    assert rule.eligible_approvers is None
    approved = q.approve("act_1", decided_by="alice")
    assert approved.state == "approved"
    assert approved.approvals == ["alice"]


def test_two_of_three_first_vote_stays_pending() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_1", "t", required_approvals=2)
    after_first = q.approve("act_1", decided_by="alice")
    assert after_first.state == "pending"
    assert after_first.approvals == ["alice"]
    assert after_first.approvals_remaining == 1
    after_second = q.approve("act_1", decided_by="bob")
    assert after_second.state == "approved"
    assert after_second.approvals == ["alice", "bob"]
    assert after_second.approvals_remaining == 0
    assert after_second.decided_by == "bob"
    assert after_second.decided_at is not None


def test_three_of_three_needs_all() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_1", "t", required_approvals=3)
    assert q.approve("act_1", decided_by="a").state == "pending"
    assert q.approve("act_1", decided_by="b").state == "pending"
    assert q.approve("act_1", decided_by="c").state == "approved"


def test_partial_vote_bumps_row_version_and_orders_approvers() -> None:
    q = InMemoryApprovalQueue()
    base = q.enqueue("act_1", "t", required_approvals=3)
    assert base.row_version == 1
    r1 = q.approve("act_1", decided_by="alice")
    r2 = q.approve("act_1", decided_by="bob")
    assert r1.row_version == 2
    assert r2.row_version == 3
    assert r2.approvals == ["alice", "bob"]


def test_approvals_remaining_tracks_tally() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_1", "t", required_approvals=3)
    assert q.get("act_1").approvals_remaining == 3
    q.approve("act_1", decided_by="a")
    assert q.get("act_1").approvals_remaining == 2
    q.approve("act_1", decided_by="b")
    assert q.get("act_1").approvals_remaining == 1


def test_duplicate_approver_rejected() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_1", "t", required_approvals=2)
    q.approve("act_1", decided_by="alice")
    with pytest.raises(DuplicateApproverError, match="alice"):
        q.approve("act_1", decided_by="alice")
    # The duplicate attempt did not count toward quorum.
    assert q.get("act_1").approvals == ["alice"]
    assert q.is_pending("act_1") is True


def test_quorum_requires_named_approver() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_1", "t", required_approvals=2)
    with pytest.raises(ApproverNotEligibleError, match="named approver"):
        q.approve("act_1")  # decided_by=None


def test_approve_outside_pool_rejected() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue(
        "act_1", "t", required_approvals=2, eligible_approvers=["alice", "bob", "carol"]
    )
    with pytest.raises(ApproverNotEligibleError, match="dave"):
        q.approve("act_1", decided_by="dave")
    assert q.get("act_1").approvals == []


def test_reject_outside_pool_rejected() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_1", "t", required_approvals=2, eligible_approvers=["alice", "bob"])
    with pytest.raises(ApproverNotEligibleError):
        q.reject("act_1", decided_by="mallory", reason="sneaky veto")
    assert q.is_pending("act_1") is True


def test_in_pool_single_approver_resolves() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_1", "t", required_approvals=1, eligible_approvers=["alice", "bob"])
    approved = q.approve("act_1", decided_by="bob")
    assert approved.state == "approved"


def test_reject_vetoes_partially_approved_row() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_1", "t", required_approvals=3)
    q.approve("act_1", decided_by="alice")
    q.approve("act_1", decided_by="bob")
    rejected = q.reject("act_1", decided_by="carol", reason="suspicious")
    assert rejected.state == "rejected"
    assert rejected.decided_by == "carol"
    assert rejected.decision_note == "suspicious"


def test_veto_preserves_partial_approvals() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_1", "t", required_approvals=3)
    q.approve("act_1", decided_by="alice")
    rejected = q.reject("act_1", decided_by="bob", reason="no")
    assert rejected.approvals == ["alice"]


def test_approve_after_quorum_raises_state_error() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_1", "t", required_approvals=2)
    q.approve("act_1", decided_by="alice")
    q.approve("act_1", decided_by="bob")  # quorum reached
    with pytest.raises(ApprovalStateError, match="already 'approved'"):
        q.approve("act_1", decided_by="carol")


def test_approve_after_veto_raises_state_error() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_1", "t", required_approvals=2)
    q.approve("act_1", decided_by="alice")
    q.reject("act_1", decided_by="bob", reason="veto")
    with pytest.raises(ApprovalStateError, match="already 'rejected'"):
        q.approve("act_1", decided_by="carol")


def test_enqueue_rejects_zero_quorum() -> None:
    q = InMemoryApprovalQueue()
    with pytest.raises(ValueError, match="required_approvals"):
        q.enqueue("act_1", "t", required_approvals=0)


def test_enqueue_rejects_quorum_larger_than_pool() -> None:
    q = InMemoryApprovalQueue()
    with pytest.raises(ValueError, match="exceeds"):
        q.enqueue(
            "act_1", "t", required_approvals=3, eligible_approvers=["alice", "bob"]
        )


def test_enqueue_rejects_duplicate_pool_members() -> None:
    q = InMemoryApprovalQueue()
    with pytest.raises(ValueError, match="duplicates"):
        q.enqueue(
            "act_1", "t", required_approvals=1, eligible_approvers=["alice", "alice"]
        )


def test_partial_rows_stay_pending_resolved_leave() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_a", "t", required_approvals=2)
    q.enqueue("act_b", "t", required_approvals=1)
    q.approve("act_a", decided_by="alice")  # partial
    q.approve("act_b", decided_by="bob")  # resolves
    pending_ids = {r.action_id for r in q.list_pending("t")}
    assert pending_ids == {"act_a"}


def test_open_pool_counts_distinct_named_voters() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_1", "t", required_approvals=2)  # no pool
    q.approve("act_1", decided_by="anyone")
    resolved = q.approve("act_1", decided_by="someone-else")
    assert resolved.state == "approved"


def test_quorum_rows_stay_tenant_isolated() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_alpha", "alpha", required_approvals=2)
    q.enqueue("act_beta", "beta", required_approvals=2)
    q.approve("act_alpha", decided_by="a")
    assert {r.action_id for r in q.list_pending("alpha")} == {"act_alpha"}
    assert {r.action_id for r in q.list_pending("beta")} == {"act_beta"}
