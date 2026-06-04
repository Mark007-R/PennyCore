"""Day-32 demo scenario UI — Jane's mortgage journey (Phase 6 wrap).

Launch:
    streamlit run ui/demo_scenario_app.py

This is the project's storytelling surface. A hiring manager / YC
partner clicks through the Jane's-mortgage-journey timeline and the
page surfaces:

  * the event stream landing on context-engine,
  * the brief assembled by the retrieval champion (Phase-5 hybrid),
  * the orchestrator's policy decision (Phase-3 declarative champion),
  * the N-of-M quorum vote (Phase-5 four-eyes),
  * the executor's action,
  * the audit trail closing the loop.

Each timeline step links back to a Phase-3 or Phase-5 finding so the
"production-grade infrastructure" story stays grounded in the
project's measured results, not just diagrams.

The page reads from `ui.demo_scenario` for the timeline + the live
adapter (`ui.approver_data`) for the action-state pieces. Same
pattern as the Day-31 approver dashboard — Streamlit is presentation,
business logic is testable Python below it.
"""

from __future__ import annotations

import streamlit as st

from ui.approver_data import get_action_detail, seed_demo_data
from ui.demo_scenario import (
    JANE_TIMELINE,
    PHASE_FINDINGS,
    SCENARIO_TENANT,
    build_scenario_state,
)

PAGE_TITLE = "PennyCore — Jane's Mortgage Journey"


def _set_page_config() -> None:
    st.set_page_config(
        page_title=PAGE_TITLE,
        page_icon=":house:",
        layout="wide",
        initial_sidebar_state="expanded",
    )


def _sidebar() -> dict:
    st.sidebar.title(":compass: Demo controls")
    st.sidebar.markdown(
        "This walkthrough drives the live PennyCore pipeline — every "
        "step you see on the right is computed, not pre-rendered."
    )
    if st.sidebar.button(
        ":seedling: (Re)build Jane's scenario", use_container_width=True
    ):
        build_scenario_state()
        st.rerun()
    step_idx = st.sidebar.slider(
        "Timeline step",
        min_value=0,
        max_value=len(JANE_TIMELINE) - 1,
        value=0,
        step=1,
    )
    st.sidebar.divider()
    st.sidebar.markdown(
        "**Source:** SKILL Day-32 task — *demo scenario UI: Jane's "
        "mortgage journey walkthrough with timeline visualization.*"
    )
    return {"step_idx": step_idx}


def _render_header() -> None:
    st.title(PAGE_TITLE)
    st.caption(
        "Document upload → context retrieval → policy decision → "
        "human approval → action execution, end-to-end. The "
        "highlighted box on each step names the Phase-3 / Phase-5 "
        "finding the step depends on."
    )


def _render_timeline(step_idx: int) -> None:
    st.subheader(":clock1: Timeline")
    for i, step in enumerate(JANE_TIMELINE):
        completed = i < step_idx
        active = i == step_idx
        icon = ":white_check_mark:" if completed else (
            ":hourglass_flowing_sand:" if active else ":white_circle:"
        )
        with st.container(border=active):
            st.markdown(
                f"### {icon} **Step {i + 1}.** {step['title']}"
            )
            st.markdown(step["description"])
            if active and step.get("finding"):
                _render_finding_box(step["finding"])


def _render_finding_box(finding_key: str) -> None:
    """Render the Phase-3 / Phase-5 finding card attached to a step."""
    finding = PHASE_FINDINGS.get(finding_key)
    if finding is None:
        return
    st.info(
        f"**Phase-{finding['phase']} finding · {finding['title']}**\n\n"
        f"{finding['body']}\n\n"
        f"_Reference:_ `{finding['reference']}`"
    )


def _render_scenario_state(step_idx: int) -> None:
    """Right-side panel: snapshot of the orchestrator's state for the
    selected step (what would a Jaeger / dashboard view show at this
    moment?)."""
    st.subheader(":mag_right: System state at this step")
    state = build_scenario_state()
    step = JANE_TIMELINE[step_idx]
    action_id = step.get("action_id")
    if action_id:
        detail = get_action_detail(action_id, tenant_id=SCENARIO_TENANT)
        if detail is not None:
            _render_action_card(detail)
            return
    elif step.get("show_brief"):
        _render_brief_card(state)
        return
    elif step.get("show_event"):
        _render_event_card(state)
        return

    st.caption(
        "Scrub the **Timeline step** slider in the sidebar to watch "
        "the orchestrator state evolve through Jane's journey."
    )


def _render_event_card(state: dict) -> None:
    event = state["paystub_event"]
    st.markdown(f"**:envelope: Event** &nbsp; `{event['event_id']}`")
    st.markdown(f"**Tenant** &nbsp; `{event['tenant_id']}`")
    st.markdown(f"**Channel** &nbsp; `{event['channel_code']}`")
    st.markdown(f"**Type** &nbsp; `{event['event_type']}`")
    st.markdown(f"**Customer** &nbsp; `{event['customer_id']}`")
    st.json(event["payload"])


def _render_brief_card(state: dict) -> None:
    brief = state["brief"]
    st.markdown(
        f"**:scroll: Brief assembled** &nbsp; "
        f"strategy=`{brief['strategy']}` &nbsp; "
        f"tokens=`{brief['tokens']}` of `{brief['budget']}` budget"
    )
    st.code(brief["body"], language="markdown")


def _render_action_card(detail: dict) -> None:
    st.markdown(
        f"**:rocket: Action** &nbsp; `{detail['action_type']}` &nbsp; "
        f"(`{detail['status']}`)"
    )
    st.caption(f"action_id  `{detail['action_id']}`")
    st.caption(f"event_id  `{detail['event_id']}`")
    if detail.get("reasoning"):
        st.markdown(f"> {detail['reasoning']}")
    progress = detail.get("approval_progress")
    if progress is not None:
        required = max(progress["required_approvals"], 1)
        recorded = progress["approvals_recorded"]
        ratio = min(recorded / required, 1.0)
        st.progress(
            ratio,
            text=(
                f"{recorded} of {required} approvals "
                f"({progress['approvals_remaining']} remaining)"
            ),
        )
        if progress.get("eligible_approvers"):
            st.caption(
                "Eligible approvers: "
                + ", ".join(progress["eligible_approvers"])
            )
    if detail.get("audit_trail"):
        st.markdown("**Audit trail**")
        for entry in detail["audit_trail"]:
            st.markdown(
                f"- `{entry['ts']}` &nbsp; **{entry['kind']}** by "
                f"`{entry['actor_kind']}`"
                + (f" ({entry['actor_id']})" if entry.get("actor_id") else "")
            )


def _render_phase_findings_summary() -> None:
    st.divider()
    st.subheader(":sparkles: Phase findings referenced in this walkthrough")
    for key, finding in PHASE_FINDINGS.items():
        with st.expander(
            f"Phase {finding['phase']} — {finding['title']}", expanded=False
        ):
            st.markdown(finding["body"])
            st.caption(f"Reference: `{finding['reference']}`")


def main() -> None:
    _set_page_config()
    _render_header()
    filters = _sidebar()
    # Make sure the live pipeline has Jane's data the first time the
    # page renders. The seeder is idempotent.
    seed_demo_data()
    # Ensure the scenario-specific state exists (rebuilds only on
    # sidebar button click or first-load).
    build_scenario_state()

    left, right = st.columns([1.4, 1])
    with left:
        _render_timeline(filters["step_idx"])
    with right:
        _render_scenario_state(filters["step_idx"])

    _render_phase_findings_summary()


if __name__ == "__main__":  # pragma: no cover — Streamlit entry point
    main()
