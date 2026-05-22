# PennyCore — System Design

**Status:** Draft 1 (Day 2, Phase 1 — Foundation).
**Owner:** Solo build (engineering lead).
**Scope:** Two tightly-coupled components in one repo — `context-engine` (memory
layer) + `orchestrator` (decision-maker) — each with a thin adapter that
satisfies an external take-home assessment harness.

This document is the architectural source-of-truth for the 35-day build. It
describes WHAT the system is, HOW it is split into components, WHAT data
flows between them, and the INVARIANTS that every later commit must respect.
It does NOT specify SQL DDL or Pydantic field-by-field schemas — those land
on Day 3.

---

## 1. The system in one paragraph

PennyCore is the production-grade backend that an AI customer-service company
(think Decagon / Sierra / Parloa) would run behind their LLM-facing product.
It does two jobs that look like one: it remembers everything a customer has
ever said across every channel (chat, email, SMS, voice) and produces a
token-budgeted brief that the LLM can actually use; and it listens to domain
events, asks the LLM what to do, gates the LLM's proposed actions through a
tenant-specific policy engine, runs a human-approval queue when policy says
so, and writes an audit trail that lets a compliance officer reconstruct
every decision after the fact. The demo scenario is a mortgage borrower
named Jane Doe applying for a loan at "Acme Bank"; the architecture is
indifferent to the vertical.

---

## 2. Component map

```
                              ┌─────────────────────────────────────┐
                              │           External world            │
                              │  chat / email / SMS / voice / API   │
                              └──────────────────┬──────────────────┘
                                                 │ events
                                                 ▼
            ┌────────────────────────────────────────────────────────────────┐
            │                       context-engine                            │
            │                                                                 │
            │   POST /events ──▶ ingestion ──▶ linking ──▶ Postgres          │
            │                                       │                         │
            │                                       └──▶ Redis (event bus)   │
            │                                                                 │
            │   GET /brief ─────▶ retrieval (recency/semantic/summary/hybrid)│
            │                       │                                         │
            │                       └──▶ brief_assembly ──▶ token-budgeted   │
            │                                                  brief          │
            └─────────────────────┬───────────────────────────────────────────┘
                                  │ brief                ▲ subscribe
                                  ▼                      │
            ┌────────────────────────────────────────────────────────────────┐
            │                        orchestrator                             │
            │                                                                 │
            │   event_listener ──▶ planner ──▶ policy engine                 │
            │                          │           │                          │
            │                          ▼           ▼                          │
            │                       LLM call    auto-execute │ approval queue│
            │                                       │                  │      │
            │                                       ▼                  ▼      │
            │                                    executor       human approver│
            │                                       │                  │      │
            │                                       └─────┬────────────┘      │
            │                                             ▼                   │
            │                                         audit_log               │
            └────────────────────────────────────────────────────────────────┘
```

Two services, one repo, one shared schema. Postgres is the system of record
for everything (events, customers, identities, actions, policies, audit_log).
Redis is the in-memory event bus and short-lived caches; it never holds
state that Postgres can't recreate.

### 2.1 context-engine responsibilities

1. **Ingest** an event from any channel into a normalized `events` table.
2. **Link** that event's apparent customer to an existing `customer` record
   when possible (email match, phone match, external-ID match).
3. **Assemble** a brief: given `(customer_id, query, token_budget)`, return
   a string ≤ token_budget that the LLM can read to "remember" the customer.
4. **Record** assistant actions so they can be folded into future briefs
   ("don't ask Jane for her W-2 again — we already got it last Tuesday").
5. Expose two HTTP surfaces: `POST /events` (write) and
   `GET /brief?customer_id=…&token_budget=…&query=…` (read).
6. Expose the `MemorySystem` Protocol from the external take-home evaluator
   via a thin adapter (`takehome/context-engine/memory_system.py`) — the
   adapter calls into the same retrieval/brief modules the production API
   uses, so the external scorecard grades the real implementation.

### 2.2 orchestrator responsibilities

1. **Subscribe** to a Redis channel that context-engine publishes to on every
   ingest.
2. **Plan** actions: build a prompt from `(event, brief)`, ask the LLM (via
   structured tool-calling) for a list of proposed actions.
3. **Apply policy**: for each proposed action, look up the tenant's policy
   for that action type and decide auto-execute / require-approval / reject.
4. **Run the approval queue**: persist pending actions, expose `GET
   /pending`, `POST /approve`, `POST /reject`.
5. **Execute** auto-approved (and human-approved) actions through executor
   stubs that, in this build, log the intended side-effect rather than
   actually sending an email — the executor abstraction is real, the
   integration is mock for the demo.
6. **Audit**: every proposal, policy decision, approval, rejection, and
   execution writes one row to `audit_log` keyed by `(tenant_id,
   action_id)` with a `caused_by_event_id` link.
7. Expose the `AgentOrchestrator` ABC from the external take-home evaluator
   via a thin adapter (`takehome/orchestrator/orchestrator_impl.py`).

---

## 3. Data flow — Jane's mortgage scenario, end-to-end

This is the canonical end-to-end path the system runs on Day 11. Every
arrow below is one of the testable invariants in §5.

```
1.  Jane sends an SMS: "Hey, did my W-2 go through?"
2.  POST /events  ─────▶  context-engine.ingestion
       writes events row (tenant=acme-bank, channel=sms, ...)
       writes message_id idempotency key
       resolves Jane → customer_id=cust-7 via phone-number link
       emits {event_id: e-42, tenant_id, customer_id, ...} to Redis
3.  Redis ─────▶ orchestrator.event_listener
4.  orchestrator.planner builds prompt:
       brief = context-engine.assemble_context(cust-7, query=sms_text,
                                               token_budget=8000)
       LLM(brief, event) ─▶ proposed_actions = [
           {type: "send_borrower_message", body: "Yes, received Tue."},
           {type: "notify_loan_officer", body: "Jane is asking about W-2."}
       ]
5.  orchestrator.policy:
       acme-bank.policy[send_borrower_message] = "auto"
       acme-bank.policy[notify_loan_officer]   = "approval_required"
       ─▶ action #1 → executed (mock: log line)
       ─▶ action #2 → queued in approval_queue
6.  orchestrator.audit writes 4 rows:
       (proposal e-42, action-A1, send_borrower_message, "auto")
       (decision  e-42, action-A1, executed)
       (proposal e-42, action-A2, notify_loan_officer, "approval_required")
       (decision  e-42, action-A2, queued)
7.  A loan officer hits GET /pending, sees action-A2, POSTs /approve.
       audit writes:  (approval action-A2 by user-loan-officer-3)
       executor runs  (action-A2 → mock send_email log)
       audit writes:  (execution action-A2)
```

Every step writes Postgres before publishing to Redis (i.e., Postgres is the
durable log; Redis is the fan-out). If the orchestrator crashes between
steps 4 and 5, the next process can re-read the event from Postgres and
resume — Redis delivery is treated as a hint, not the truth.

---

## 4. High-level schema (table list — DDL lands Day 3)

Twelve tables, one Postgres database, one logical schema. Multi-tenant
discriminator column (`tenant_id`) is on every row of every table that
holds tenant-scoped data.

| Table              | Purpose                                              | Tenant-scoped? |
| ------------------ | ---------------------------------------------------- | -------------- |
| tenants            | One row per customer-of-PennyCore (e.g. Acme Bank)   | (root)         |
| customers          | One row per end-user (e.g. Jane Doe at Acme Bank)    | yes            |
| customer_identities| Channel-specific handles (email, phone, ext-id) ─▶ customers | yes |
| channels           | Lookup (chat / email / sms / voice / api)            | no (global)    |
| events             | Every inbound event from any channel                 | yes            |
| messages           | Conversational content (subset/derivative of events) | yes            |
| briefs             | Cache of assembled briefs (optional, Phase 5)        | yes            |
| actions            | Every action the system has proposed or executed     | yes            |
| action_proposals   | Per-event LLM proposals before policy application    | yes            |
| approval_queue     | Pending-human-approval state for actions             | yes            |
| policies           | Tenant-specific autonomy configuration (YAML/JSON)   | yes            |
| audit_log          | Append-only record of every decision/approval/exec   | yes            |

Notes:
- `events` and `messages` overlap deliberately. `events` is the firehose
  (any incoming domain event); `messages` is the subset where
  `event_type ∈ {chat, email, sms, voice}` and the payload is text we want
  to retrieve over. We materialize messages from events on ingest so
  retrieval queries don't pay the parsing cost.
- `customer_identities` is the cross-channel linking table. A row reads:
  "phone number +1-555-0100 belongs to customer cust-7 at tenant
  acme-bank". When an SMS arrives from +1-555-0100 for tenant acme-bank,
  the linker looks up this row and stamps `customer_id=cust-7` on the
  event. Same customer using two channels = one customer row, two
  identity rows.
- `audit_log` is append-only. Update / delete on this table is rejected at
  the database layer (via trigger, finalized Day 4).

---

## 5. Invariants (the hard rules every commit must respect)

These are the contracts that turn this from a tutorial into infrastructure.
Each one will get an explicit test suite in Phase 4 (Days 19-23).

### 5.1 Idempotency (Day 19 hardening)

> The same event delivered twice produces exactly one set of actions.

Mechanism:
- Every inbound event carries an `idempotency_key` (caller-supplied for
  HTTP, derived from `(channel, message_id)` for streaming).
- `events.idempotency_key` has a UNIQUE constraint scoped by
  `(tenant_id, idempotency_key)`. Re-ingest of the same key returns the
  existing `event_id` and skips the publish to Redis.
- The orchestrator's planner is keyed on `event_id`. If an event is
  re-fanned-out by Redis, the planner sees an existing
  `action_proposals.event_id` row and short-circuits.
- Duplicate-event tests live in `tests/integration/test_idempotency.py`
  (20 tests, Day 19).

### 5.2 Multi-tenant isolation (Day 20 hardening)

> Bank A's data is INVISIBLE to Bank B. Always. Without exception.

Mechanism:
- `tenant_id` is on every tenant-scoped row. There is no "global queue"
  shared across tenants.
- Every repository function takes `tenant_id` as the first parameter and
  every SQL query filters by it. There is no repository function that
  reads or writes tenant data without a tenant_id parameter — enforced by
  code review and a unit test that lints every repository module.
- Postgres Row-Level Security (RLS) policies are added on Day 20 as a
  belt-and-suspenders second layer: even if app-layer filtering bugs out,
  RLS rejects the query.
- Isolation tests live in `tests/integration/test_tenant_isolation.py`
  (15 tests, Day 20). Test pattern: insert data for tenant A, instantiate
  a repository scoped to tenant B, assert nothing from A is visible —
  through every read endpoint, the approval queue, the audit log, and
  retrieval.

### 5.3 Auditability (Day 10 onward)

> Every action's existence is traceable to the event that caused it and
> the human (if any) who approved it.

Mechanism:
- `audit_log` rows: `(tenant_id, ts, kind, action_id, event_id,
  actor_kind, actor_id, payload_json)`. `kind` ∈ {proposal, decision,
  approval, rejection, execution, execution_failed, policy_change}.
- Every state transition on an action writes one row. The action's full
  history is `SELECT * FROM audit_log WHERE action_id = ? ORDER BY ts`.
- `caused_by_event_id` is a column, not a JSON field — joinable for
  compliance queries like "show me every action triggered by events from
  channel=email between dates X and Y".
- Audit-trail tests: every test in `tests/integration/` asserts that the
  flow under test wrote the expected audit rows. There is no path through
  the orchestrator that mutates `actions` without a corresponding
  `audit_log` insert.

### 5.4 Token-budget honesty (Day 7 onward)

> `assemble_context` returns a string whose token count ≤ token_budget.

Mechanism:
- One canonical token estimator: `tiktoken` with
  `cl100k_base` (Anthropic and OpenAI both round to within ~5%, good enough
  for budgeting). Naive `len(text)//4` heuristic is the fallback that the
  external scorecard uses; we honor the stricter of the two.
- Brief assembly is greedy with a hard cap: keep adding segments in
  priority order until the next segment would exceed the budget; truncate
  the partial-fit segment at sentence boundary.
- Property test: for 1,000 random `(customer_history_size,
  token_budget)` pairs, assert `tiktoken_count(brief) ≤ token_budget *
  1.05` (5% slack to accommodate the external scorecard's rougher
  estimator).

### 5.5 Graceful degradation (Day 23 hardening)

> An LLM API outage degrades gracefully. It does NOT 500 the customer.

Mechanism:
- Every LLM call has a deadline (default 8s) and a fallback path.
- For brief assembly: LLM-summarized retrieval falls back to recency-only
  retrieval if the summarization call fails.
- For action planning: LLM planner falls back to a hard-coded rule table
  per `event_type` ("on document_uploaded → propose
  notify_loan_officer"). The action is marked `proposed_by=fallback` in
  `action_proposals.metadata` so the policy engine and audit log can see
  the degradation.
- Fallback tests in `tests/adversarial/` simulate LLM 5xx, timeout, and
  schema-violating responses.

### 5.6 Append-only takehome harness

> `takehome/context-engine/evaluate.py` and
> `takehome/orchestrator/evaluate.py` are NEVER edited.

Mechanism:
- The two `evaluate.py` files are committed verbatim. Any compatibility
  work happens in the adapter modules
  (`memory_system.py` / `orchestrator_impl.py`) that the evaluator
  imports.
- A pre-commit hook (Day 4) computes a checksum of the two files and
  refuses to commit if either changed.

---

## 6. Component-by-component design notes

### 6.1 context-engine

- **Ingestion** (`context_engine/ingestion.py`): one FastAPI POST endpoint
  that accepts a normalized `Event` Pydantic model. Idempotency-key handling
  is a dependency-injected function so tests can mock the dedup oracle.
  Linking is a separate function called inline (not async) — it must run
  before the event is acknowledged so the response can include
  `customer_id`.
- **Linking** (`context_engine/linking.py`): rule pipeline. Try external_id
  first, then email, then phone. If none match, create a new customer +
  identity row. Identities are scoped to (tenant_id, identity_kind, value)
  with a UNIQUE constraint, so two events with the same email become the
  same customer.
- **Retrieval** (`context_engine/retrieval/*.py`): four sibling modules
  implementing four strategies (recency, semantic, summarized, hybrid).
  Each implements the same `Retriever` Protocol with one method:
  `retrieve(customer_id, query, token_budget) -> list[BriefSegment]`. The
  brief assembler is strategy-agnostic; Phase 3 comparison studies (Days
  12-15) ran all four plus a `naive_dump` baseline head-to-head over 200
  pairs and named **`hybrid`** the production champion (41% fewer brief
  tokens than recency at parity fact recall; locked subject to Phase 5 /
  Day 27 real-LLM re-judge). `semantic` and `summarized` remain in the
  codebase as comparison rows, not as defaults.
- **Brief assembly** (`context_engine/brief_assembly.py`): takes the
  `BriefSegment` list from retrieval, sorts by priority, packs into a
  budget. Rendering is templated (Jinja2) so the brief is human-readable
  and the LLM gets consistent structure.
- **LLM dispatch** (`context_engine/llm/*`): one factory `get_client()` reads
  `LLM_PROVIDER` from `.env`. Anthropic Claude (`claude-sonnet-4-6`) is the
  default for the main system; Azure AI Foundry Models (OpenAI-compatible
  REST, default model `Kimi-K2.6`) is the default for the takehome adapter;
  a deterministic mock is the fallback when no provider is configured. The
  dispatch order at runtime is: `MOCK_LLM=true` → mock; else `LLM_PROVIDER`
  picks one of `anthropic` / `azure` / `openai`; else mock. Every call site
  imports `from context_engine.llm import get_client` — no hard-coded
  providers, no direct `os.environ` reads outside this module. The
  takehome adapters carry their own per-component `.env` that pins
  `LLM_PROVIDER=azure` regardless of the global default, satisfying the
  external evaluator's OpenAI-shaped API requirement (Azure Foundry's
  Models endpoint is OpenAI-compatible, so the same `openai` SDK client
  works against both). The `.env.example` template at the project root
  documents every variable; it's the canonical reference.

### 6.2 orchestrator

- **Event listener** (`orchestrator/event_listener.py`): Redis SUBSCRIBE
  loop in an asyncio task. On each message, it loads the event from
  Postgres (Redis carries only the event_id and tenant_id — no payload),
  then dispatches to the planner.
- **Planner** (`orchestrator/planner.py`): builds the LLM prompt from
  `(event, brief)`, calls the LLM via tool-calling with a strict JSON
  schema for `propose_actions(actions: list[Action])`. Fall-back path
  (§5.5) lives in this module.
- **Policy engine** (`orchestrator/policy/*.py`): four sibling modules
  implementing four strategies (declarative YAML, Python rules, LLM-judge,
  naive LLM passthrough). Each implements the same `PolicyEngine`
  Protocol: `decide(tenant_id, action) -> PolicyDecision`. Phase 3 (Day
  17) ran all four head-to-head on 200 scenarios and named
  **`declarative`** the champion outright: 1.000 correctness, 0.6 µs p50
  latency, $0 per 100 decisions, 5/5 auditability + 5/5 maintainability.
  `naive_llm` was the costly cautionary tale at 54% correctness with 0/5
  on `reject`-required scenarios; `llm_judge` stays in the codebase for
  the Phase-5 / Day-28 ambiguous-policy slice. The rest of the
  orchestrator is policy-agnostic. Full case in [POLICIES.md](POLICIES.md).
- **Approval queue** (`orchestrator/approval_queue.py`): thin wrapper over
  the `approval_queue` table. Three operations: enqueue, approve, reject.
  All three write to audit_log.
- **Executor** (`orchestrator/executor.py`): one dispatch dict mapping
  `action_type` → executor function. For the demo, every executor logs
  rather than performs. The shape is real, the side-effects are stubs.
- **Audit** (`orchestrator/audit.py`): one function `record(kind,
  action_id, event_id, actor, payload)`. Called from every state
  transition. Postgres trigger blocks UPDATE/DELETE on the table.

### 6.3 takehome adapters

The adapters are deliberately thin. They translate the take-home
evaluator's data classes (`Message`, `Action` in context-engine;
event/policy/action dicts in orchestrator) into PennyCore's internal
contracts and call into the production code paths. The adapter is the
ONLY layer that knows about the take-home's loose-typed dict-shaped
events; PennyCore proper deals only in Pydantic models.

The takehome adapter gets its own `.env` at `takehome/<component>/.env`
that pins `LLM_PROVIDER=azure` regardless of the global default — this
satisfies the external assessment's "OpenAI API" requirement (Azure AI
Foundry Models endpoint is OpenAI-compatible REST).

---

## 7. Why this shape (and what was rejected)

### 7.1 Two services in one repo, not eight microservices

Rejected: split into ingestion-svc / linking-svc / retrieval-svc /
brief-svc / planner-svc / policy-svc / executor-svc / audit-svc. Pros:
trendy, scalable, mirrors the team-org of a Series-B startup. Cons: 8x
the deployment surface, 8x the auth wiring, 8x the place a bug can hide.
For a 35-day solo build with one demo scenario, two services is enough.
The boundary that matters (memory ↔ decision) is the only boundary
worth paying the network cost for.

### 7.2 Postgres is the system of record, Redis is the bus

Rejected: Kafka or NATS as the bus. Pros: durable, partition-friendly,
production-grade. Cons: another piece of infrastructure to run in
Docker Compose for the local demo, another consumer-group to reason
about. Redis pub/sub is good enough for a demo where the bus is
NOT the source of truth — Postgres is. If a Redis message is dropped,
a polling replay job (built Day 23 as a graceful-degradation hardening
task) re-reads recent events from Postgres and re-publishes.

### 7.3 Token budgeting via greedy-fit, not LP-optimization

Rejected: linear-programming-style optimal budget allocation across
segments. Pros: provably optimal. Cons: the scoring function isn't
clean enough to optimize against (segment "value" is partly a function
of LLM-judged quality of the resulting brief, which is itself measured
post-hoc), and implementation complexity is high. Greedy by priority
is what every production system the author has read about uses; if
Phase 5 shows LP-optimization is worth it on the benchmark, we add it
as Strategy 6.

### 7.4 LLM dispatch via factory, not direct imports

Rejected: hard-code Claude in `planner.py`, hard-code Azure OpenAI in
`memory_system.py`, hard-code mock in tests. Pros: no abstraction tax.
Cons: the comparison study in Phase 3 needs to swap providers cleanly,
the takehome adapter needs Azure-only while the main system uses Claude,
and tests need mock. One factory dispatch means every call site reads
`from context_engine.llm import get_client` and the runtime decides.

### 7.5 RLS + app-layer tenant filter, not just one of them

Rejected: RLS only. Pros: bulletproof at the database layer. Cons:
debugging "why does this query return empty" is opaque when RLS is
silently filtering. Rejected: app-layer only. Pros: explicit, debuggable.
Cons: one missed `WHERE tenant_id = ?` and you have a leak. Doing both
means the app-layer filter is the documented contract and RLS is the
backstop that catches bugs in the contract.

---

## 8. Phase-by-phase build path (this design's rollout)

| Phase | Days | What of this design lands |
| ----- | ---- | ------------------------- |
| 1 | 1–4   | Folder scaffold (Day 1), this doc (Day 2), schema DDL + Pydantic contracts (Day 3), Docker + FastAPI scaffolds (Day 4) |
| 2 | 5–11  | Ingestion + linking + brief-assembly (recency-only) + planner + policy + queue + executor + audit. End-to-end Jane scenario green on Day 11. Takehome adapters (Day 7, Day 10). |
| 3 | 12–18 | ✅ Built benchmark datasets (Day 12 context-engine 200 pairs, Day 16 orchestrator 200 tuples), implemented 5 retrieval strategies (naive_dump, recency, semantic, summarized, hybrid — Days 13-15) + 4 policy strategies (Day 17), ran 9-strategy head-to-head on 400 inputs. **Champions named Day 18**: `hybrid` retrieval, `declarative` policy. Full case in [POLICIES.md](POLICIES.md). |
| 4 | 19–23 | Idempotency (Day 19 ✅ — 20-test integration suite, see `tests/integration/test_idempotency.py`), isolation, race-condition, load, and graceful-degradation test suites. RLS lands here. Pre-commit secret-scan hardens here. |
| 5 | 24–28 | Re-ranker, semantic cache, N-of-M approval, naive-baseline comparisons. |
| 6 | 29–32 | Production-grade Dockerfiles, OpenTelemetry, admin UI, demo UI. |
| 7 | 33–35 | Test sweep, READMEs, ARCHITECTURE.md, demo video, final scorecard pass. |

---

## 9. Open questions (resolved over Phase 1–2)

- **Embedding model** for semantic retrieval: `sentence-transformers/all-MiniLM-L6-v2` (cheap, 384-dim, runs on CPU) vs. `text-embedding-3-small` (better quality, costs $). Decided Day 14 when Strategy 3 lands; default to MiniLM unless quality lags.
- **Approval-queue concurrency**: optimistic-lock via row version vs.
  Postgres advisory lock vs. SELECT FOR UPDATE. Decided Day 21 when
  race-condition tests land.
- **Audit-log retention**: append-only forever vs. partitioned-by-month
  with archive policy. Decided Day 30 during OpenTelemetry work; for the
  demo, append-only forever is fine.
- **Brief cache invalidation**: when a new event is ingested for a
  customer, the cached brief is stale. Time-based TTL (60s) vs. event-
  driven cache bust. Decided Day 25 with the semantic-cache work.

---

## 10. Glossary

- **Event** — anything inbound from the outside world (customer message,
  document upload, status change). The single inbound shape the
  context-engine accepts.
- **Message** — the subset of events whose payload is conversational text.
  Materialized from events on ingest for retrieval performance.
- **Brief** — a token-budgeted string the LLM reads to "remember" a
  customer. Built from retrieval segments.
- **BriefSegment** — one chunk of brief content with metadata (source,
  priority, token estimate). The retrieval layer outputs these; the
  brief assembler packs them.
- **Action** — something the orchestrator wants to do in response to an
  event (send a message, notify a loan officer, schedule a call).
- **ActionProposal** — the LLM's pre-policy proposal. After policy
  application, it becomes a concrete action with an assigned status.
- **Policy** — tenant-specific configuration that decides
  auto-execute / require-approval / reject for each action type.
- **Tenant** — one customer-of-PennyCore (e.g. Acme Bank). The
  multi-tenant isolation boundary.
- **Customer / Borrower** — the end-user (e.g. Jane Doe). Always scoped
  to a tenant.
- **Audit log** — the append-only record of every decision the system
  made and every human approval that was applied to it.

---

## 11. Companion docs

- **[API.md](API.md)** — HTTP contracts for both services (request /
  response shapes, status codes, idempotency-key reconciliation).
- **[POLICIES.md](POLICIES.md)** — tenant policy model, all 4 engines,
  Phase-3 head-to-head comparison, declarative-champion case,
  compliance-authoring workflow.
- **[DEMO_SCENARIO.md](DEMO_SCENARIO.md)** — Jane Doe's mortgage
  walkthrough — the canonical end-to-end story. The
  `tests/integration/test_end_to_end_jane_scenario.py` test is the
  source of truth this doc narrates.
- **[RESEARCH_SURVEY.md](RESEARCH_SURVEY.md)** — Phase-1 survey of
  production AI infrastructure (Decagon, Sierra, Parloa, Anthropic,
  MultiWOZ) and the implications for PennyCore.

---

*End of design draft 1. Day 3 turns the table list in §4 into SQL DDL +
Pydantic models in `contracts/`. Day 4 stands up Docker Compose and the
FastAPI scaffolds for both services so the system boots end-to-end (with
empty implementations) before Phase 2 starts filling them in. Phase 3
wrap (Day 18) named champions; Phase 4 (Days 19-23) hardens around them.*
