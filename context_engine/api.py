"""context-engine FastAPI app.

Day 4 surface: `GET /` + `/healthz` + `/readyz` (scaffold).
Day 5 surface (this file): adds `POST /events` — the ingestion front door.

The ingestion endpoint is intentionally tiny: parse → validate → ingest →
respond. All the work happens in `context_engine.ingestion.ingest_event`
which is bus-and-repo agnostic. The defaults here are the in-memory
implementations (`InMemoryEventRepository`, `InMemoryEventBus`) so the
container boots end-to-end without needing Postgres or Redis. Day 6+ swaps
the defaults for the real Postgres-backed repository; Day 8 swaps the bus
for Redis. Tests use FastAPI's `app.dependency_overrides` to inject fresh
in-memory instances per test (see `tests/unit/test_ingestion.py`).

Multi-tenant invariant (rule 15): every endpoint reads `tenant_id` from
the request body / path / header — there is no implicit "default tenant".
"""

from __future__ import annotations

import warnings
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status

from context_engine.customer_repository import (
    CustomerRepository,
    InMemoryCustomerRepository,
)
from context_engine.event_bus import EventBus, InMemoryEventBus
from context_engine.ingestion import (
    IngestionRequest,
    build_event_from_request,
    ingest_event,
)
from context_engine.linking import resolve_customer
from context_engine.llm import get_client
from context_engine.repository import EventRepository, InMemoryEventRepository

# `.env` lives at the project root and is loaded once at import time.
load_dotenv()


app = FastAPI(
    title="PennyCore — context-engine",
    description=(
        "Memory librarian. Stores every customer interaction across channels, "
        "links customers across channels, and assembles a token-budgeted brief "
        "for the LLM before each reply."
    ),
    version="0.2.0",
)


# ----------------------------------------------------------------------------
# Module-level singletons (in-memory mode). Tests override via
# `app.dependency_overrides[get_repo]` to get a fresh instance per test.
# Day 6 replaces these with Postgres / Redis adapters; the FastAPI dependency
# wiring stays identical.
# ----------------------------------------------------------------------------

_default_repo: EventRepository = InMemoryEventRepository()
_default_bus: EventBus = InMemoryEventBus()
_default_customer_repo: CustomerRepository = InMemoryCustomerRepository()


def get_repo() -> EventRepository:
    return _default_repo


def get_bus() -> EventBus:
    return _default_bus


def get_customer_repo() -> CustomerRepository:
    return _default_customer_repo


# ----------------------------------------------------------------------------
# Meta + health endpoints (Day 4 — unchanged shape, version + phase bumped).
# ----------------------------------------------------------------------------


def _llm_mode() -> str:
    """Report the *resolved* LLM client name — what `get_client()` would
    actually return, including the placeholder-key → mock fallback. Reading
    `LLM_PROVIDER` directly would lie under the `.env.example` defaults
    (LLM_PROVIDER=anthropic + placeholder key), reporting `anthropic` while
    the dispatch layer is actually returning MockClient.

    Warnings are suppressed here because this is a hot diagnostic path
    (called on every `GET /`); the dispatch layer still emits its warning
    on the first real call site (planner / brief assembler / etc.).
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return get_client().name


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {
        "service": "context-engine",
        "version": app.version,
        "llm_mode": _llm_mode(),
        "status": "mvp",
        "phase": "2-mvp-build",
    }


@app.get("/healthz", tags=["health"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", tags=["health"])
def readyz() -> dict[str, Any]:
    """Readiness probe.

    Day 5: the in-memory repo + bus are always ready. Day 6 wires real
    Postgres connectivity probes here (and Day 8 adds Redis). The
    `datastore_mode` field is the hand-off — once it flips from `memory`
    to `postgres`, the readiness contract gets stricter.
    """
    return {
        "status": "ready",
        "datastores_probed": False,
        "datastore_mode": "memory",
    }


# ----------------------------------------------------------------------------
# POST /events — the ingestion front door (Day 5).
# ----------------------------------------------------------------------------


@app.post("/events", tags=["ingestion"])
def post_event(
    request: IngestionRequest,
    response: Response,
    x_idempotency_key: str | None = Header(default=None, alias="X-Idempotency-Key"),
    repo: EventRepository = Depends(get_repo),
    bus: EventBus = Depends(get_bus),
    customer_repo: CustomerRepository = Depends(get_customer_repo),
) -> dict[str, Any]:
    """Accept an event from any channel.

    Idempotency: callers MUST supply an idempotency key — either via the
    `X-Idempotency-Key` HTTP header (preferred) or the `idempotency_key`
    body field. If BOTH are present they MUST be equal: disagreement
    returns `400 Bad Request` rather than silently letting one win,
    because silent precedence would mask a bug in the upstream caller
    (the SKILL §5.5 graceful-degradation rule values visible failures
    over invisible drift). A repeat call with the same key returns
    `200 OK` with `created=false` and the same `event_id` — never an
    error.

    Customer linking (Day 6): the request payload is scanned for identity
    hints (`from_email`, `from_phone`, `external_id`, `chat_handle` and
    aliases). The resolver matches against existing
    `customer_identities` rows for this tenant in priority order
    (external_id > email > phone > chat_handle); on no match a new
    customer is created with all hints inserted as identities. The
    response carries the resolved `customer_id` and `customer_created`
    flag so callers can downstream-route based on whether this is a
    first-contact event. An explicit `request.customer_id` for an
    unknown customer in this tenant returns `404 Not Found`.

    Status codes:
      * `202 Accepted`  — new event accepted for processing.
      * `200 OK`        — idempotent replay; existing event returned.
      * `400 Bad Request` — no idempotency key supplied (header AND body
                            empty) or header/body keys disagree.
      * `404 Not Found` — explicit `customer_id` not found in this tenant.
      * `422 Unprocessable Entity` — request body fails Pydantic validation
                                     (handled by FastAPI automatically).
    """
    idem = x_idempotency_key or request.idempotency_key
    if not idem:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "idempotency_key required: supply via X-Idempotency-Key "
                "header or `idempotency_key` field in the request body"
            ),
        )

    if (
        x_idempotency_key
        and request.idempotency_key
        and x_idempotency_key != request.idempotency_key
    ):
        # Disagreement is a programmer error worth surfacing — silently
        # picking one would mask the bug at the upstream caller.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Idempotency-Key header and body idempotency_key disagree",
        )

    # Replay short-circuit: if this idempotency key has already been seen
    # for this tenant, skip the linker entirely and return the original
    # event. Without this, a replay would re-run identity extraction
    # against a (potentially tampered) replayed payload, which could
    # pollute `customer_identities` even though the stored event is
    # immutable. The Day-19 idempotency hardening tests will exercise the
    # full race-safe path; for Day 6 the pre-check + post-check belt-and-
    # braces is sufficient.
    existing = repo.get_event_by_idempotency(
        tenant_id=request.tenant_id, idempotency_key=idem
    )
    if existing is not None:
        response.status_code = status.HTTP_200_OK
        return {
            "event_id": existing.id,
            "tenant_id": existing.tenant_id,
            "customer_id": existing.customer_id,
            "customer_created": False,
            "matched_identity": None,
            "channel_code": existing.channel_code.value,
            "event_type": existing.event_type.value,
            "idempotency_key": existing.idempotency_key,
            "received_at": existing.received_at.isoformat(),
            "created": False,
            "deduped": True,
        }

    try:
        link = resolve_customer(request, customer_repo=customer_repo)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    event = build_event_from_request(
        request, idempotency_key=idem, customer_id=link.customer_id
    )
    result = ingest_event(event, repo=repo, bus=bus)

    response.status_code = (
        status.HTTP_202_ACCEPTED if result.created else status.HTTP_200_OK
    )
    return {
        "event_id": result.event.id,
        "tenant_id": result.event.tenant_id,
        "customer_id": result.event.customer_id,
        "customer_created": link.customer_created if result.created else False,
        "matched_identity": (
            {
                "kind": link.matched_kind.value,
                "value": link.matched_value,
            }
            if link.matched_kind is not None and result.created
            else None
        ),
        "channel_code": result.event.channel_code.value,
        "event_type": result.event.event_type.value,
        "idempotency_key": result.event.idempotency_key,
        "received_at": result.event.received_at.isoformat(),
        "created": result.created,
        "deduped": not result.created,
    }
