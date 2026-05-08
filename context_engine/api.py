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

import os
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status

from context_engine.event_bus import EventBus, InMemoryEventBus
from context_engine.ingestion import (
    IngestionRequest,
    build_event_from_request,
    ingest_event,
)
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


def get_repo() -> EventRepository:
    return _default_repo


def get_bus() -> EventBus:
    return _default_bus


# ----------------------------------------------------------------------------
# Meta + health endpoints (Day 4 — unchanged shape, version + phase bumped).
# ----------------------------------------------------------------------------


def _llm_mode() -> str:
    """Resolve the active LLM mode from env. Mirrors `context_engine.llm.get_client`."""
    if os.getenv("MOCK_LLM", "").lower() in ("true", "1", "yes"):
        return "mock"
    return os.getenv("LLM_PROVIDER", "mock").lower()


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
) -> dict[str, Any]:
    """Accept an event from any channel.

    Idempotency: callers MUST supply an idempotency key — either via the
    `X-Idempotency-Key` HTTP header (preferred) or the `idempotency_key`
    body field. The header wins if both are present and disagree, because
    network retries (gateways, queue replays) populate the header but
    can't see the parsed body. A repeat call with the same key returns
    `200 OK` with `created=false` and the same `event_id` — never an error.

    Status codes:
      * `202 Accepted`  — new event accepted for processing.
      * `200 OK`        — idempotent replay; existing event returned.
      * `400 Bad Request` — no idempotency key supplied (header AND body
                            empty) or header/body keys contradict each other.
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

    event = build_event_from_request(request, idempotency_key=idem)
    result = ingest_event(event, repo=repo, bus=bus)

    response.status_code = (
        status.HTTP_202_ACCEPTED if result.created else status.HTTP_200_OK
    )
    return {
        "event_id": result.event.id,
        "tenant_id": result.event.tenant_id,
        "channel_code": result.event.channel_code.value,
        "event_type": result.event.event_type.value,
        "idempotency_key": result.event.idempotency_key,
        "received_at": result.event.received_at.isoformat(),
        "created": result.created,
        "deduped": not result.created,
    }
