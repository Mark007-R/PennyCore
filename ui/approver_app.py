"""Day-31 admin UI — Streamlit dashboard for approvers (Phase 6).

Launch:
    streamlit run ui/approver_app.py

The dashboard renders three panels for a chosen tenant:

  1. **Pending queue.** One row per pending action with the N-of-M
     quorum progress bar, the planner's reasoning, and approve / reject
     buttons. The voter identity is read from the sidebar; the same
     voter can't satisfy two quorum seats (the data adapter raises a
     `duplicate_vote` `ApproverActionError`, which we surface as a
     yellow warning banner).
  2. **Action detail.** The currently-selected pending row's full
     audit trail — every state transition, who triggered it, when.
     This is the "show your work" surface compliance reviewers ask
     for.
  3. **Recent audit log.** Newest-first across the whole tenant. The
     limit is sidebar-controlled.

The "Seed demo data" sidebar button pushes a small Jane's-mortgage
dataset through the live pipeline so a fresh launch has rows to
render. The same data the Day-32 demo UI uses.

All state lives in the orchestrator's in-memory pipeline — this app
and the FastAPI surface point at the same dictionary. Approve via
this UI, observe in `GET /actions/{id}` — same row.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

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


PAGE_TITLE = "PennyCore — Approver Dashboard"


def _set_page_config() -> None:
    st.set_page_config(
        page_title=PAGE_TITLE,
        page_icon=":memo:",
        layout="wide",
        initial_sidebar_state="expanded",
    )


def _sidebar() -> dict[str, Any]:
    """Render the sidebar; return the user's filter selections."""
    st.sidebar.title(":compass: Controls")

    tenants = list_tenants()
    if not tenants:
        st.sidebar.info(
            "No data in pipeline yet — click _Seed demo data_ below to "
            "populate Jane's mortgage scenario across three tenants."
        )

    tenant_id = st.sidebar.selectbox(
        "Tenant",
        options=tenants if tenants else ["(none — seed demo data first)"],
        key="ui_tenant",
        disabled=not tenants,
    )

    voter = st.sidebar.text_input(
        "You (approver name)",
        value="compliance_alice",
        max_chars=64,
        help=(
            "Used as `decided_by` on every approve / reject. Each voter "
            "fills one seat of an N-of-M quorum exactly once."
        ),
    )

    audit_limit = st.sidebar.slider(
        "Recent audit limit",
        min_value=5,
        max_value=100,
        value=20,
        step=5,
    )

    st.sidebar.divider()
    if st.sidebar.button(":seedling: Seed demo data", use_container_width=True):
        summary = seed_demo_data(force=True)
        st.sidebar.success(
            f"Seeded {summary.get('pending', 0)} pending rows + "
            f"{summary.get('audit', 0)} audit entries across "
            f"{len(summary.get('tenants', []))} tenants."
        )
        st.rerun()

    return {
        "tenant_id": tenant_id if tenants else None,
        "voter": voter.strip() or "anonymous",
        "audit_limit": audit_limit,
    }


def _render_pending_queue(tenant_id: str, voter: str) -> str | None:
    """Render the pending-action table. Returns the action_id the user
    most recently selected (or None)."""
    st.subheader(":hourglass_flowing_sand: Pending approvals")
    rows = list_pending(tenant_id)
    if not rows:
        st.success("No pending approvals for this tenant — queue is clear.")
        return None

    selected_id = st.session_state.get("selected_action_id")
    for row in rows:
        with st.container(border=True):
            top = st.columns([4, 2, 2, 2])
            top[0].markdown(
                f"**{row['action_type']}** for `{row['customer_id']}`"
            )
            top[1].caption(f"Action `{row['action_id'][:14]}…`")
            top[2].caption(f"Event `{row['event_id']}`")
            top[3].caption(f"Status `{row['status']}`")

            if row.get("reasoning"):
                st.caption(f"Reasoning: {row['reasoning']}")

            _render_progress_block(row.get("approval_progress"))

            buttons = st.columns([1, 1, 1, 3])
            approve_clicked = buttons[0].button(
                ":white_check_mark: Approve",
                key=f"approve_{row['action_id']}",
                type="primary",
            )
            reject_clicked = buttons[1].button(
                ":x: Reject",
                key=f"reject_{row['action_id']}",
            )
            details_clicked = buttons[2].button(
                ":mag_right: Details",
                key=f"details_{row['action_id']}",
            )

            reject_reason = buttons[3].text_input(
                "reject reason (optional)",
                key=f"reject_reason_{row['action_id']}",
                label_visibility="collapsed",
                placeholder="optional reject reason",
            )

            if approve_clicked:
                _handle_approve(row["action_id"], voter, tenant_id)
            elif reject_clicked:
                _handle_reject(
                    row["action_id"], voter, tenant_id, reject_reason
                )
            elif details_clicked:
                selected_id = row["action_id"]
                st.session_state["selected_action_id"] = selected_id

    return selected_id


def _render_progress_block(progress: dict[str, Any] | None) -> None:
    if progress is None:
        st.caption("No queue row — single-step decision.")
        return
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
    if progress["eligible_approvers"]:
        st.caption(
            "Eligible: "
            + ", ".join(progress["eligible_approvers"])
            + (
                " ⋅ voted: " + ", ".join(progress["approvers"])
                if progress["approvers"]
                else ""
            )
        )


def _handle_approve(action_id: str, voter: str, tenant_id: str) -> None:
    try:
        result = approve(action_id, decided_by=voter, tenant_id=tenant_id)
    except ApproverActionError as exc:
        _render_action_error(exc)
        return
    progress = result.get("approval_progress")
    if progress is None or progress["state"] == "approved":
        st.success(f"Approved action {action_id[:14]}… as {voter}.")
    else:
        st.info(
            f"Recorded approval as {voter} — "
            f"{progress['approvals_remaining']} more needed for quorum."
        )
    st.rerun()


def _handle_reject(
    action_id: str, voter: str, tenant_id: str, reason: str
) -> None:
    try:
        reject(
            action_id,
            decided_by=voter,
            tenant_id=tenant_id,
            reason=reason,
        )
    except ApproverActionError as exc:
        _render_action_error(exc)
        return
    st.warning(f"Rejected action {action_id[:14]}… as {voter}.")
    st.rerun()


def _render_action_error(exc: ApproverActionError) -> None:
    icon = {
        "not_found": ":mag:",
        "already_decided": ":lock:",
        "not_eligible": ":no_entry:",
        "duplicate_vote": ":repeat:",
    }.get(exc.category, ":warning:")
    st.error(f"{icon} {exc.message}")


def _render_action_detail(tenant_id: str, action_id: str) -> None:
    st.subheader(":mag_right: Action detail")
    detail = get_action_detail(action_id, tenant_id=tenant_id)
    if detail is None:
        st.warning(
            "Selected action is no longer visible in this tenant — "
            "another tenant's row was filtered out by the multi-tenant guard, "
            "or the row was resolved and dropped."
        )
        return

    cols = st.columns(2)
    cols[0].markdown(f"**Action ID** &nbsp; `{detail['action_id']}`")
    cols[0].markdown(f"**Status** &nbsp; `{detail['status']}`")
    cols[0].markdown(f"**Action type** &nbsp; `{detail['action_type']}`")
    cols[1].markdown(f"**Event ID** &nbsp; `{detail['event_id']}`")
    cols[1].markdown(f"**Customer** &nbsp; `{detail['customer_id']}`")
    cols[1].markdown(f"**Updated** &nbsp; `{detail['updated_at']}`")

    if detail.get("reasoning"):
        st.markdown(f"> {detail['reasoning']}")

    st.markdown("**Audit trail**")
    if not detail["audit_trail"]:
        st.caption("(no audit entries)")
        return
    for entry in detail["audit_trail"]:
        st.markdown(
            f"- `{entry['ts']}` &nbsp; **{entry['kind']}** &nbsp; "
            f"by `{entry['actor_kind']}`"
            + (f" ({entry['actor_id']})" if entry.get("actor_id") else "")
        )


def _render_recent_audit(tenant_id: str, limit: int) -> None:
    st.subheader(":scroll: Recent audit log")
    entries = recent_audit(tenant_id, limit=limit)
    if not entries:
        st.caption("No audit entries yet for this tenant.")
        return
    st.dataframe(entries, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Page entry point.
# ---------------------------------------------------------------------------


def main() -> None:
    _set_page_config()
    st.title(PAGE_TITLE)
    st.caption(
        "Pending-action queue, N-of-M quorum progress, and the audit "
        "trail — all reading from the same in-memory state the "
        "orchestrator's `/approvals` API exposes."
    )

    filters = _sidebar()
    tenant_id = filters["tenant_id"]
    if tenant_id is None:
        st.info("Seed demo data from the sidebar to populate the dashboard.")
        return

    left, right = st.columns([2, 1])

    with left:
        selected = _render_pending_queue(tenant_id, filters["voter"])

    with right:
        if selected:
            _render_action_detail(tenant_id, selected)
        else:
            st.info(
                "Click **Details** on any pending row to see the full "
                "audit trail."
            )

    st.divider()
    _render_recent_audit(tenant_id, filters["audit_limit"])


if __name__ == "__main__":  # pragma: no cover — Streamlit entry point
    main()
