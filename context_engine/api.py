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
import warnings
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

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
from context_engine.quarantine import (
    QuarantineBuffer,
    QuarantineReason,
    get_quarantine,
)
from context_engine.repository import EventRepository, InMemoryEventRepository
from context_engine.safety import sanitize_for_prompt
from context_engine.safety.prompt_injection import has_injection_markers

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


@app.exception_handler(RequestValidationError)
async def _validation_quarantine_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Day 23 — quarantine the raw payload on Pydantic validation
    failure, then emit FastAPI's standard 422 response shape.

    We keep the 422 contract identical (existing tests rely on the
    `detail` array), but the quarantine record gives operators a feed
    of "events we couldn't even parse" — the SYSTEM_DESIGN §5.6 dead-
    letter pattern that protects against silent schema drift.

    Tenant scoping: we try to recover `tenant_id` from the raw body so
    the record lands in the right tenant's bucket. If recovery fails
    (the body is so malformed that `tenant_id` isn't a string), the
    entry lands in the `"_unparseable_"` bucket — the SKILL says
    "lose nothing, even if we can't categorize it perfectly".
    """
    body: Any = None
    try:
        body = await request.json()
    except Exception:  # pylint: disable=broad-except
        body = None
    tenant_id = "_unparseable_"
    if isinstance(body, dict):
        candidate = body.get("tenant_id")
        if isinstance(candidate, str) and candidate:
            tenant_id = candidate
    try:
        get_quarantine().quarantine(
            tenant_id=tenant_id,
            reason=QuarantineReason.VALIDATION_FAILED,
            detail=f"Pydantic validation failed: {len(exc.errors())} error(s)",
            raw_payload=body if isinstance(body, dict) else {"_non_dict_body": str(body)[:500]},
            context={"path": request.url.path, "method": request.method},
        )
    except Exception:  # pylint: disable=broad-except
        # The handler must never raise — that would mask the user-facing
        # 422 with a 500. We silently drop the quarantine write rather
        # than failing the request.
        pass
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": jsonable_encoder(exc.errors())},
    )


# ----------------------------------------------------------------------------
# Datastore selection — DATABASE_URL flips the entire stack to Postgres.
#
# When DATABASE_URL is set at import time, the module-level repositories
# become the psycopg-backed adapters; otherwise they stay in-memory. The
# FastAPI dependency wiring is identical either way (same Protocol shape),
# so request handlers don't know or care which backend is live. Tests
# always override via `dependency_overrides`, so the unit-test path is
# unaffected by the deployer's `DATABASE_URL` value.
# ----------------------------------------------------------------------------


def _build_default_repos() -> tuple[
    EventRepository, CustomerRepository, str
]:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return (
            InMemoryEventRepository(),
            InMemoryCustomerRepository(),
            "memory",
        )

    # Lazy imports — psycopg is only needed when DATABASE_URL is set.
    from context_engine.pg_customer_repository import (
        PgCustomerRepository,
        make_connection_factory,
    )
    from context_engine.pg_repository import PgEventRepository

    factory = make_connection_factory(database_url)
    return (
        PgEventRepository(factory),
        PgCustomerRepository(factory),
        "postgres",
    )


_default_repo: EventRepository
_default_customer_repo: CustomerRepository
_datastore_mode: str
_default_repo, _default_customer_repo, _datastore_mode = _build_default_repos()


def _build_default_bus() -> tuple[EventBus, str]:
    """Pick the publish-side bus from env. Mirrors the orchestrator's
    listener-mode selector: `REDIS_URL` set → `RedisEventBus`; unset →
    `InMemoryEventBus` (the test path and any single-process scenario).

    Day-8 end-to-end wiring lives here: if both services run with the
    same `REDIS_URL` (as docker-compose configures), the orchestrator's
    `RedisEventListener` actually receives what this bus publishes.
    """
    redis_url = os.getenv("REDIS_URL", "").strip()
    if not redis_url:
        return InMemoryEventBus(), "in-memory"

    # Lazy import — keeps the test path psycopg-style: only paths that
    # explicitly opt into Redis pay the import cost.
    from context_engine.redis_event_bus import make_redis_event_bus

    return make_redis_event_bus(redis_url), "redis"


_default_bus: EventBus
_bus_mode: str
_default_bus, _bus_mode = _build_default_bus()


def get_repo() -> EventRepository:
    return _default_repo


def get_bus() -> EventBus:
    return _default_bus


def get_customer_repo() -> CustomerRepository:
    return _default_customer_repo


def _payload_strings(payload: dict[str, Any]) -> list[str]:
    """Pull out every string leaf from a (possibly nested) payload dict so
    the injection sanitiser only sees text — never numbers, booleans, or
    `None`. Used by the Day-23 hardening hook on `POST /events`."""
    out: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            out.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(payload)
    return out


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
        "datastore_mode": _datastore_mode,
        "bus_mode": _bus_mode,
    }


@app.get("/healthz", tags=["health"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/quarantine/recent", tags=["operations"])
def quarantine_recent(
    tenant_id: str,
    limit: int = 50,
    buffer: QuarantineBuffer = Depends(get_quarantine),
) -> dict[str, Any]:
    """Operator visibility for malformed events (Day 23).

    Multi-tenant invariant (rule 15): `tenant_id` is REQUIRED; there
    is no global view. A caller asking for Bank A's quarantine never
    sees Bank B's records.

    The endpoint is read-only and intentionally unauthenticated at the
    Phase-4 surface — the Phase-6 production polish will sit it behind
    the same approver auth as `/approvals/*`. Today it's a triage
    convenience.
    """
    if not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant_id query parameter required",
        )
    if limit < 1 or limit > 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="limit must be between 1 and 200",
        )
    entries = buffer.recent(tenant_id, limit=limit)
    return {
        "tenant_id": tenant_id,
        "count": len(entries),
        "entries": [
            {
                "id": e.id,
                "reason": e.reason.value,
                "detail": e.detail,
                "quarantined_at": e.quarantined_at.isoformat(),
                "raw_payload": e.raw_payload,
                "context": e.context,
            }
            for e in entries
        ],
    }


@app.get("/readyz", tags=["health"])
def readyz(response: Response) -> dict[str, Any]:
    """Readiness probe.

    In `memory` mode (no DATABASE_URL) the in-memory repo is always
    ready and we don't probe anything. In `postgres` mode we open a
    short-lived connection and run `SELECT 1` to confirm the database
    is reachable AND that the schema is loaded (the `events` table
    must exist; otherwise the migration runner hasn't been pointed at
    this database yet). A failed probe returns 503 so kube/docker
    readiness gates fail-closed.

    Day 8 adds the Redis probe alongside the Postgres one.
    """
    if _datastore_mode == "memory":
        return {
            "status": "ready",
            "datastores_probed": False,
            "datastore_mode": "memory",
        }

    # Postgres-mode probe — small, single round-trip.
    try:
        repo = _default_repo
        # The narrowest live-connectivity check that also exercises the
        # schema: count() runs `SELECT COUNT(*) FROM events`. If the
        # table is missing the query raises and we fail-closed below.
        repo.count()
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "not_ready",
            "datastores_probed": True,
            "datastore_mode": _datastore_mode,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "status": "ready",
        "datastores_probed": True,
        "datastore_mode": _datastore_mode,
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
    quarantine: QuarantineBuffer = Depends(get_quarantine),
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
        # Day 23: drop the raw payload into the quarantine ring buffer
        # so operators can triage missing-key bugs without grepping
        # access logs. The 400 response shape is unchanged.
        quarantine.quarantine(
            tenant_id=request.tenant_id,
            reason=QuarantineReason.MISSING_IDEMPOTENCY_KEY,
            detail="POST /events with neither X-Idempotency-Key nor body idempotency_key",
            raw_payload=request.model_dump(mode="json"),
        )
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
        quarantine.quarantine(
            tenant_id=request.tenant_id,
            reason=QuarantineReason.IDEMPOTENCY_KEY_MISMATCH,
            detail=(
                f"X-Idempotency-Key={x_idempotency_key!r} disagrees with "
                f"body idempotency_key={request.idempotency_key!r}"
            ),
            raw_payload=request.model_dump(mode="json"),
            context={"header_key": x_idempotency_key, "body_key": request.idempotency_key},
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Idempotency-Key header and body idempotency_key disagree",
        )

    # Day 23 — injection detection on the payload body. We FLAG (record
    # to quarantine for operator visibility) but do NOT block ingestion:
    # blocking would create a false-positive denial-of-service vector
    # (a legitimate banker saying "please ignore my previous request"
    # would be locked out). The planner's sanitiser is the real
    # mitigation (it rewrites the brief before the LLM sees it); the
    # quarantine entry is the audit signal.
    if has_injection_markers(_payload_strings(request.payload)):
        quarantine.quarantine(
            tenant_id=request.tenant_id,
            reason=QuarantineReason.INJECTION_SUSPECTED,
            detail=(
                "payload contains injection-marker text; planner will "
                "sanitise before LLM call (this entry is for visibility "
                "only — event still ingested)"
            ),
            raw_payload=request.model_dump(mode="json"),
            context={"idempotency_key": idem, "blocking": False},
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
        # Day 23: quarantine unknown-customer events so operators can
        # see "Bank A is sending us events tagged with cust_xyz that
        # we don't know about" without grepping the 404 log channel.
        quarantine.quarantine(
            tenant_id=request.tenant_id,
            reason=QuarantineReason.UNKNOWN_CUSTOMER,
            detail=str(exc),
            raw_payload=request.model_dump(mode="json"),
            context={"idempotency_key": idem},
        )
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
