"""orchestrator FastAPI app.

Day 4 surface: `GET /` + `/healthz` + `/readyz` (scaffold).
Day 8 surface: wires the event listener at app startup, adds
`GET /events/recent` for diagnostic visibility into what the listener has
received, and upgrades `/readyz` to probe Postgres + Redis when their
respective env vars (`DATABASE_URL`, `REDIS_URL`) are set.
Day 9 surface: `GET /proposals/recent` mirrors `/events/recent` for the
planner's output.
Day 10 surface (this file): the policy + queue + executor + audit
pipeline lives behind `_pipeline`. The planner handler in the listener
chain now forwards proposals into `_pipeline.handle_proposal()` so every
inbound event runs the full decision flow. `GET /approvals` lists the
tenant's pending queue; `POST /approvals/{action_id}/approve` and
`POST /approvals/{action_id}/reject` resolve a pending row. The
`/actions/{id}` endpoint exposes the full action + audit-trail view.

Listener selection mirrors the context-engine's `DATABASE_URL` switch:

  * `REDIS_URL` set → `RedisEventListener` (production / docker-compose).
  * `REDIS_URL` unset → `InMemoryEventListener` armed against an attached
    `InMemoryEventBus`. Tests inject the same bus the context-engine app
    uses so a publish on one side fires the orchestrator handler synchronously.

Multi-tenant invariant (rule 15): every tenant-scoped endpoint takes a
mandatory `?tenant_id=` — there is no implicit "all tenants" view. The
pipeline + queue + audit log enforce tenant isolation at the data layer
on top of the URL discipline.

Audit invariant (rule 16): every action proposal, approval, rejection,
and execution writes to `_pipeline.audit`. The pipeline is the source of
truth for compliance review; the bounded diagnostic buffers
(`/events/recent`, `/proposals/recent`) exist for operator visibility,
not for audit.
"""

from __future__ import annotations

import os
import warnings
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Response, status

from context_engine.event_bus import InMemoryEventBus
from context_engine.llm import get_client
from contracts.actions import Action
from contracts.build_info import build_info
from orchestrator.approval_queue import (
    ApprovalNotFoundError,
    ApprovalStateError,
    ApproverNotEligibleError,
    DuplicateApproverError,
)
from orchestrator.decision_pipeline import (
    ActionNotFoundError,
    DecisionPipeline,
    make_default_pipeline,
)
from orchestrator.event_listener import (
    EventListener,
    InMemoryEventListener,
    RecentEventsBuffer,
    RedisEventListener,
    make_buffered_handler,
)
from orchestrator.planner import (
    ProposalsBuffer,
    chain_handlers,
    make_planning_handler,
)

load_dotenv()


# ----------------------------------------------------------------------------
# Module-level singletons. The listener and recent-events buffer outlive any
# single request, so they live on the module — same pattern the context-engine
# uses for its repositories. Tests use `attach_in_memory_bus` to swap in a
# fresh bus + listener per test session.
# ----------------------------------------------------------------------------

_recent_events: RecentEventsBuffer = RecentEventsBuffer()
_recent_proposals: ProposalsBuffer = ProposalsBuffer()
_pipeline: DecisionPipeline = make_default_pipeline()
_listener: EventListener | None = None
_listener_mode: str = "unattached"


def _compose_handler() -> Any:
    """Compose the Day-8 buffered handler with the Day-9 planner +
    Day-10 pipeline.

    Chained so a bug in any handler doesn't stop the others from
    running (the listener invariant — handler exceptions are isolated).
    The planning handler is the one wired to the pipeline; the buffered
    handler stays alongside it for the `/events/recent` diagnostic view.
    """
    return chain_handlers(
        make_buffered_handler(_recent_events),
        make_planning_handler(_recent_proposals, pipeline=_pipeline),
    )


def _build_default_listener() -> tuple[EventListener | None, str]:
    """Decide which listener to construct based on env.

    Returns `(listener_or_None, mode)`. `mode` is one of:
      * `"redis"`     — REDIS_URL set; production / docker-compose path.
      * `"unattached"` — no listener pre-built; tests / single-process
                          scenarios will call `attach_in_memory_bus`
                          to supply one bound to a shared `InMemoryEventBus`.
    """
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return None, "unattached"

    # Lazy import — keeps the unit-test path free of the redis dep cost
    # (although the package is in requirements.txt for the prod image).
    import redis  # noqa: PLC0415

    client = redis.Redis.from_url(redis_url, decode_responses=True)
    return RedisEventListener(client=client), "redis"


def attach_in_memory_bus(bus: InMemoryEventBus) -> None:
    """Bind an `InMemoryEventListener` to the supplied bus.

    Used by the Day-11 end-to-end scenario and by unit tests that need
    publish-on-context-engine → handle-on-orchestrator semantics in one
    process. Idempotent: re-attaching the same bus is a no-op; attaching
    a different bus stops the existing listener first.
    """
    global _listener, _listener_mode
    if _listener is not None:
        _listener.stop()
    _listener = InMemoryEventListener(bus)
    _listener.start(_compose_handler())
    _listener_mode = "in-memory"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Start the listener at app boot, stop it at shutdown.

    In `unattached` mode (no REDIS_URL, no test-side bus injection yet),
    the lifespan is a no-op and `/events/recent` simply returns an empty
    list until something publishes through an attached bus. This keeps
    the TestClient path zero-config.
    """
    global _listener, _listener_mode
    if _listener is None:
        built, mode = _build_default_listener()
        _listener = built
        _listener_mode = mode
    if _listener is not None and _listener_mode == "redis":
        # In-memory listeners are armed at attach time, not at lifespan
        # start, so a test that attaches a bus before issuing requests
        # gets handler invocations even before the first lifespan event.
        _listener.start(_compose_handler())
    try:
        yield
    finally:
        if _listener is not None:
            _listener.stop()


app = FastAPI(
    title="PennyCore — orchestrator",
    description=(
        "Decision-maker. Listens to events, asks the LLM what to do, applies "
        "tenant-specific policy rules, manages a human-approval queue, "
        "prevents double-action on duplicate events, writes the audit trail."
    ),
    version="0.2.0",
    lifespan=lifespan,
)


# ----------------------------------------------------------------------------
# Meta + health endpoints.
# ----------------------------------------------------------------------------


def _llm_mode() -> str:
    """Resolve the active LLM mode via the dispatch layer (same logic
    `context_engine/api.py` uses). Suppressed warnings on the hot path —
    the dispatch layer logs once on the first real call site."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return get_client().name


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {
        "service": "orchestrator",
        "version": app.version,
        "llm_mode": _llm_mode(),
        "status": "mvp",
        "phase": "2-mvp-build",
        "listener_mode": _listener_mode,
    }


@app.get("/healthz", tags=["health"])
def healthz() -> dict[str, str]:
    """Liveness probe — the process is up. No external dependencies probed."""
    return {"status": "ok"}


@app.get("/info", tags=["meta"])
def info() -> dict[str, Any]:
    """Self-describe the running container (Day 29 surface).

    Surfaces the build metadata baked in by `Dockerfile.prod` so a
    deployed instance can identify itself — git SHA, build date, image
    version. Dev containers respond with ``mode="dev"`` and the
    placeholder ``"unknown"`` values so a caller can distinguish a
    local boot from a real prod image.
    """
    return {"service": "orchestrator", **build_info()}


@app.get("/readyz", tags=["health"])
def readyz(response: Response) -> dict[str, Any]:
    """Readiness probe.

    Probes Postgres if `DATABASE_URL` is set, Redis if `REDIS_URL` is set.
    A failed probe returns 503 so kube/docker readiness gates fail-closed.
    With neither configured the orchestrator runs in scaffold/in-memory
    mode and is always ready.
    """
    database_url = os.getenv("DATABASE_URL", "").strip()
    redis_url = os.getenv("REDIS_URL", "").strip()

    probed: dict[str, str] = {}
    failures: dict[str, str] = {}

    if database_url:
        try:
            # Lazy import — only paid when DATABASE_URL is set.
            from context_engine.pg_customer_repository import make_connection_factory
            from context_engine.pg_repository import PgEventRepository

            factory = make_connection_factory(database_url)
            PgEventRepository(factory).count()
            probed["postgres"] = "ok"
        except Exception as exc:
            failures["postgres"] = f"{type(exc).__name__}: {exc}"

    if redis_url:
        try:
            import redis  # noqa: PLC0415

            client = redis.Redis.from_url(redis_url, decode_responses=True)
            client.ping()
            probed["redis"] = "ok"
        except Exception as exc:
            failures["redis"] = f"{type(exc).__name__}: {exc}"

    if failures:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "not_ready",
            "datastores_probed": True,
            "probed": probed,
            "failures": failures,
            "listener_mode": _listener_mode,
        }

    return {
        "status": "ready",
        "datastores_probed": bool(probed),
        "probed": probed,
        "listener_mode": _listener_mode,
    }


# ----------------------------------------------------------------------------
# /events/recent — diagnostic surface for the Day-8 listener.
# ----------------------------------------------------------------------------


@app.get("/events/recent", tags=["events"])
def events_recent(
    tenant_id: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Return the most recent envelopes the listener has received for `tenant_id`.

    Multi-tenant invariant: `tenant_id` is mandatory; there is no implicit
    "all tenants" view. The buffer is bounded per-tenant so a noisy
    tenant cannot push another tenant's traffic out of view.

    Use cases:
      * Smoke-testing the bus wiring during local dev
      * Powering the Day-31 admin UI's "recent events" panel
      * Asserting in end-to-end tests that ingestion → orchestrator works
    """
    if _listener is None:
        # No listener attached → nothing has been buffered. Return an empty
        # page rather than 503; readyz is the right place to flag missing
        # listener configuration, not this diagnostic endpoint.
        return {
            "tenant_id": tenant_id,
            "limit": limit,
            "count": 0,
            "events": [],
            "listener_mode": _listener_mode,
        }

    items = _recent_events.recent(tenant_id=tenant_id, limit=limit)
    return {
        "tenant_id": tenant_id,
        "limit": limit,
        "count": len(items),
        "events": items,
        "listener_mode": _listener_mode,
    }


# ----------------------------------------------------------------------------
# /proposals/recent — diagnostic surface for the Day-9 planner.
#
# Mirrors /events/recent. Mandatory tenant_id, bounded per-tenant ring
# buffer. The Day-10 audit log is the canonical record; this endpoint
# exists for operator visibility during Phase 2 development.
# ----------------------------------------------------------------------------


@app.get("/proposals/recent", tags=["proposals"])
def proposals_recent(
    tenant_id: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """Return the most recent action proposals the planner has emitted
    for `tenant_id`.

    Multi-tenant invariant: `tenant_id` is mandatory; tenants can never
    see each other's proposals via this endpoint.

    Each proposal includes `proposed_by` (`llm` | `fallback`) so the
    operator can spot degraded planning at a glance — a streak of
    `fallback` proposals signals either an LLM outage or a prompt
    regression.
    """
    items = _recent_proposals.recent(tenant_id=tenant_id, limit=limit)
    serialized = [p.model_dump(mode="json") for p in items]
    return {
        "tenant_id": tenant_id,
        "limit": limit,
        "count": len(serialized),
        "proposals": serialized,
        "listener_mode": _listener_mode,
    }


# ----------------------------------------------------------------------------
# Action serialization helper. Centralized so /approvals, /actions, and the
# takehome adapter all surface the same shape — diff-friendly across surfaces.
# ----------------------------------------------------------------------------


def _action_view(action: Action) -> dict[str, Any]:
    """Render an `Action` for the HTTP surface, denormalizing the audit
    trail and exposing the planner reasoning as a top-level field.

    The `audit_trail` is a list of audit entries scoped to this action,
    in insertion order — gives the admin UI / external reviewer a
    one-call "what happened, when, by whom" view. The full audit log is
    still available via `/audit/...` (lands Day 23 hardening); this
    embedded view is the convenience surface.
    """
    audit_entries = _pipeline.audit_for_action(action.id)
    audit_trail = [e.model_dump(mode="json") for e in audit_entries]
    payload = dict(action.payload)
    reasoning = payload.pop("_planner_reasoning", "")
    executed_payload = payload.pop("_executed_payload", None)
    return {
        "action_id": action.id,
        "tenant_id": action.tenant_id,
        "event_id": action.event_id,
        "proposal_id": action.proposal_id,
        "customer_id": action.customer_id,
        "action_type": action.action_type.value,
        "status": action.status.value,
        "reasoning": reasoning,
        "payload": payload,
        "executed_payload": executed_payload,
        "created_at": action.created_at.isoformat(),
        "updated_at": action.updated_at.isoformat(),
        "executed_at": (
            action.executed_at.isoformat() if action.executed_at else None
        ),
        "approval_progress": _approval_progress(action),
        "audit_trail": audit_trail,
    }


def _approval_progress(action: Action) -> dict[str, Any] | None:
    """Render the N-of-M quorum tally for an action's queue row, or
    ``None`` if the action was never queued for approval.

    The admin UI's "approve next" panel (Day 31) renders this to show
    "1 of 3 approvals — waiting on legal, risk" without a second
    round-trip. For a single-approver action the block still appears
    (`required=1`), so the front-end has one uniform shape to read.
    """
    rule = _pipeline.approval_rule(action.id)
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


# ----------------------------------------------------------------------------
# /approvals — list / approve / reject pending actions for a tenant.
# ----------------------------------------------------------------------------


@app.get("/approvals", tags=["approvals"])
def approvals_list(
    tenant_id: str = Query(..., min_length=1, max_length=64),
) -> dict[str, Any]:
    """List pending-approval actions for `tenant_id`.

    Returns FIFO order — oldest first, which is what the admin UI's
    "approve next" workflow wants.
    """
    pending = _pipeline.list_pending_actions(tenant_id)
    return {
        "tenant_id": tenant_id,
        "count": len(pending),
        "approvals": [_action_view(a) for a in pending],
    }


@app.post("/approvals/{action_id}/approve", tags=["approvals"])
def approvals_approve(
    action_id: str,
    decided_by: str = Query("api", min_length=1, max_length=128),
    tenant_id: str | None = Query(default=None, min_length=1, max_length=64),
) -> dict[str, Any]:
    """Record one approval vote on a pending action.

    For a single-approver action the vote executes it immediately. For an
    N-of-M action the vote is recorded; the action only executes on the
    vote that completes the quorum. Either way the response embeds
    `approval_progress`, so the caller sees how many more approvals are
    needed. `decided_by` identifies the approver — required to be distinct
    per quorum seat.

    Multi-tenant invariant (rule 15): when `tenant_id` is supplied the
    pipeline refuses any action that belongs to a different tenant —
    response is a plain 404 with no leak that the action exists
    elsewhere. Callers without a `tenant_id` get the legacy global
    lookup (back-compat for in-process callers like the takehome
    adapter, which is single-tenant per scenario). The Day 20 hardening
    tests cover both paths.

    Errors:
      * 403 — `decided_by` is not in the action's eligible-approver pool.
      * 404 — no such action OR cross-tenant attempt with `tenant_id`
        supplied OR no pending row (already resolved / never queued).
      * 409 — the action is already resolved (race — another approver
        completed quorum first) OR `decided_by` already voted on it.
    """
    try:
        action = _pipeline.approve_action(
            action_id, decided_by=decided_by, expected_tenant_id=tenant_id
        )
    except (ActionNotFoundError, ApprovalNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApproverNotEligibleError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (ApprovalStateError, DuplicateApproverError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _action_view(action)


@app.post("/approvals/{action_id}/reject", tags=["approvals"])
def approvals_reject(
    action_id: str,
    reason: str = Query("", max_length=2048),
    decided_by: str = Query("api", min_length=1, max_length=128),
    tenant_id: str | None = Query(default=None, min_length=1, max_length=64),
) -> dict[str, Any]:
    """Reject a pending action — a veto. A single eligible rejection
    resolves the action to `rejected` regardless of how many approvals
    have accumulated, and writes the rejection audit row.

    Same error contract as `/approvals/{action_id}/approve` (including the
    403 eligible-approver guard and the optional `tenant_id` cross-tenant
    guard); a duplicate-approver error can't arise on reject because one
    veto ends the row.
    """
    try:
        action = _pipeline.reject_action(
            action_id,
            reason=reason,
            decided_by=decided_by,
            expected_tenant_id=tenant_id,
        )
    except (ActionNotFoundError, ApprovalNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ApproverNotEligibleError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ApprovalStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _action_view(action)


# ----------------------------------------------------------------------------
# /actions — single-action lookup with full audit trail.
# ----------------------------------------------------------------------------


@app.get("/actions/{action_id}", tags=["actions"])
def actions_get(
    action_id: str,
    tenant_id: str | None = Query(default=None, min_length=1, max_length=64),
) -> dict[str, Any]:
    """Fetch one action by ID. The audit trail is embedded.

    Multi-tenant invariant (rule 15): when `tenant_id` is supplied, an
    action owned by a different tenant returns a plain 404 — the
    response leaks nothing about whether the id exists for another
    tenant. With no `tenant_id`, behaves as the legacy global lookup
    (back-compat for the takehome adapter, which is single-tenant per
    scenario by construction).

    Returns 404 if the action is unknown to this pipeline instance OR
    if `tenant_id` is supplied and the action belongs to a different
    tenant.
    """
    try:
        action = _pipeline.get_action(action_id, expected_tenant_id=tenant_id)
    except ActionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _action_view(action)


# ----------------------------------------------------------------------------
# Test-only helpers. Not exported through OpenAPI; callers reach in through
# the Python module path. Keeping these here (rather than in `tests/`) so the
# in-memory wiring story is documented next to the production wiring.
# ----------------------------------------------------------------------------


def _reset_listener_for_tests() -> None:
    """Drop the listener and clear the recent-events/proposals buffers
    and the decision pipeline.

    The Day-11 end-to-end test scaffolding uses this between scenarios to
    keep state from one test from leaking into the next. NOT for
    production callers.
    """
    global _listener, _listener_mode
    if _listener is not None:
        _listener.stop()
        _listener = None
    _listener_mode = "unattached"
    _recent_events.clear()
    _recent_proposals.clear()
    _pipeline.clear()


def _current_listener_mode() -> str:
    """Test introspection — returns the resolved listener mode label."""
    return _listener_mode


def _get_pipeline_for_tests() -> DecisionPipeline:
    """Test introspection — direct handle on the module-level pipeline
    so unit tests can configure policies + drive the planner without
    going through the HTTP surface."""
    return _pipeline
