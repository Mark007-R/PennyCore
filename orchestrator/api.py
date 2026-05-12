"""orchestrator FastAPI app.

Day 4 surface: `GET /` + `/healthz` + `/readyz` (scaffold).
Day 8 surface (this file): wires the event listener at app startup, adds
`GET /events/recent` for diagnostic visibility into what the listener has
received, and upgrades `/readyz` to probe Postgres + Redis when their
respective env vars (`DATABASE_URL`, `REDIS_URL`) are set.

Listener selection mirrors the context-engine's `DATABASE_URL` switch:

  * `REDIS_URL` set → `RedisEventListener` (production / docker-compose).
  * `REDIS_URL` unset → `InMemoryEventListener` armed against an attached
    `InMemoryEventBus`. Tests inject the same bus the context-engine app
    uses so a publish on one side fires the orchestrator handler synchronously.

Multi-tenant invariant (rule 15): `/events/recent` is mandatory `?tenant_id=`
— there is no implicit "all tenants" view. The tenant-scoped ring buffer
in `event_listener.RecentEventsBuffer` enforces isolation at the data layer
on top of the URL discipline.

Audit invariant (rule 16): the action-execution pipeline lands Day 10. The
diagnostic buffer here is NOT the audit log — it is bounded, in-memory, and
explicitly for operator visibility, not for compliance review.
"""

from __future__ import annotations

import os
import warnings
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Response, status

from context_engine.event_bus import InMemoryEventBus
from context_engine.llm import get_client
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
_listener: EventListener | None = None
_listener_mode: str = "unattached"


def _compose_handler() -> Any:
    """Compose the Day-8 buffered handler with the Day-9 planner handler.

    Chained so a bug in either handler doesn't stop the other from
    running (the listener invariant — handler exceptions are isolated).
    Day 10 will append the policy + queue + audit handler to this chain.
    """
    return chain_handlers(
        make_buffered_handler(_recent_events),
        make_planning_handler(_recent_proposals),
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
# Test-only helpers. Not exported through OpenAPI; callers reach in through
# the Python module path. Keeping these here (rather than in `tests/`) so the
# in-memory wiring story is documented next to the production wiring.
# ----------------------------------------------------------------------------


def _reset_listener_for_tests() -> None:
    """Drop the listener and clear the recent-events/proposals buffers.

    The Day-11 end-to-end test scaffolding uses this between scenarios to
    keep buffer state from one test from leaking into the next. NOT for
    production callers.
    """
    global _listener, _listener_mode
    if _listener is not None:
        _listener.stop()
        _listener = None
    _listener_mode = "unattached"
    _recent_events.clear()
    _recent_proposals.clear()


def _current_listener_mode() -> str:
    """Test introspection — returns the resolved listener mode label."""
    return _listener_mode
