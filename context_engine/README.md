# context_engine — memory layer

The memory librarian. Ingests customer interactions from every channel,
links identities across channels, and assembles a token-budgeted brief
for the LLM before every reply.

This README is a tour of the modules. The architecture picture is in
[../ARCHITECTURE.md §1](../ARCHITECTURE.md#1-component-map);
the prose source-of-truth is
[../docs/SYSTEM_DESIGN.md §2.1](../docs/SYSTEM_DESIGN.md).

## Public surface

Two HTTP routes (`api.py`) — both scoped by `tenant_id`:

| Route | What it does | Idempotency key |
|-------|--------------|-----------------|
| `POST /events` | Ingest a normalized event from any channel | `(tenant_id, event_id)` |
| `GET /brief` | Return a brief ≤ `token_budget` for `(customer_id, query)` | n/a (pure read) |

Plus health probes (`/healthz`, `/readyz`).

The takehome adapter (`takehome/context-engine/memory_system.py`) wraps
the same modules to expose the external `MemorySystem` Protocol — no
parallel implementation.

## Modules

### Ingestion + identity
- **`ingestion.py`** — `POST /events` handler. Normalizes any channel
  payload to a `contracts.events.Event`, dedupes by
  `(tenant_id, event_id)`, writes to the repository, publishes to the
  event bus. Quarantines malformed events.
- **`linking.py`** — cross-channel customer matching. Match incoming
  event's contact identifiers (email / phone / external IDs) to an
  existing `Customer`. Day-21 patched the check-then-create TOCTOU
  with catch-and-retry on UNIQUE violation (DDIA §7.2.3 pattern).
- **`quarantine.py`** — malformed-event sink. Keeps the ingest hot
  path 200-clean while still surfacing bad inputs for inspection.

### Brief assembly
- **`brief_assembly.py`** — token-budgeted composer. Calls the right
  retrieval strategy, runs the safety filter, optionally consults the
  semantic cache, returns a string ≤ `token_budget`.
- **`retrieval/`** — five strategies, all implementing the same
  `Strategy` protocol:
  - `recency.py` — last K messages in time order.
  - `semantic.py` — top-K by embedding similarity to query
    (sentence-transformers; embeddings cached per message).
  - `summarized.py` — LLM-summarized cold tail + verbatim recent.
  - `rerank.py` — cross-encoder rerank stage layered on semantic.
  - `hybrid.py` — **champion**. Recency (last 24h) + semantic (older)
    + summary (cold tail). Wins on quality AND cost.
- **`safety/prompt_injection.py`** — strips known-bad patterns from
  customer-controlled fields before they enter the prompt. Backed by
  `tests/adversarial/test_prompt_injection.py`.

### Persistence
- **`repository.py`** — in-memory event/customer/brief store. The
  production hot path in CI / local mode; 92%+ coverage.
- **`customer_repository.py`** — in-memory customer + identity store.
  Multi-tenant filter at every query (95% coverage).
- **`pg_repository.py` / `pg_customer_repository.py`** — Postgres
  adapters with the same Protocol. Skipped without `DATABASE_URL`;
  exercised by `tests/integration/test_pg_repositories.py`.

### Event bus
- **`event_bus.py`** — in-process pub/sub (default; used in tests).
- **`redis_event_bus.py`** — Redis pub/sub for the docker-compose
  topology. Both implement the same `EventBus` Protocol.

### LLM dispatch
- **`llm/__init__.py`** — `get_client()` reads `LLM_PROVIDER` and
  `MOCK_LLM` from env (loaded from `.env`), returns the right adapter.
  The dispatch decision tree is drawn in
  [../ARCHITECTURE.md §5](../ARCHITECTURE.md#5-llm-provider-dispatch).
- **`llm/anthropic_client.py`** — Claude (default — sonnet for system,
  haiku for cost-sensitive paths, sonnet for LLM-as-judge).
- **`llm/azure_client.py`** — Azure AI Foundry "Models" endpoint
  (OpenAI-compatible REST). The path the takehome adapter takes.
- **`llm/openai_client.py`** — Standard OpenAI (`gpt-4o`, `gpt-4o-mini`).
- **`llm/mock.py`** — Deterministic stubs. Safe default when no keys
  are set. Numbers from mock mode are flagged "(MOCK-LLM)" in reports.
- **`llm/semantic_cache.py`** — query+brief embedding-similarity cache
  for LLM responses. Phase-5 measured ~28% cost reduction on the
  benchmark when warm.

### Hardening surfaces
- **`slow_call_queue.py`** — defer queue for upstream calls that
  exceeded the timeout budget. Caller gets a degraded response now;
  the work completes async.
- **`timeouts.py`** — per-call deadline machinery used by ingestion
  and brief paths.

## Invariants the tests enforce

1. **Tenant-scoped.** Every repository method takes `tenant_id`;
   `tests/integration/test_tenant_isolation.py` proves no cross-tenant
   leak across 15 surfaces.
2. **Idempotent ingest.** `POST /events` with the same
   `(tenant_id, event_id)` is a no-op;
   `tests/integration/test_idempotency.py` proves it across the
   in-process pure path AND the HTTP path.
3. **Race-safe linking.** Two concurrent ingests for the same new
   customer never raise `IdentityCollision`;
   `tests/integration/test_race_conditions.py` proves it with 10 ms
   latency injection.

## Local dev

```bash
uvicorn context_engine.api:app --reload --port 8001
pytest tests/unit -k "ingestion or brief or linking or llm"
```
