"""Tests for the Day-31 approver-UI data adapter (`ui.approver_data`).

The Streamlit UI itself is hard to test directly — Streamlit renders
on import via globals, and AppTest is a moving target across versions.
Our load-bearing contract is the data adapter: if `list_pending`,
`approve`, `reject`, `seed_demo_data`, and `get_action_detail` are
each correct in isolation and translate exceptions cleanly, the UI's
behavior is determined by those plus Streamlit's own rendering.

What we cover here:

  * `seed_demo_data` is idempotent without `force=True`.
  * `seed_demo_data(force=True)` populates the three demo tenants and
    arms the acme_bank N-of-M quorum rule.
  * `list_pending` returns the pending rows for one tenant only
    (multi-tenant invariant, rule 15).
  * `approve` happy path resolves a single-approver row.
  * `approve` quorum path records a partial vote, second vote
    resolves.
  * `approve` raises `ApproverActionError("duplicate_vote", ...)` on
    a repeat voter.
  * `approve` raises `ApproverActionError("not_eligible", ...)` on a
    voter outside the pool.
  * `approve` raises `ApproverActionError("not_found", ...)` on a
    cross-tenant id (404-shape — never leaks existence).
  * `reject` veto resolves a quorum row regardless of partial votes.
  * `get_action_detail` returns None on cross-tenant access.
  * `recent_audit` returns newest-first, respects `limit`.
"""

from __future__ import annotations

import pytest

from ui.approver_data import (
    ApproverActionError,
    approve,
    get_action_detail,
    list_pending,
    list_tenants,
    recent_audit,
    reject,
    seed_demo_data,
)


@pytest.fixture(autouse=True)
def _fresh_pipeline():
    """Clear the orchestrator pipeline before every test so seed and
    approve calls run against a clean slate."""
    from orchestrator.api import _get_pipeline_for_tests

    _get_pipeline_for_tests().clear()
    yield
    _get_pipeline_for_tests().clear()


# ---------------------------------------------------------------------------
# seed_demo_data
# ---------------------------------------------------------------------------


def test_seed_demo_data_populates_three_tenants() -> None:
    summary = seed_demo_data(force=True)
    assert summary["seeded"] is True
    assert "tenant_acme_bank" in summary["tenants"]
    assert "tenant_globetrek_concierge" in summary["tenants"]
    assert "tenant_jefferson_credit" in summary["tenants"]
    # Pending rows: acme_bank notify_loan_officer (quorum) +
    # acme_bank request_document if seeded (not in current set) +
    # jefferson_credit request_document. The auto-executed rows
    # don't queue. So at minimum: 1 acme quorum + 1 jefferson single
    # = 2 pending.
    assert summary["pending"] >= 2


def test_seed_demo_data_is_idempotent_without_force() -> None:
    summary_first = seed_demo_data(force=True)
    assert summary_first["seeded"] is True
    summary_second = seed_demo_data()
    assert summary_second["seeded"] is False
    assert summary_second["audit"] == summary_first["audit"]


def test_seed_demo_data_force_wipes_first() -> None:
    seed_demo_data(force=True)
    a = recent_audit("tenant_acme_bank")
    seed_demo_data(force=True)
    b = recent_audit("tenant_acme_bank")
    # After re-seeding, the audit count should be identical (deterministic
    # seeder writes the same rows). The action_ids will differ because
    # they're freshly UUID-allocated.
    assert len(a) == len(b)


def test_seed_demo_data_arms_acme_bank_quorum_rule() -> None:
    seed_demo_data(force=True)
    pending = list_pending("tenant_acme_bank")
    quorum_row = next(
        (r for r in pending if r["action_type"] == "notify_loan_officer"),
        None,
    )
    assert quorum_row is not None
    progress = quorum_row["approval_progress"]
    assert progress is not None
    assert progress["required_approvals"] == 2
    assert progress["approvals_remaining"] == 2
    assert set(progress["eligible_approvers"]) == {
        "compliance_alice",
        "risk_bob",
        "legal_carol",
    }


# ---------------------------------------------------------------------------
# Tenant isolation in the read path
# ---------------------------------------------------------------------------


def test_list_pending_is_tenant_scoped() -> None:
    seed_demo_data(force=True)
    acme = list_pending("tenant_acme_bank")
    jeff = list_pending("tenant_jefferson_credit")
    # No overlap between tenants' pending rows.
    acme_ids = {r["action_id"] for r in acme}
    jeff_ids = {r["action_id"] for r in jeff}
    assert acme_ids.isdisjoint(jeff_ids)


def test_list_pending_empty_for_unknown_tenant() -> None:
    seed_demo_data(force=True)
    assert list_pending("tenant_nonexistent") == []


def test_list_tenants_returns_seeded_tenants() -> None:
    seed_demo_data(force=True)
    tenants = list_tenants()
    assert set(tenants) >= {
        "tenant_acme_bank",
        "tenant_globetrek_concierge",
        "tenant_jefferson_credit",
    }


# ---------------------------------------------------------------------------
# approve / reject happy paths
# ---------------------------------------------------------------------------


def _jefferson_pending_action_id() -> str:
    rows = list_pending("tenant_jefferson_credit")
    assert rows, "jefferson_credit should have one pending row after seed"
    return rows[0]["action_id"]


def _acme_quorum_action_id() -> str:
    rows = list_pending("tenant_acme_bank")
    quorum = next(
        r for r in rows if r["action_type"] == "notify_loan_officer"
    )
    return quorum["action_id"]


def test_approve_single_approver_resolves_immediately() -> None:
    seed_demo_data(force=True)
    action_id = _jefferson_pending_action_id()
    result = approve(
        action_id,
        decided_by="compliance_alice",
        tenant_id="tenant_jefferson_credit",
    )
    # Single-approver row → status moves out of pending_approval.
    assert result["status"] != "pending_approval"
    # Subsequent list call: the row is gone from pending.
    pending_ids = {r["action_id"] for r in list_pending("tenant_jefferson_credit")}
    assert action_id not in pending_ids


def test_approve_quorum_records_partial_vote() -> None:
    seed_demo_data(force=True)
    action_id = _acme_quorum_action_id()
    result = approve(
        action_id,
        decided_by="compliance_alice",
        tenant_id="tenant_acme_bank",
    )
    progress = result["approval_progress"]
    assert progress["approvals_recorded"] == 1
    assert progress["approvals_remaining"] == 1
    # Action still pending until second vote completes the quorum.
    assert result["status"] == "pending_approval"


def test_approve_quorum_second_vote_resolves() -> None:
    seed_demo_data(force=True)
    action_id = _acme_quorum_action_id()
    approve(
        action_id,
        decided_by="compliance_alice",
        tenant_id="tenant_acme_bank",
    )
    result = approve(
        action_id,
        decided_by="risk_bob",
        tenant_id="tenant_acme_bank",
    )
    assert result["status"] != "pending_approval"
    assert result["approval_progress"]["state"] == "approved"


def test_reject_vetoes_quorum_regardless_of_partial_approvals() -> None:
    seed_demo_data(force=True)
    action_id = _acme_quorum_action_id()
    # Partial approval.
    approve(
        action_id,
        decided_by="compliance_alice",
        tenant_id="tenant_acme_bank",
    )
    # Any eligible reject ends the row.
    result = reject(
        action_id,
        decided_by="risk_bob",
        tenant_id="tenant_acme_bank",
        reason="potential income misrep",
    )
    assert result["status"] == "rejected"


# ---------------------------------------------------------------------------
# approve / reject error translations
# ---------------------------------------------------------------------------


def test_approve_duplicate_vote_raises_duplicate_category() -> None:
    seed_demo_data(force=True)
    action_id = _acme_quorum_action_id()
    approve(
        action_id,
        decided_by="compliance_alice",
        tenant_id="tenant_acme_bank",
    )
    with pytest.raises(ApproverActionError) as excinfo:
        approve(
            action_id,
            decided_by="compliance_alice",
            tenant_id="tenant_acme_bank",
        )
    assert excinfo.value.category == "duplicate_vote"


def test_approve_outside_pool_raises_not_eligible() -> None:
    seed_demo_data(force=True)
    action_id = _acme_quorum_action_id()
    with pytest.raises(ApproverActionError) as excinfo:
        approve(
            action_id,
            decided_by="external_dave",
            tenant_id="tenant_acme_bank",
        )
    assert excinfo.value.category == "not_eligible"


def test_approve_cross_tenant_raises_not_found() -> None:
    """The Day-20 multi-tenant guard: an approve call with the wrong
    tenant_id must return the same shape as a true miss — never leaks
    existence."""
    seed_demo_data(force=True)
    action_id = _acme_quorum_action_id()
    with pytest.raises(ApproverActionError) as excinfo:
        approve(
            action_id,
            decided_by="compliance_alice",
            tenant_id="tenant_jefferson_credit",
        )
    assert excinfo.value.category == "not_found"


def test_approve_already_decided_raises_already_decided() -> None:
    seed_demo_data(force=True)
    action_id = _jefferson_pending_action_id()
    approve(
        action_id,
        decided_by="compliance_alice",
        tenant_id="tenant_jefferson_credit",
    )
    with pytest.raises(ApproverActionError) as excinfo:
        approve(
            action_id,
            decided_by="risk_bob",
            tenant_id="tenant_jefferson_credit",
        )
    # The queue raises ApprovalNotFound once the row resolves and is
    # removed from pending (or ApprovalStateError if it lingers). Either
    # surfaces as `not_found` / `already_decided`; we accept both.
    assert excinfo.value.category in {"already_decided", "not_found"}


# ---------------------------------------------------------------------------
# get_action_detail + recent_audit
# ---------------------------------------------------------------------------


def test_get_action_detail_returns_audit_trail() -> None:
    seed_demo_data(force=True)
    action_id = _jefferson_pending_action_id()
    detail = get_action_detail(
        action_id, tenant_id="tenant_jefferson_credit"
    )
    assert detail is not None
    assert detail["action_id"] == action_id
    assert len(detail["audit_trail"]) >= 2  # PROPOSAL + DECISION


def test_get_action_detail_cross_tenant_returns_none() -> None:
    seed_demo_data(force=True)
    action_id = _jefferson_pending_action_id()
    assert (
        get_action_detail(action_id, tenant_id="tenant_acme_bank") is None
    )


def test_recent_audit_is_newest_first_and_respects_limit() -> None:
    seed_demo_data(force=True)
    entries = recent_audit("tenant_acme_bank", limit=3)
    assert len(entries) <= 3
    ids = [e["id"] for e in entries if e["id"] is not None]
    # Newest-first by id (id is the BIGSERIAL surrogate, monotonic).
    assert ids == sorted(ids, reverse=True)
    # Every entry has a ts field too.
    for entry in entries:
        assert "ts" in entry


def test_recent_audit_tenant_scoped() -> None:
    seed_demo_data(force=True)
    acme_audit = recent_audit("tenant_acme_bank", limit=100)
    jeff_audit = recent_audit("tenant_jefferson_credit", limit=100)
    # Every acme entry's tenant is acme; every jefferson entry's is jefferson.
    for entry in acme_audit:
        assert entry["tenant_id"] == "tenant_acme_bank"
    for entry in jeff_audit:
        assert entry["tenant_id"] == "tenant_jefferson_credit"


# ---------------------------------------------------------------------------
# Streamlit app smoke test — actually boot the page through Streamlit's
# AppTest harness. Doesn't render to pixels, but it does exercise every
# import and the sidebar/page-config calls — catches "the data adapter
# changed shape and the UI broke" at PR time.
# ---------------------------------------------------------------------------


def test_approver_app_boots_clean_without_seed() -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("ui/approver_app.py")
    at.run(timeout=30)
    assert not at.exception, [str(e.value) for e in at.exception]
    # No tenants yet → info message + disabled selectbox.
    assert any("Seed demo data" in b.label for b in at.sidebar.button)


def test_approver_app_renders_pending_after_seed() -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("ui/approver_app.py")
    at.run(timeout=30)
    assert not at.exception, [str(e.value) for e in at.exception]
    # Click "Seed demo data" — Streamlit re-runs the script and the
    # tenant dropdown should populate.
    at.sidebar.button[0].click().run(timeout=30)
    assert not at.exception, [str(e.value) for e in at.exception]
    # After seed, the main page should render the pending-approvals
    # subheader text somewhere in the rendered output.
    headers = [h.value for h in at.subheader]
    assert any("Pending" in h or "pending" in h for h in headers), headers
