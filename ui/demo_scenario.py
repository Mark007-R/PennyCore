"""Scenario builder for the Day-32 demo UI (Jane's mortgage journey).

Separated from the Streamlit page so the timeline, finding references,
and scenario-state builder are unit-testable.

What lives here:

  * **`JANE_TIMELINE`** — ordered list of step dicts the UI walks
    through. Each step has a title, plain-English description, and
    (optionally) a `finding` key linking to a Phase-3 or Phase-5
    measured result.
  * **`PHASE_FINDINGS`** — dict of finding cards (phase, title, body,
    in-repo reference) the timeline links to. Reading
    `PHASE_FINDINGS` is the single-source-of-truth way the
    Phase 5 wrap insights surface in the UI.
  * **`build_scenario_state()`** — pushes Jane's events through the
    live pipeline (idempotent: keyed by event_id, so re-runs don't
    double-up) and returns a state snapshot used by the Streamlit
    panels.

The scenario is intentionally small — three events, three actions,
the quorum row + the auto-execute row + the rejection-then-approval
flow. Enough to render every state shape the Phase-3 / Phase-5
findings describe, no more.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from contracts import ChannelType, Event, EventType
from contracts.actions import (
    ActionProposal,
    ActionType,
    ProposedBy,
)


SCENARIO_TENANT = "tenant_acme_bank"
SCENARIO_CUSTOMER = "cus_jane_doe"

JANE_PAYSTUB_EVENT_ID = "evt_jane_paystub_upload"
JANE_PAYSTUB_ACTION_ID_HINT = "send_borrower_message"  # auto-executed
JANE_ANOMALY_EVENT_ID = "evt_jane_income_anomaly"
JANE_ANOMALY_ACTION_TYPE = ActionType.NOTIFY_LOAN_OFFICER

# ---------------------------------------------------------------------------
# Phase findings the timeline links to. Single source of truth — the UI
# reads this dict; future findings only need a new entry here.
# ---------------------------------------------------------------------------

PHASE_FINDINGS: dict[str, dict[str, Any]] = {
    "hybrid_retrieval": {
        "phase": 5,
        "title": "Hybrid retrieval — 4× cheaper than naive on long histories",
        "body": (
            "On the 200-pair benchmark, hybrid retrieval (recency + "
            "semantic + summarized cold tail) matched naive-baseline "
            "answer quality on the 50 fact-bearing mortgage queries "
            "while running 4× cheaper on the `very_long` history "
            "bucket and 24% cheaper aggregate. The cost gap widens "
            "as the customer's history grows — exactly the shape a "
            "production system needs."
        ),
        "reference": "reports/day27_phase5_report.md + results/phase5_naive_vs_champion_context_engine.json",
    },
    "declarative_pareto": {
        "phase": 3,
        "title": "Declarative YAML policy — strict Pareto winner over naive LLM",
        "body": (
            "Declarative beats naive on every axis: correctness "
            "(100% vs 54%), latency (0.6 µs vs 1.8 µs), prod cost "
            "($0 vs $0.57/100), auditability (5 vs 2). Naive's "
            "failure mode is tenant-asymmetric — over-approves the "
            "strict tenant (compliance bug) AND over-rejects the "
            "permissive tenants (UX bug). A tenant-agnostic heuristic "
            "can't satisfy tenants with opposite rules."
        ),
        "reference": "reports/day28_phase5_report.md + results/phase5_naive_vs_champion_orchestrator.json",
    },
    "quorum_four_eyes": {
        "phase": 5,
        "title": "N-of-M quorum with veto asymmetry — Day 26",
        "body": (
            "Approval rows can be armed for N distinct sign-offs "
            "from an M-eligible pool. One eligible reject vetoes the "
            "row regardless of partial approvals (four-eyes "
            "asymmetry). Duplicate voters can't fill two seats. "
            "Defaults stay single-approver so the takehome adapter "
            "6/6 score is untouched."
        ),
        "reference": "reports/day26_phase5_report.md + tests/unit/test_quorum_approval_queue.py",
    },
    "audit_trail": {
        "phase": 2,
        "title": "Every transition writes audit — Day 10 invariant 16",
        "body": (
            "Every action proposal, decision, approval, rejection, "
            "and execution writes an `AuditLogEntry`. The audit log "
            "is the source of truth for compliance review — "
            "reconstructing 'who decided what and when' is one "
            "tenant-scoped query."
        ),
        "reference": "orchestrator/decision_pipeline.py §AuditKind",
    },
    "tracing_spine": {
        "phase": 6,
        "title": "Tenant-aware OTel spans across the event flow — Day 30",
        "body": (
            "Every span on the ingest → decide → execute path carries "
            "`pennycore.tenant_id` as a first-class attribute. Multi-"
            "tenant isolation (rule 15) becomes a Jaeger tag filter, "
            "not a documentation invariant."
        ),
        "reference": "contracts/observability.py + results/samples/day30_phase6_jaeger_trace.png",
    },
}


# ---------------------------------------------------------------------------
# Timeline. UI scrubs through this; each step optionally links a finding
# card + names the action_id its snapshot panel should show.
# ---------------------------------------------------------------------------

JANE_TIMELINE: list[dict[str, Any]] = [
    {
        "title": "Jane uploads a paystub",
        "description": (
            "A `document_uploaded` event lands on the context-engine "
            "ingestion API for tenant_acme_bank. The event is stored, "
            "deduped on `(tenant_id, idempotency_key)`, and published "
            "on the bus."
        ),
        "show_event": True,
    },
    {
        "title": "Context-engine assembles a brief",
        "description": (
            "Hybrid retrieval pulls Jane's last 24h messages, the "
            "semantic-relevant slice from older history, and a "
            "summary of the cold tail. The packer fits the segments "
            "into the token budget."
        ),
        "show_brief": True,
        "finding": "hybrid_retrieval",
    },
    {
        "title": "Orchestrator decides — auto-acknowledge",
        "description": (
            "Declarative policy for tenant_acme_bank says "
            "`send_borrower_message: auto`. The pipeline auto-"
            "executes the acknowledgement and writes the audit row. "
            "No queue stop, no human in the loop — exactly what the "
            "Phase-3 declarative champion is for."
        ),
        "action_kind": "ack",
        "finding": "declarative_pareto",
    },
    {
        "title": "Income anomaly triggers an approval-required action",
        "description": (
            "A follow-up `anomaly_detected` event proposes "
            "`notify_loan_officer`. The same declarative engine "
            "returns `approval_required` for this action type on "
            "tenant_acme_bank — a different decision for a different "
            "action, same tenant, same policy file."
        ),
        "action_kind": "anomaly",
        "finding": "audit_trail",
    },
    {
        "title": "2-of-3 quorum signed off",
        "description": (
            "The notify_loan_officer row is armed for a 2-of-3 quorum "
            "across compliance_alice, risk_bob, and legal_carol. "
            "Alice signs first (partial); Bob completes the quorum; "
            "the executor runs the action and the audit trail closes."
        ),
        "action_kind": "anomaly",
        "finding": "quorum_four_eyes",
    },
    {
        "title": "Every step is one Jaeger trace",
        "description": (
            "Ingest, decide, execute — all three spans carry "
            "`pennycore.tenant_id` and roll up under one trace per "
            "event. The static screenshot in `results/samples/"
            "day30_phase6_jaeger_trace.png` is rendered from real "
            "captured spans."
        ),
        "finding": "tracing_spine",
    },
]


# ---------------------------------------------------------------------------
# State builder. Idempotent — uses fixed event_id + idempotency_key so
# re-runs against the same pipeline don't double-stack rows.
# ---------------------------------------------------------------------------


def build_scenario_state() -> dict[str, Any]:
    """Push Jane's events through the live pipeline if absent; return
    a state snapshot the UI panels render against.

    Returns a dict shaped like:
        {
            "paystub_event": {...envelope dict...},
            "brief": {strategy, body, tokens, budget},
            "ack_action_id": str | None,
            "anomaly_action_id": str | None,
        }

    Idempotent: keyed on event_id, so re-running on a pipeline already
    holding Jane's events returns the existing action ids.
    """
    from orchestrator.api import _get_pipeline_for_tests
    from ui.approver_data import seed_demo_data

    seed_demo_data()  # ensures tenant policies + quorum rule exist
    pipeline = _get_pipeline_for_tests()

    paystub_envelope = _push_paystub_event(pipeline)
    ack_action_id = _push_ack_proposal(pipeline)
    anomaly_action_id = _push_anomaly_proposal(pipeline)

    # Hard-coded brief for the timeline panel. The Phase-5 hybrid
    # strategy is the documented champion; the body is the kind of
    # token-budgeted output the assembler emits.
    brief = {
        "strategy": "hybrid (Phase-5 champion)",
        "tokens": 412,
        "budget": 512,
        "body": (
            "## Customer brief — Jane Doe (cus_jane_doe)\n"
            "### Recent (last 24h)\n"
            "- 2026-06-02 14:32 — email: 'sending paystub for May'\n"
            "- 2026-06-02 14:33 — paystub.pdf uploaded (this event)\n"
            "### Semantic-relevant older history\n"
            "- 2026-05-18 — discussed income verification timeline\n"
            "- 2026-05-09 — confirmed employer name (ACME Tech Co.)\n"
            "### Cold-tail summary\n"
            "- Application opened 2026-04-04. Pre-approval granted "
            "  2026-04-10. Conditional approval contingent on income "
            "  verification and clean credit pull."
        ),
    }

    # Stamp the timeline with the resolved action ids so the panel
    # builder can find the right action_id for each step.
    for step in JANE_TIMELINE:
        if step.get("action_kind") == "ack":
            step["action_id"] = ack_action_id
        elif step.get("action_kind") == "anomaly":
            step["action_id"] = anomaly_action_id

    return {
        "paystub_event": paystub_envelope,
        "brief": brief,
        "ack_action_id": ack_action_id,
        "anomaly_action_id": anomaly_action_id,
    }


def _push_paystub_event(pipeline: Any) -> dict[str, Any]:
    """Push the `document_uploaded` event through the ingestion API.
    Returns a serialisable dict for the UI to render."""
    from context_engine.event_bus import InMemoryEventBus
    from context_engine.ingestion import ingest_event
    from context_engine.repository import InMemoryEventRepository

    # Local bus / repo — the scenario UI doesn't share the api's
    # in-memory store, but the orchestrator pipeline (where the
    # actions land) IS shared. That's the bit we care about.
    repo = InMemoryEventRepository()
    bus = InMemoryEventBus()
    event = Event(
        id=JANE_PAYSTUB_EVENT_ID,
        tenant_id=SCENARIO_TENANT,
        customer_id=SCENARIO_CUSTOMER,
        channel_code=ChannelType.EMAIL,
        event_type=EventType.DOCUMENT_UPLOADED,
        idempotency_key="idem_jane_paystub_v1",
        payload={
            "document_type": "paystub",
            "document_url": "s3://acme-mortgage/jane/may-paystub.pdf",
            "filename": "may-paystub.pdf",
        },
        received_at=datetime.now(timezone.utc),
    )
    ingest_event(event, repo=repo, bus=bus)
    return {
        "event_id": event.id,
        "tenant_id": event.tenant_id,
        "customer_id": event.customer_id,
        "channel_code": event.channel_code.value,
        "event_type": event.event_type.value,
        "idempotency_key": event.idempotency_key,
        "payload": event.payload,
    }


def _push_ack_proposal(pipeline: Any) -> str:
    """Push the auto-execute send_borrower_message proposal. Returns
    the resulting action_id (looked up via dedup if already
    processed)."""
    proposal = ActionProposal(
        id="pp_jane_ack",
        tenant_id=SCENARIO_TENANT,
        event_id=JANE_PAYSTUB_EVENT_ID,
        customer_id=SCENARIO_CUSTOMER,
        action_type=ActionType.SEND_BORROWER_MESSAGE,
        proposed_by=ProposedBy.LLM,
        payload={
            "_planner_reasoning": (
                "Acknowledge paystub receipt and inform Jane it will be "
                "reviewed within one business day."
            ),
        },
    )
    actions = pipeline.handle_proposal(proposal)
    return actions[0].id


def _push_anomaly_proposal(pipeline: Any) -> str:
    """Push the approval-required notify_loan_officer proposal. The
    quorum rule + tenant policy are already set up by seed_demo_data."""
    proposal = ActionProposal(
        id="pp_jane_anomaly",
        tenant_id=SCENARIO_TENANT,
        event_id=JANE_ANOMALY_EVENT_ID,
        customer_id=SCENARIO_CUSTOMER,
        action_type=JANE_ANOMALY_ACTION_TYPE,
        proposed_by=ProposedBy.LLM,
        payload={
            "_planner_reasoning": (
                "Income on May paystub deviates 18% from the application's "
                "stated income — above the auto-flag threshold."
            ),
        },
    )
    actions = pipeline.handle_proposal(proposal)
    return actions[0].id
