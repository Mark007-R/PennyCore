"""Malformed-event quarantine queue (Day 23 — Phase 4 hardening).

Failure mode this module handles: the ingestion API receives a payload
that cannot be processed normally — extra fields, wrong enum value,
missing idempotency key, or a body that fails any other Pydantic
validation in `IngestionRequest`. Pre-Day-23 the only response was a
422 / 400; the payload was thrown away and the caller had to retry.

That's two failures dressed as one:

  * The customer-facing failure: a webhook from a third-party channel
    is replayed and rejected; the next replay is rejected the same way;
    nobody sees it until a human gets paged.
  * The operational failure: the team has no list of "events the
    pipeline could not eat", so a recurring schema mismatch is invisible
    until someone happens to grep logs for 4xx responses.

Quarantine fixes both. When the API can't ingest, it stores the raw
payload + the reason in a bounded, tenant-scoped ring buffer and exposes
`GET /quarantine/recent?tenant_id=...` for operators to triage. The
buffer is per-process + in-memory — durable storage isn't on the
Day-23 surface because the failure mode it addresses is "the payload
disappeared", not "the payload was lost in a crash". Phase 6 production
polish (Day 29-32) can swap the in-memory ring for a Postgres-backed
dead-letter table without changing the API contract.

Multi-tenant invariant (rule 15): every recall is tenant-scoped; a
caller asking for Bank A's quarantine never sees Bank B's payloads,
even by accident. The ring buffer is keyed by `tenant_id` and the
public `recent()` method REQUIRES a `tenant_id` argument — there is no
"give me everything" accessor.

Audit invariant (rule 16): each quarantine entry carries an immutable
`quarantined_at` UTC timestamp + a UUID id + the structured reason so
the audit log can cite the record. Day-29 production hardening will
also persist the entry to the audit_log table.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from threading import RLock
from typing import Any


class QuarantineReason(str, Enum):
    """Coarse-grained reason taxonomy.

    Chosen to be useful in a triage UI (the operator wants to know
    "schema bug vs. missing-key bug vs. data-injection attempt"),
    not exhaustive. The `detail` field on `QuarantinedEvent` carries
    the verbose breakdown.
    """

    VALIDATION_FAILED = "validation_failed"
    MISSING_IDEMPOTENCY_KEY = "missing_idempotency_key"
    IDEMPOTENCY_KEY_MISMATCH = "idempotency_key_mismatch"
    UNKNOWN_CUSTOMER = "unknown_customer"
    OVERSIZED_PAYLOAD = "oversized_payload"
    INJECTION_SUSPECTED = "injection_suspected"
    OTHER = "other"


@dataclass(frozen=True)
class QuarantinedEvent:
    """One row in the quarantine buffer.

    `raw_payload` is the JSON body the API received, stored as a dict
    (not bytes) — FastAPI has already parsed it, and storing the parsed
    shape makes the triage UI human-readable without a second decode.
    The trade-off: payloads that aren't valid JSON never reach this
    layer (FastAPI returns 400 at the framework boundary, before our
    handler sees them). That's the right boundary — non-JSON in a JSON
    endpoint is an upstream-routing bug, not a schema mismatch worth
    triaging.
    """

    id: str
    tenant_id: str
    reason: QuarantineReason
    detail: str
    raw_payload: dict[str, Any]
    quarantined_at: datetime
    # Free-form headers we want to surface in triage (idempotency key,
    # source IP, etc.). NOT a full dump of every header — that's too
    # noisy and risks PII (cookies, auth tokens). API layer curates.
    context: dict[str, Any] = field(default_factory=dict)


def _new_quarantine_id() -> str:
    return f"q_{uuid.uuid4().hex[:24]}"


class QuarantineBuffer:
    """Bounded per-tenant ring buffer for malformed events.

    Same isolation properties as the Day-8 `RecentEventsBuffer` and
    the Day-9 `ProposalsBuffer`: bounded per-tenant (so a misbehaving
    integration in one tenant can't push another tenant's records out
    of the buffer), RLock-guarded, and the public `recent()` accessor
    REQUIRES the tenant_id.

    Why not a single global cap with tenant tagging: a single global
    cap means Bank A's 10 000-event replay floods out Bank B's three
    legitimate quarantines from yesterday. Per-tenant cap caps the
    cross-tenant blast radius at zero — the SYSTEM_DESIGN §5.2
    multi-tenant isolation invariant calls for exactly this.
    """

    def __init__(self, max_per_tenant: int = 200) -> None:
        if max_per_tenant <= 0:
            raise ValueError(
                f"max_per_tenant must be positive, got {max_per_tenant}"
            )
        self._max = max_per_tenant
        self._lock = RLock()
        self._by_tenant: dict[str, list[QuarantinedEvent]] = {}

    def quarantine(
        self,
        *,
        tenant_id: str,
        reason: QuarantineReason,
        detail: str,
        raw_payload: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        id_factory: Any = _new_quarantine_id,
    ) -> QuarantinedEvent:
        """Add a record. Returns the canonical entry (id + timestamp
        stamped server-side so callers don't get to forge them).

        `tenant_id` MUST be non-empty — the caller is responsible for
        deriving it from the payload (the FastAPI handler reads it from
        the request body) before reaching this method. If the payload
        itself is so broken that the tenant_id cannot be recovered, the
        handler passes `tenant_id="_unparseable_"` so the record at
        least lands in a known bucket; the SYSTEM_DESIGN guidance is
        "lose nothing, even if we can't categorize it perfectly".
        """
        if not tenant_id:
            raise ValueError("tenant_id required (use a sentinel if unknown)")

        entry = QuarantinedEvent(
            id=id_factory(),
            tenant_id=tenant_id,
            reason=reason,
            detail=detail,
            raw_payload=dict(raw_payload or {}),
            quarantined_at=datetime.now(timezone.utc),
            context=dict(context or {}),
        )
        with self._lock:
            buf = self._by_tenant.setdefault(tenant_id, [])
            buf.append(entry)
            if len(buf) > self._max:
                # FIFO eviction — newest entries push out the oldest.
                # Operator UI shows newest first anyway, so the eviction
                # never hides the freshest signal.
                del buf[: len(buf) - self._max]
        return entry

    def recent(self, tenant_id: str, limit: int = 50) -> list[QuarantinedEvent]:
        """Newest-first view for one tenant. Returns a copy so the
        caller cannot mutate the buffer behind the lock."""
        if limit <= 0:
            return []
        with self._lock:
            buf = self._by_tenant.get(tenant_id, [])
            return list(reversed(buf[-limit:]))

    def count(self, tenant_id: str) -> int:
        with self._lock:
            return len(self._by_tenant.get(tenant_id, []))

    def clear(self) -> None:
        """Test-only — production never clears the quarantine buffer."""
        with self._lock:
            self._by_tenant.clear()


# Module-level singleton — the FastAPI app reuses one buffer across
# requests. Tests override via dependency_overrides on `get_quarantine`.
_default_buffer = QuarantineBuffer()


def get_quarantine() -> QuarantineBuffer:
    return _default_buffer


__all__ = [
    "QuarantineBuffer",
    "QuarantineReason",
    "QuarantinedEvent",
    "get_quarantine",
]
