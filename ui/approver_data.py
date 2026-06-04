"""Data-adapter layer for the Day-31 approver UI (Phase 6).

Sits between the Streamlit app and the orchestrator pipeline. Three
responsibilities:

  1. Surface the data the dashboard panels need as plain dicts — the
     pending-action queue, recent audit entries, single-action detail
     with the embedded audit trail.
  2. Translate `orchestrator.decision_pipeline` exceptions into a
     single `ApproverActionError` carrying a user-readable message
     so the Streamlit error surface is uniform.
  3. Seed a demo dataset (Jane's mortgage scenario) so a fresh launch
     of the dashboard has something to render. The seeder is idempotent
     — re-running it on a non-empty pipeline is a no-op.

The adapter is **stateless** — it always reaches through to the live
pipeline from `orchestrator.api`. That's what makes both this UI and
the Day-32 demo UI work against the same in-memory state the FastAPI
app exposes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contracts.actions import (
    Action,
    ActionProposal,
    ActionType,
    ProposedBy,
)
from contracts.audit import AuditLogEntry
from orchestrator.approval_queue import (
    ApprovalNotFoundError,
    ApprovalStateError,
    ApproverNotEligibleError,
    DuplicateApproverError,
)
from orchestrator.decision_pipeline import (
    ActionNotFoundError,
    DecisionPipeline,
)


class ApproverActionError(RuntimeError):
    """Single exception type the Streamlit surface needs to catch.

    Carries a `category` so the UI can render an appropriate icon /
    color without branching on the underlying pipeline exception:

      * ``"not_found"``     — action / approval row missing (cross-tenant
                                or stale).
      * ``"already_decided"`` — race: another approver completed quorum
                                or vetoed first.
      * ``"not_eligible"``   — voter outside the eligible-approver pool.
      * ``"duplicate_vote"`` — voter already submitted on this row.
      * ``"unknown"``        — anything we didn't expect; surfaces as a
                                red banner with the raw message.
    """

    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


# ---------------------------------------------------------------------------
# Pipeline accessor — split out so tests can inject a fresh pipeline.
# ---------------------------------------------------------------------------


def _pipeline() -> DecisionPipeline:
    """Return the orchestrator module's live pipeline.

    Module-import surface mirrors the FastAPI app — the Streamlit app
    and the API serve the same state."""
    from orchestrator.api import _get_pipeline_for_tests

    return _get_pipeline_for_tests()


# ---------------------------------------------------------------------------
# Read-side adapters
# ---------------------------------------------------------------------------


def list_tenants() -> list[str]:
    """Tenants that have any action in the pipeline.

    Reads from the audit log (every tenant we've ever seen writes
    there) — the Streamlit dropdown needs *any* tenant id that has
    state, not just tenants with pending rows.
    """
    pipeline = _pipeline()
    seen: dict[str, None] = {}
    for entry in pipeline.audit.all_entries():
        seen.setdefault(entry.tenant_id, None)
    return list(seen.keys())


def list_pending(tenant_id: str) -> list[dict[str, Any]]:
    """Pending-approval rows for `tenant_id`, oldest first.

    Each dict carries the action snapshot + N-of-M quorum progress so
    the UI doesn't have to make a second call per row. Multi-tenant
    invariant (rule 15): we go through the pipeline's
    `list_pending_actions(tenant_id)` which walks the tenant-scoped
    index — never the global action map.
    """
    pipeline = _pipeline()
    rows = []
    for action in pipeline.list_pending_actions(tenant_id):
        rows.append(_action_to_panel_dict(pipeline, action))
    return rows


def recent_audit(tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """The N most recent audit entries for `tenant_id`, newest first.

    `limit` is a soft cap — a tenant with three audit rows returns
    three.
    """
    pipeline = _pipeline()
    entries = pipeline.audit.entries_for_tenant(tenant_id)
    entries = sorted(entries, key=lambda e: e.id or 0, reverse=True)[:limit]
    return [_audit_entry_to_dict(e) for e in entries]


def get_action_detail(
    action_id: str, *, tenant_id: str
) -> dict[str, Any] | None:
    """Full action snapshot — used by the side-panel "details" view.

    Returns ``None`` on miss or cross-tenant access (Day-20
    multi-tenant isolation: the UI doesn't get to even *know* an
    action exists in a different tenant).
    """
    pipeline = _pipeline()
    try:
        action = pipeline.get_action(action_id, expected_tenant_id=tenant_id)
    except ActionNotFoundError:
        return None
    return _action_to_detail_dict(pipeline, action)


# ---------------------------------------------------------------------------
# Write-side adapters — return the post-mutation action panel dict for
# the UI to render. Raise `ApproverActionError` on any pipeline error.
# ---------------------------------------------------------------------------


def approve(
    action_id: str,
    *,
    decided_by: str,
    tenant_id: str,
) -> dict[str, Any]:
    """Submit one approval vote.

    Raises `ApproverActionError` with the appropriate category on every
    failure shape. On success, returns the updated panel dict so the
    UI can refresh the row in place."""
    pipeline = _pipeline()
    try:
        action = pipeline.approve_action(
            action_id, decided_by=decided_by, expected_tenant_id=tenant_id
        )
    except (ActionNotFoundError, ApprovalNotFoundError) as exc:
        raise ApproverActionError("not_found", str(exc)) from exc
    except ApprovalStateError as exc:
        raise ApproverActionError("already_decided", str(exc)) from exc
    except ApproverNotEligibleError as exc:
        raise ApproverActionError("not_eligible", str(exc)) from exc
    except DuplicateApproverError as exc:
        raise ApproverActionError("duplicate_vote", str(exc)) from exc
    return _action_to_panel_dict(pipeline, action)


def reject(
    action_id: str,
    *,
    decided_by: str,
    tenant_id: str,
    reason: str = "",
) -> dict[str, Any]:
    """Veto a pending row. Single eligible reject ends the row.

    Same error-translation contract as `approve` (modulo
    `duplicate_vote` which can't arise on reject)."""
    pipeline = _pipeline()
    try:
        action = pipeline.reject_action(
            action_id,
            reason=reason,
            decided_by=decided_by,
            expected_tenant_id=tenant_id,
        )
    except (ActionNotFoundError, ApprovalNotFoundError) as exc:
        raise ApproverActionError("not_found", str(exc)) from exc
    except ApprovalStateError as exc:
        raise ApproverActionError("already_decided", str(exc)) from exc
    except ApproverNotEligibleError as exc:
        raise ApproverActionError("not_eligible", str(exc)) from exc
    return _action_to_panel_dict(pipeline, action)


# ---------------------------------------------------------------------------
# Demo seeder — gives a freshly-launched dashboard something to render.
# ---------------------------------------------------------------------------


def seed_demo_data(*, force: bool = False) -> dict[str, Any]:
    """Push a small Jane's-mortgage demo dataset through the pipeline.

    Idempotent unless `force=True` — re-running on a non-empty pipeline
    is a no-op. Returns a summary dict (counts of pending rows / audit
    rows / tenants seeded) so the Streamlit "Seed demo data" button can
    render confirmation.

    The dataset exercises:
      * tenant_acme_bank (strict) — `notify_loan_officer` queued for N-of-M
        quorum, two eligible approvers.
      * tenant_globetrek_concierge (permissive) — `send_borrower_message`
        auto-executed.
      * tenant_jefferson_credit — `request_document` single-approver pending.

    Three tenants means the tenant-dropdown shows real options; mixing
    auto + single-approver + quorum means the dashboard renders every
    state shape the UI was designed for.
    """
    pipeline = _pipeline()

    if not force and pipeline.audit.all_entries():
        # Non-empty pipeline — leave it alone.
        return {
            "seeded": False,
            "reason": "pipeline already contains data; pass force=True to reseed",
            "pending": len(pipeline.queue._by_id),  # type: ignore[attr-defined]
            "audit": len(pipeline.audit.all_entries()),
        }

    # Wipe before reseed so re-runs are deterministic.
    if force:
        pipeline.clear()

    # Tenant policies — necessary so the pipeline doesn't fall through
    # to the safe APPROVAL_REQUIRED default for every action_type.
    pipeline.policy.set_policies(
        "tenant_acme_bank",
        {
            "send_borrower_message": "auto",
            "notify_loan_officer": "approval_required",
            "request_document": "approval_required",
        },
    )
    pipeline.policy.set_policies(
        "tenant_globetrek_concierge",
        {
            "send_borrower_message": "auto",
            "notify_loan_officer": "auto",
            "request_document": "approval_required",
        },
    )
    pipeline.policy.set_policies(
        "tenant_jefferson_credit",
        {
            "send_borrower_message": "auto",
            "notify_loan_officer": "approval_required",
            "request_document": "approval_required",
        },
    )

    # Arm the acme_bank notify_loan_officer scenario as a 2-of-3 quorum
    # so the UI's N-of-M progress block has real data to render.
    pipeline.quorum.set_rule(
        tenant_id="tenant_acme_bank",
        action_type=ActionType.NOTIFY_LOAN_OFFICER,
        required_approvals=2,
        eligible_approvers=["compliance_alice", "risk_bob", "legal_carol"],
    )

    proposals = [
        ActionProposal(
            id="pp_demo_acme_1",
            tenant_id="tenant_acme_bank",
            event_id="evt_demo_acme_1",
            customer_id="cus_jane_doe",
            action_type=ActionType.SEND_BORROWER_MESSAGE,
            proposed_by=ProposedBy.LLM,
            payload={"_planner_reasoning": "acknowledge paystub upload"},
        ),
        ActionProposal(
            id="pp_demo_acme_2",
            tenant_id="tenant_acme_bank",
            event_id="evt_demo_acme_2",
            customer_id="cus_jane_doe",
            action_type=ActionType.NOTIFY_LOAN_OFFICER,
            proposed_by=ProposedBy.LLM,
            payload={
                "_planner_reasoning": "income verification anomaly above threshold"
            },
        ),
        ActionProposal(
            id="pp_demo_globe_1",
            tenant_id="tenant_globetrek_concierge",
            event_id="evt_demo_globe_1",
            customer_id="cus_alex_traveler",
            action_type=ActionType.SEND_BORROWER_MESSAGE,
            proposed_by=ProposedBy.FALLBACK,
            payload={"_planner_reasoning": "auto-reply to chat ping"},
        ),
        ActionProposal(
            id="pp_demo_jeff_1",
            tenant_id="tenant_jefferson_credit",
            event_id="evt_demo_jeff_1",
            customer_id="cus_chris_borrower",
            action_type=ActionType.REQUEST_DOCUMENT,
            proposed_by=ProposedBy.LLM,
            payload={"_planner_reasoning": "missing W-2 for prior year"},
        ),
    ]
    for proposal in proposals:
        pipeline.handle_proposal(proposal)

    return {
        "seeded": True,
        "tenants": [
            "tenant_acme_bank",
            "tenant_globetrek_concierge",
            "tenant_jefferson_credit",
        ],
        "pending": len(pipeline.queue._by_id),  # type: ignore[attr-defined]
        "audit": len(pipeline.audit.all_entries()),
    }


# ---------------------------------------------------------------------------
# Internal dict-shaping helpers
# ---------------------------------------------------------------------------


def _action_to_panel_dict(
    pipeline: DecisionPipeline, action: Action
) -> dict[str, Any]:
    """Compact dict the queue table renders one row from."""
    progress = _approval_progress(pipeline, action)
    return {
        "action_id": action.id,
        "tenant_id": action.tenant_id,
        "event_id": action.event_id,
        "customer_id": action.customer_id,
        "action_type": action.action_type.value,
        "status": action.status.value,
        "reasoning": action.payload.get("_planner_reasoning", ""),
        "created_at": action.created_at.isoformat(),
        "updated_at": action.updated_at.isoformat(),
        "approval_progress": progress,
    }


def _action_to_detail_dict(
    pipeline: DecisionPipeline, action: Action
) -> dict[str, Any]:
    """Full dict the "details" panel renders. Includes the embedded
    audit trail so a reviewer sees every transition without a second
    call."""
    base = _action_to_panel_dict(pipeline, action)
    base["audit_trail"] = [
        _audit_entry_to_dict(e) for e in pipeline.audit_for_action(action.id)
    ]
    base["payload"] = {
        k: v for k, v in action.payload.items() if not k.startswith("_")
    }
    return base


def _approval_progress(
    pipeline: DecisionPipeline, action: Action
) -> dict[str, Any] | None:
    """N-of-M quorum progress for the action's queue row, or ``None``
    if the action was never queued."""
    rule = pipeline.approval_rule(action.id)
    if rule is None:
        return None
    return {
        "state": rule.state,
        "required_approvals": rule.required_approvals,
        "approvals_recorded": len(rule.approvals),
        "approvals_remaining": rule.approvals_remaining,
        "approvers": list(rule.approvals),
        "eligible_approvers": (
            list(rule.eligible_approvers)
            if rule.eligible_approvers is not None
            else None
        ),
    }


def _audit_entry_to_dict(entry: AuditLogEntry) -> dict[str, Any]:
    """Compact dict the audit-log panel renders one row from."""
    return {
        "id": entry.id,
        "kind": entry.kind.value,
        "tenant_id": entry.tenant_id,
        "action_id": entry.action_id,
        "event_id": entry.caused_by_event_id,
        "actor_kind": entry.actor_kind.value,
        "actor_id": entry.actor_id,
        "ts": entry.ts.isoformat(),
        "payload": dict(entry.payload),
    }


# ---------------------------------------------------------------------------
# Dataclass surface for the Streamlit app's filter state. Keeps the
# top-of-file imports in approver_app.py readable.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApproverFilters:
    """Filter state the Streamlit sidebar reads + writes."""

    tenant_id: str
    audit_limit: int = 20
    show_resolved: bool = False
