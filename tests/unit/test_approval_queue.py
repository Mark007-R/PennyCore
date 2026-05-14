"""Tests for the Day-10 in-memory approval queue.

Coverage:
  1. enqueue → list_pending → approve transition.
  2. enqueue is idempotent on action_id.
  3. approve raises ApprovalStateError on double-approve.
  4. reject raises ApprovalStateError on double-reject.
  5. approve raises ApprovalStateError after reject (and vice versa).
  6. approve / reject raises ApprovalNotFoundError for unknown id.
  7. list_pending returns FIFO order.
  8. list_pending isolates tenants.
  9. Resolved rows leave the pending list.
 10. `get` returns the (possibly resolved) row; is_pending true only on pending.
 11. Empty tenant_id / action_id rejected.
"""

from __future__ import annotations

import pytest

from orchestrator.approval_queue import (
    ApprovalNotFoundError,
    ApprovalStateError,
    InMemoryApprovalQueue,
)


def test_enqueue_then_approve_transitions_state() -> None:
    q = InMemoryApprovalQueue()
    rule = q.enqueue("act_001", "tenant-a")
    assert rule.state == "pending"
    pending = q.list_pending("tenant-a")
    assert [r.action_id for r in pending] == ["act_001"]
    approved = q.approve("act_001", decided_by="alice")
    assert approved.state == "approved"
    assert approved.decided_by == "alice"
    assert approved.decided_at is not None
    assert q.list_pending("tenant-a") == []


def test_enqueue_idempotent_on_action_id() -> None:
    q = InMemoryApprovalQueue()
    a = q.enqueue("act_001", "tenant-a")
    b = q.enqueue("act_001", "tenant-a")
    # Same row object — no duplicate ID in the pending list.
    assert a.id == b.id
    assert len(q.list_pending("tenant-a")) == 1


def test_double_approve_raises_state_error() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_001", "tenant-a")
    q.approve("act_001")
    with pytest.raises(ApprovalStateError, match="already 'approved'"):
        q.approve("act_001")


def test_double_reject_raises_state_error() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_001", "tenant-a")
    q.reject("act_001", reason="bad payload")
    with pytest.raises(ApprovalStateError, match="already 'rejected'"):
        q.reject("act_001", reason="again")


def test_approve_after_reject_raises() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_001", "tenant-a")
    q.reject("act_001", reason="bad")
    with pytest.raises(ApprovalStateError):
        q.approve("act_001")


def test_reject_after_approve_raises() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_001", "tenant-a")
    q.approve("act_001")
    with pytest.raises(ApprovalStateError):
        q.reject("act_001")


def test_approve_unknown_action_raises_not_found() -> None:
    q = InMemoryApprovalQueue()
    with pytest.raises(ApprovalNotFoundError, match="act_999"):
        q.approve("act_999")


def test_reject_unknown_action_raises_not_found() -> None:
    q = InMemoryApprovalQueue()
    with pytest.raises(ApprovalNotFoundError):
        q.reject("act_999")


def test_list_pending_fifo() -> None:
    q = InMemoryApprovalQueue()
    for i in range(5):
        q.enqueue(f"act_{i:03d}", "tenant-fifo")
    pending = q.list_pending("tenant-fifo")
    assert [r.action_id for r in pending] == [
        "act_000",
        "act_001",
        "act_002",
        "act_003",
        "act_004",
    ]


def test_list_pending_isolates_tenants() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_alpha_1", "alpha")
    q.enqueue("act_alpha_2", "alpha")
    q.enqueue("act_beta_1", "beta")
    assert {r.action_id for r in q.list_pending("alpha")} == {
        "act_alpha_1",
        "act_alpha_2",
    }
    assert {r.action_id for r in q.list_pending("beta")} == {"act_beta_1"}


def test_resolved_row_dropped_from_pending() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_a", "t")
    q.enqueue("act_b", "t")
    q.enqueue("act_c", "t")
    q.approve("act_b")
    q.reject("act_c", reason="bad")
    pending_ids = {r.action_id for r in q.list_pending("t")}
    assert pending_ids == {"act_a"}


def test_get_returns_resolved_row() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_001", "tenant-a")
    q.approve("act_001")
    rule = q.get("act_001")
    assert rule is not None
    assert rule.state == "approved"
    assert q.get("never-enqueued") is None


def test_is_pending_reflects_state() -> None:
    q = InMemoryApprovalQueue()
    q.enqueue("act_001", "tenant-a")
    assert q.is_pending("act_001") is True
    q.approve("act_001")
    assert q.is_pending("act_001") is False
    assert q.is_pending("missing") is False


def test_empty_action_id_rejected() -> None:
    q = InMemoryApprovalQueue()
    with pytest.raises(ValueError, match="action_id"):
        q.enqueue("", "tenant-a")


def test_empty_tenant_id_rejected() -> None:
    q = InMemoryApprovalQueue()
    with pytest.raises(ValueError, match="tenant_id"):
        q.enqueue("act_001", "")
