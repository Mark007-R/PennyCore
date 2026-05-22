# PennyCore — API surface (Phase 2 baseline)

Captured at the end of Phase 2 (Day 11, 2026-05-14). The two FastAPI
services boot independently and communicate one-way over a Pub/Sub
event bus — the orchestrator subscribes; the context-engine publishes.
Tenant-scoped reads / writes everywhere; multi-tenant invariant (rule
15 of the SKILL) means there is no implicit "all tenants" surface.

- **context-engine** runs on `http://localhost:8001` under
  docker-compose; `http://0.0.0.0:8000` inside the container.
- **orchestrator** runs on `http://localhost:8002` under
  docker-compose; `http://0.0.0.0:8000` inside the container.

This doc captures what's stable through Day 11. Phase 4 (Days 19-23)
adds `/audit/*` (the full audit-log query surface). Phase 6
(Days 29-32) adds an admin UI (Streamlit / FastAPI+HTMX) over the
`/approvals` and `/actions/*` endpoints documented below — the API
itself doesn't change.

**Companion docs:**

- [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md) — architecture, data flow,
  invariants.
- [POLICIES.md](POLICIES.md) — the policy model that drives the
  `/approvals` endpoints below; the Phase-3 comparison locking
  `declarative` as the champion.
- [DEMO_SCENARIO.md](DEMO_SCENARIO.md) — Jane Doe's mortgage
  walkthrough; the canonical request sequence that exercises every
  endpoint on this page end-to-end.

## Conventions

- **Idempotency.** Every state-changing call carries an
  `X-Idempotency-Key` header (preferred) or an `idempotency_key`
  body field. Both supplied AND disagreeing → `400`. Neither
  supplied → `400`. Same key replayed → `200` with the original
  resource (`deduped=true`, `created=false`).
- **Tenant scoping.** Read endpoints take `?tenant_id=`; write
  endpoints take `tenant_id` in the body. Cross-tenant reads are not
  possible through this surface — the data layer enforces the
  partition (`(tenant_id, ...)` UNIQUE indexes; in-memory stores
  keyed by tenant).
- **Status codes.** `200` for idempotent replays + successful reads;
  `202 Accepted` for newly-created events; `400` for malformed
  idempotency / body mismatches; `404` for unknown
  customer/action/tenant; `409` for illegal state transitions
  (double-approve); `422` for Pydantic-rejected bodies; `503` from
  `/readyz` when a probe fails.
- **Time format.** All `*_at` fields are RFC3339 / ISO-8601 with `+00:00`
  offset — timezone-aware UTC, never naive.

---

## context-engine — `http://localhost:8001`

### `GET /`

Service metadata. No auth, no tenant scope.

```json
{
  "service": "context-engine",
  "version": "0.2.0",
  "llm_mode": "mock",            // resolved client name (mock/anthropic/azure/openai)
  "status": "mvp",
  "phase": "2-mvp-build",
  "datastore_mode": "postgres",  // "memory" if DATABASE_URL unset
  "bus_mode": "redis"            // "in-memory" if REDIS_URL unset
}
```

### `GET /healthz`

Liveness probe. Returns `{"status":"ok"}` if the process is up. No
external probes — use `/readyz` for that.

### `GET /readyz`

Readiness probe. In `memory` mode (no DATABASE_URL) always ready. In
`postgres` mode runs `SELECT COUNT(*) FROM events` to confirm DB
reachability AND schema presence. Returns `503` on probe failure.

```json
{
  "status": "ready",
  "datastores_probed": true,
  "datastore_mode": "postgres"
}
```

### `POST /events`

The ingestion front door. Accepts an event from any channel, runs
cross-channel customer linking, persists, and publishes to the event
bus. Idempotent on `(tenant_id, idempotency_key)`.

**Headers:**
- `X-Idempotency-Key: <string>` — preferred. May ALSO be in the body
  via `idempotency_key`; if both supplied they MUST match.

**Body** (`IngestionRequest`):

```json
{
  "tenant_id": "acme-bank",
  "customer_id": null,            // optional — server-resolves via linker if omitted
  "channel_code": "chat",         // chat | email | sms | voice | api
  "event_type": "message_received",  // message_received | document_uploaded | status_changed | anomaly_detected | system_event
  "idempotency_key": "jane-evt-001",  // OR provide via header
  "payload": {
    "external_id": "jane.doe.001",     // identity hint for linker
    "from_email": "jane@example.com",  // identity hint
    "from_phone": "+15550100123",      // identity hint
    "text": "any update on my mortgage?"
  }
}
```

**Identity hints honored by the linker** (priority order):
`external_id` / `customer_external_id` > `from_email` / `email` /
`sender_email` > `from_phone` / `phone` / `sender_phone` >
`chat_handle` / `user_handle` / `username`.

**Response** (`202 Accepted` new, `200 OK` replay):

```json
{
  "event_id": "evt_3fcfdaaa66b3428a908957e5",
  "tenant_id": "acme-bank",
  "customer_id": "cust_4a3c7166fa5d432ba6686b03",
  "customer_created": true,                  // false on replay or matched existing
  "matched_identity": {"kind": "external_id", "value": "jane.doe.001"},  // null on first-contact create
  "channel_code": "chat",
  "event_type": "message_received",
  "idempotency_key": "jane-evt-001",
  "received_at": "2026-05-14T11:43:49+00:00",
  "created": true,
  "deduped": false
}
```

**Error responses:**
- `400` — no idempotency key supplied (header AND body empty) or
  header/body keys disagree.
- `404` — explicit `customer_id` not found in this tenant.
- `422` — Pydantic validation failed (unknown channel / event_type,
  missing required field, etc.).

---

## orchestrator — `http://localhost:8002`

### `GET /`

Service metadata.

```json
{
  "service": "orchestrator",
  "version": "0.2.0",
  "llm_mode": "mock",
  "status": "mvp",
  "phase": "2-mvp-build",
  "listener_mode": "redis"  // "in-memory" if no REDIS_URL + no bus attached, "unattached" otherwise
}
```

### `GET /healthz`

Liveness probe.

### `GET /readyz`

Readiness probe. Probes Postgres if `DATABASE_URL` is set, Redis if
`REDIS_URL` is set. Returns `503` on any probe failure.

```json
{
  "status": "ready",
  "datastores_probed": true,
  "probed": {"postgres": "ok", "redis": "ok"},
  "listener_mode": "redis"
}
```

### `GET /events/recent`

Tenant-scoped ring buffer of envelopes the listener has received.
**Diagnostic only** — the canonical audit record is in the audit log,
which is reachable via `/actions/{action_id}` (Phase 4 will land
`/audit/*` for richer audit-log queries).

**Query params:**
- `tenant_id` (required, 1-64 chars)
- `limit` (default 20, range 1-100)

The buffer holds the most-recent **50 envelopes per tenant**; a noisy
tenant cannot evict another tenant's traffic. Items are newest-first.

```json
{
  "tenant_id": "acme-bank",
  "limit": 50,
  "count": 5,
  "events": [
    {
      "event_id": "evt_...",
      "tenant_id": "acme-bank",
      "customer_id": "cust_...",
      "channel_code": "chat",
      "event_type": "message_received",
      "received_at": "2026-05-14T11:43:49+00:00"
    }
  ],
  "listener_mode": "redis"
}
```

### `GET /proposals/recent`

Tenant-scoped ring buffer of `ActionProposal` rows the planner emitted.
Same shape and isolation rules as `/events/recent`.

```json
{
  "tenant_id": "acme-bank",
  "limit": 20,
  "count": 5,
  "proposals": [
    {
      "id": "prop_...",
      "tenant_id": "acme-bank",
      "event_id": "evt_...",
      "customer_id": "cust_...",
      "action_type": "send_borrower_message",
      "proposed_by": "fallback",  // "llm" when the planner LLM succeeded
      "payload": {"_planner_reasoning": "rule-table fallback for event_type='message_received'"},
      "created_at": "2026-05-14T11:43:49+00:00"
    }
  ],
  "listener_mode": "redis"
}
```

### `GET /approvals`

List actions awaiting human approval for `tenant_id`, FIFO. The
admin UI's "approve next" workflow consumes this.

**Query params:** `tenant_id` (required).

```json
{
  "tenant_id": "acme-bank",
  "count": 2,
  "approvals": [
    { "action_id": "act_...", "action_type": "send_borrower_message", "status": "pending_approval", ... }
  ]
}
```

Each entry is shaped by the `_action_view` renderer (see
`/actions/{action_id}` for the full field list).

### `POST /approvals/{action_id}/approve`

Approve a pending action. The pipeline transitions
`pending_approval → pending_exec → executed` and writes
`APPROVAL` + `EXECUTION` audit rows.

**Query params:** `decided_by` (default `"api"`, 1-128 chars).

**Errors:**
- `404` — unknown `action_id` OR no queue row (action was
  auto-executed or rejected at policy time).
- `409` — action is in a non-approvable state (already resolved by a
  concurrent approver).

Response shape: same as `GET /actions/{action_id}` — the freshly-
transitioned action with its full audit trail embedded.

### `POST /approvals/{action_id}/reject`

Reject a pending action. The pipeline transitions
`pending_approval → rejected` and writes a `REJECTION` audit row.

**Query params:**
- `reason` (default `""`, up to 2048 chars)
- `decided_by` (default `"api"`)

Same error contract as `approve`.

### `GET /actions/{action_id}`

Fetch one action by ID with its full audit trail embedded.

```json
{
  "action_id": "act_4407e3ff373841e18cc84d8833682af7",
  "tenant_id": "acme-bank",
  "event_id": "evt_3c2a36f8fdc449a79f63bd24",
  "proposal_id": "prop_aa0fa31ce3a149eea2eaa2077d3bec8c",
  "customer_id": "cust_4a3c7166fa5d432ba6686b03",
  "action_type": "send_borrower_message",
  "status": "executed",
  "reasoning": "rule-table fallback for event_type='message_received'",
  "payload": {},
  "executed_payload": {"channel": "chat", "text": "...", "to_customer_id": "cust_..."},
  "created_at": "2026-05-14T11:43:49+00:00",
  "updated_at": "2026-05-14T11:43:50+00:00",
  "executed_at": "2026-05-14T11:43:50+00:00",
  "audit_trail": [
    {"kind": "proposal",  "actor_kind": "fallback", "ts": "...", "payload": {...}},
    {"kind": "decision",  "actor_kind": "system",   "ts": "...", "payload": {...}},
    {"kind": "approval",  "actor_kind": "human",    "ts": "...", "actor_id": "loan-officer-1"},
    {"kind": "execution", "actor_kind": "system",   "ts": "...", "payload": {...}}
  ]
}
```

**Errors:** `404` if the action is unknown to this pipeline instance.

---

## Action types (Phase 2 enum)

The `action_type` field on proposals + actions is constrained to:

| Value | Meaning |
|---|---|
| `send_borrower_message` | Outbound chat / email / SMS to the customer |
| `notify_loan_officer` | Internal alert to the assigned officer |
| `schedule_call` | Calendar invite for the customer |
| `request_document` | Outbound document request |
| `update_status` | Internal loan-status mutation |
| `no_op` | Explicit "do nothing" — paper-trails the decision |

New values land via (a) a new enum entry in `contracts/actions.py`,
(b) a new entry in `orchestrator/executor.py:DEFAULT_EXECUTORS`,
(c) a SQL CHECK constraint update on the `actions` table.

## Policy decisions

The policy engine's `decide(tenant_id, action_type)` returns one of:

| Value | Pipeline effect |
|---|---|
| `auto` | Execute immediately; audit `PROPOSAL → DECISION → EXECUTION` |
| `approval_required` | Queue + wait for `POST /approvals/{id}/approve`; audit `PROPOSAL → DECISION` (`APPROVAL` + `EXECUTION` rows append on approval) |
| `reject` | Drop with `status=rejected`; audit `PROPOSAL → DECISION → REJECTION` |

Resolution order in `DeclarativePolicyEngine`:
exact `action_type` match → `"default"` key → `"*"` wildcard →
fallback `approval_required` (the safe default).

## Audit-kind taxonomy

`audit_trail[].kind` is one of:

`proposal`, `decision`, `approval`, `rejection`, `execution`,
`execution_failed`. Defined in `contracts/audit.py:AuditKind`. The
order in `audit_trail` is insertion order (and time-monotonic per
action).

## Latency budget (Phase 2 baseline)

Measured 2026-05-14 (Day-11 backfill — see
`benchmarks/phase2_latency_backfill.py` and
`results/phase2_latency_backfill.json`):

| Path | p50 | p95 | p99 |
|---|---|---|---|
| `DecisionPipeline.handle_proposal()` (synthetic proposal, auto path) | 0.06 ms | 0.11 ms | 0.27 ms |
| `POST /events` end-to-end (in-process, mock LLM) | 1.78 ms | 2.19 ms | 2.49 ms |

Caveat: the end-to-end numbers use `InMemoryEventBus` — production
Redis Pub/Sub adds extra ms and decouples the orchestrator response
from the POST response (publish is fire-and-forget over the wire).
Phase 6 OpenTelemetry instrumentation (Day 30) captures real
production E2E via trace spans.
