"""Tests for the Day-32 demo scenario builder (`ui.demo_scenario`).

The Streamlit page itself is tested as a smoke check via AppTest; the
load-bearing logic lives in `build_scenario_state` + the `JANE_TIMELINE`
/ `PHASE_FINDINGS` data. Cover:

  * `JANE_TIMELINE` is non-empty and every `finding` key points to a
    `PHASE_FINDINGS` entry (no dangling references).
  * `build_scenario_state()` is idempotent: re-running returns the same
    action_ids.
  * The paystub event lands on Jane's tenant + customer.
  * The auto-execute ack action transitions out of `pending_approval`.
  * The anomaly action sits at `pending_approval` with the 2-of-3
    quorum from Day 31's seed (matches the Phase-5 wrap claim).
  * `JANE_TIMELINE` action_ids are stamped after `build_scenario_state`
    so the UI panels can find the right action per step.
  * Streamlit page boots through AppTest.
"""

from __future__ import annotations

import pytest

from ui.demo_scenario import (
    JANE_PAYSTUB_EVENT_ID,
    JANE_TIMELINE,
    PHASE_FINDINGS,
    SCENARIO_CUSTOMER,
    SCENARIO_TENANT,
    build_scenario_state,
)


@pytest.fixture(autouse=True)
def _fresh_pipeline():
    """Pipeline reset before each test — same pattern as the Day-31
    adapter tests so seed + handle_proposal calls run against a
    deterministic starting state."""
    from orchestrator.api import _get_pipeline_for_tests

    _get_pipeline_for_tests().clear()
    yield
    _get_pipeline_for_tests().clear()


# ---------------------------------------------------------------------------
# Static data integrity
# ---------------------------------------------------------------------------


def test_timeline_is_non_empty() -> None:
    assert len(JANE_TIMELINE) >= 4


def test_timeline_finding_keys_all_resolve() -> None:
    for i, step in enumerate(JANE_TIMELINE):
        key = step.get("finding")
        if key is None:
            continue
        assert key in PHASE_FINDINGS, (
            f"step {i} references unknown finding key {key!r}"
        )


def test_phase_findings_carry_required_fields() -> None:
    for key, finding in PHASE_FINDINGS.items():
        for field in ("phase", "title", "body", "reference"):
            assert field in finding, f"finding {key!r} missing {field}"


# ---------------------------------------------------------------------------
# build_scenario_state
# ---------------------------------------------------------------------------


def test_build_scenario_state_returns_expected_shape() -> None:
    state = build_scenario_state()
    assert "paystub_event" in state
    assert "brief" in state
    assert "ack_action_id" in state
    assert "anomaly_action_id" in state
    # The event envelope carries Jane's identifiers.
    assert state["paystub_event"]["event_id"] == JANE_PAYSTUB_EVENT_ID
    assert state["paystub_event"]["tenant_id"] == SCENARIO_TENANT
    assert state["paystub_event"]["customer_id"] == SCENARIO_CUSTOMER


def test_build_scenario_state_is_idempotent() -> None:
    a = build_scenario_state()
    b = build_scenario_state()
    assert a["ack_action_id"] == b["ack_action_id"]
    assert a["anomaly_action_id"] == b["anomaly_action_id"]


def test_brief_panel_describes_hybrid_strategy() -> None:
    state = build_scenario_state()
    assert "hybrid" in state["brief"]["strategy"].lower()
    assert state["brief"]["tokens"] <= state["brief"]["budget"]


def test_ack_action_auto_executes() -> None:
    """tenant_acme_bank + send_borrower_message is policy=`auto` in
    the Day-31 seeder. The action must transition out of pending_approval."""
    from ui.approver_data import get_action_detail

    state = build_scenario_state()
    detail = get_action_detail(state["ack_action_id"], tenant_id=SCENARIO_TENANT)
    assert detail is not None
    assert detail["status"] != "pending_approval"


def test_anomaly_action_requires_quorum_approval() -> None:
    """notify_loan_officer on tenant_acme_bank is policy=
    `approval_required` AND arms the 2-of-3 quorum from Day 31's seed."""
    from ui.approver_data import get_action_detail

    state = build_scenario_state()
    detail = get_action_detail(
        state["anomaly_action_id"], tenant_id=SCENARIO_TENANT
    )
    assert detail is not None
    assert detail["status"] == "pending_approval"
    progress = detail["approval_progress"]
    assert progress is not None
    assert progress["required_approvals"] == 2
    assert progress["approvals_remaining"] == 2
    assert set(progress["eligible_approvers"]) == {
        "compliance_alice",
        "risk_bob",
        "legal_carol",
    }


def test_timeline_action_ids_stamped_after_build() -> None:
    """After build_scenario_state, every timeline step that has an
    `action_kind` gets an `action_id` stamped so the UI's panel
    builder can render the matching action."""
    state = build_scenario_state()
    for step in JANE_TIMELINE:
        if step.get("action_kind") == "ack":
            assert step.get("action_id") == state["ack_action_id"]
        if step.get("action_kind") == "anomaly":
            assert step.get("action_id") == state["anomaly_action_id"]


# ---------------------------------------------------------------------------
# Streamlit page smoke test
# ---------------------------------------------------------------------------


def test_demo_scenario_page_boots_clean() -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("ui/demo_scenario_app.py")
    at.run(timeout=30)
    assert not at.exception, [str(e.value) for e in at.exception]
    # The title renders the Jane's-mortgage-journey page name.
    assert any("Jane" in t.value for t in at.title)


def test_demo_scenario_page_slider_advances_timeline() -> None:
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("ui/demo_scenario_app.py")
    at.run(timeout=30)
    # Move the slider to the last step and re-run.
    sliders = at.sidebar.slider
    assert sliders, "expected a step slider in the sidebar"
    sliders[0].set_value(len(JANE_TIMELINE) - 1).run(timeout=30)
    assert not at.exception, [str(e.value) for e in at.exception]
