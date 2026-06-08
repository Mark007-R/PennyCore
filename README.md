# PennyCore

Production-grade AI infrastructure for customer-service AI agents in
regulated industries — a memory layer (`context_engine`) and a
decision layer (`orchestrator`), sharing one schema and one audit
log.

The build is a 35-day plan (May 4 – June 7, 2026) and the system also
satisfies an external take-home assessment harness whose interface
contracts live untouched in `takehome/`. Thin adapter modules wrap
the production packages so the system can be graded against the
external scorecard at any time.

## Status

**Day 35 of 35 — project complete.** All seven phases squash-merged
to `main`. The build ran May 4 – June 7, 2026; this is the artefact
it produced. The narrative close-out write-up — the engineering-blog-
shaped public artefact — is at
[`docs/SYSTEM_WRITEUP.md`](docs/SYSTEM_WRITEUP.md).

- **Test suite:** 871 passed, 10 skipped (Postgres-gated; run with
  `DATABASE_URL` set).
- **Coverage:** 90% on core packages
  (`context_engine/` + `orchestrator/` + `contracts/`). LLM adapters
  and OTel noop paths backfilled to 100% on Day 33.
- **Takehome scorecard:** context-engine 5/5 non-LLM, orchestrator 6/6.
- **External-evaluator parity:** both adapters are thin wrappers over
  the same production modules every other call site uses — no parallel
  implementation.

## What's in the box

Two services in one repo, sharing Postgres + Redis. The visual map of
the system is in [ARCHITECTURE.md](ARCHITECTURE.md) (mermaid
diagrams); the prose source-of-truth is
[docs/SYSTEM_DESIGN.md](docs/SYSTEM_DESIGN.md).

| Component | Job |
|-----------|-----|
| [`context_engine/`](context_engine/README.md) | Ingest events from any channel, link customers across channels, assemble a token-budgeted brief for the LLM. Five retrieval strategies (recency · semantic · summarized · hybrid · rerank), prompt-injection defence, semantic-cache. |
| [`orchestrator/`](orchestrator/README.md) | Listen to events, ask the LLM what to do, gate proposed actions through a tenant-specific policy engine, run an approval queue (with N-of-M quorum), execute, and audit. Four policy engines benchmarked head-to-head. |
| [`contracts/`](contracts/README.md) | Shared Pydantic models (events, customers, actions, policies, audit, briefs) + observability shims. Imported by both services and by the takehome adapters. |
| [`takehome/`](takehome/README.md) | External-scorecard adapters. `evaluate.py` is **never** modified; adapter modules wrap the production packages to expose the external `MemorySystem` / `AgentOrchestrator` interfaces. |
| [`benchmarks/`](benchmarks/README.md) | Phase-3 (200-pair retrieval × 200-tuple policy) and Phase-5 (champion-vs-naive) comparison harnesses, plus the load runner. |
| [`tests/`](tests/README.md) | pytest — unit · integration · adversarial. 871 passing. |
| `ui/` | Streamlit approver app + the Day-32 Jane's-mortgage demo walkthrough. |

## Phase champions (locked in)

These are the results that drive the project's narrative. Numbers in
`results/metrics.json`, deep dive in
[docs/POLICIES.md](docs/POLICIES.md) and the Phase 3/5 reports.

> **Measurement honesty.** The whole project ran in `MOCK_LLM` mode
> (no live LLM key worked during the benchmark runs). Token counts,
> latency, correctness, and cost-arithmetic are **computed from real
> execution on the real 200-pair / 200-tuple datasets**. Quality
> scores are a **mock-proxy token-overlap judge**, not a real
> LLM-as-judge — they reward verbatim text, so compression strategies
> score lower than they would under a real judge (noted in the results
> files' `proxy_bias_note`). Numbers below are the actual values in
> `results/phase3_day18_consolidated.json` + `results/phase5_*.json`.

### Context-engine — retrieval strategy (200 pairs)

| Strategy | Brief tokens (mean) | Quality (mock-proxy, 1–5) |
|----------|--------------------:|--------------------------:|
| naive (dump everything) | 1,449.6 | 3.04 |
| recency-only | 1,421.7 | 3.04 |
| semantic-only | 1,421.7 | 3.04 |
| summarized | 211.0 | 1.69 |
| **hybrid (champion)** | **838.4** | **2.91** |

**Hybrid uses 42% fewer brief tokens than naive and 41% fewer than
recency, at parity fact recall on 183 of 200 pairs.** Under the
mock-proxy judge — which rewards verbatim text — hybrid's quality
(2.91) sits just *below* the verbatim strategies (3.04) because it
compresses the cold tail; the champion is chosen on the **cost /
quality-per-token frontier** (hybrid 3.47 quality-per-1K-tokens vs
recency 2.13, +63%), not on raw proxy quality. Phase 5 re-scored the
fact-bearing subset with the same proxy: **naive and hybrid tie at
3.9/5**, while hybrid costs **24% less per 100 queries** ($0.585 vs
$0.769) — but is **slightly slower** at p50 (0.48 ms vs 0.24 ms),
since it does more retrieval work than a flat dump. The honest
headline is *cheaper at tied quality*, not "faster and better".

### Orchestrator — policy engine (200 tuples)

| Strategy | Correctness | Latency p50 (µs, mock/local) | Cost/100 (mock) | Cost/100 (prod proj.) | Auditability | Maintainability | LLM calls |
|----------|------------:|------------------------------:|----------------:|----------------------:|-------------:|----------------:|----------:|
| naive LLM | 54% | 1.8 | $0.139 | $0.57 | 2/5 | 4/5 | 200 |
| python_rules | 100% | 0.7 | $0 | $0 | 3/5 | 2/5 | 0 |
| **declarative YAML (champion)** | **100%** | **0.6** | **$0** | **$0** | **5/5** | **5/5** | **0** |
| llm_judge | 100% | 6.7 | $0.111 | $0.57 | 4/5 | 4/5 | 200 |

**Declarative ties the LLM engines on correctness (100%) at zero
marginal cost** — $0 vs ~$0.57 per 100 decisions *projected* under real
Sonnet pricing (mock-run payload cost was $0.111–0.139), and ~11×
lower local decision latency (0.6 vs 6.7 µs). **Naive-LLM collapses to
54% and misses all 5 `reject` scenarios (0/5)** — the silent
auto-execute failure regulators audit for. Latencies are mock-mode
**local compute only** (no network round-trip); a real LLM call would
add unmeasured network latency, so the defensible axes here are
**correctness and cost**, not latency.

## Hardening (Phase 4)

Numbers stand from Day 23:

- **Idempotency:** 20-test suite. Every replay surface keys on
  `(tenant_id, event_id)` or `(tenant_id, idempotency_key)`. All
  passed first-run; the Phase-2 dedup discipline held.
- **Multi-tenant isolation:** 15-test suite across 6 surfaces. Two
  real HTTP leak surfaces found and closed (
  `GET /actions/{id}` and `POST /approvals/{id}/{approve,reject}` now
  accept optional `tenant_id` and 404 cross-tenant attempts with the
  same shape as missing-row, no existence leak — OWASP API1:2023).
- **Race conditions:** 10-test suite. Nine were safe by Phase-2
  construction; the tenth — `linking.resolve_customer` check-then-
  create TOCTOU — was a real bug invisible to vanilla threaded tests
  (GIL serialized the window). Surfaced via 10 ms latency injection
  simulating a Postgres round-trip; patched to the catch-and-retry-on-
  UNIQUE pattern (Kleppmann DDIA §7.2.3).
- **Failure modes:** LLM down → degraded brief path; Postgres slow →
  defer queue; malformed event → quarantine; prompt-injection input →
  sanitiser + adversarial test suite.

## Production polish (Phase 6)

- **Docker:** `Dockerfile.prod`, `docker-compose.prod.yml`. Health
  probes, non-root user, multi-stage build.
- **OpenTelemetry:** spans from event ingestion through action
  execution. `docker-compose.otel.yml` brings up Jaeger + the OTel
  collector. `contracts/observability.py` is the single seam — noop
  when the SDK isn't installed, so no hard dependency on OTel for
  prod-tier customers.
- **Approver UI:** `ui/approver_app.py` (Streamlit). Pending queue,
  recent audit log, approve/reject buttons. Filters by tenant.
- **Demo scenario UI:** `ui/demo_scenario_app.py` walks Jane's
  mortgage journey end-to-end with timeline + finding cards
  cross-linked to Phase-3/5 results.

## Running locally

```bash
docker-compose up                    # Postgres + Redis + both services
pytest                               # 871 passed, 10 skipped
./scripts/run_takehome_evals.sh      # both external evaluators
streamlit run ui/approver_app.py     # approver dashboard
streamlit run ui/demo_scenario_app.py # Jane's mortgage walkthrough
```

Real LLM keys go in `.env` (git-ignored); without keys the system runs
in `MOCK_LLM` mode and benchmark numbers are flagged "(MOCK-LLM)" in
the reports. See [`.env.example`](.env.example) for the variables.

## Demo

The five-minute recorded walkthrough script lives at
[docs/DEMO_VIDEO_SCRIPT.md](docs/DEMO_VIDEO_SCRIPT.md). It threads:
docker-compose up → ingest Jane's mortgage_inquiry → watch the
auto-execute action → watch the quorum-gated action queue and approve
it → reload the approver UI → flip MOCK_LLM=true / false to show
provider dispatch → show the audit log.

## Layout

- `context_engine/` — memory librarian (ingestion, linking, brief
  assembly, retrieval strategies, LLM dispatch, safety filters,
  semantic cache, slow-call defer queue, quarantine).
- `orchestrator/` — decision-maker (event listener, planner, four
  policy engines, approval queue, quorum, executor, audit, decision
  pipeline).
- `contracts/` — shared Pydantic models + observability shim +
  build-info plumbing.
- `takehome/` — external-scorecard adapters; evaluators NEVER
  modified.
- `benchmarks/` — Phase 3 + Phase 5 + load + semantic-cache harnesses.
  - `benchmarks/data/` — 200 customer-history / query pairs and
    200 event / tenant / expected-action tuples (committed).
- `migrations/` — Alembic versions.
- `tests/` — 871 passing across unit / integration / adversarial.
- `results/` — metrics journal, experiment log, comparison charts,
  takehome scorecard, sample diagrams.
- `notebooks/` — Phase-3 / Phase-5 analysis (read results JSON,
  render comparison tables and charts).
- `docs/` — [SYSTEM_WRITEUP](docs/SYSTEM_WRITEUP.md) (public
  hiring-manager narrative, Day 35) · [SYSTEM_DESIGN](docs/SYSTEM_DESIGN.md) ·
  [API](docs/API.md) · [POLICIES](docs/POLICIES.md) ·
  [DEMO_SCENARIO](docs/DEMO_SCENARIO.md) ·
  [RESEARCH_SURVEY](docs/RESEARCH_SURVEY.md) ·
  [DEMO_VIDEO_SCRIPT](docs/DEMO_VIDEO_SCRIPT.md).
- `ui/` — Streamlit approver app + Jane's-mortgage walkthrough.
- `scripts/` — migrations, takehome eval runner, diagram renderers,
  local-run / CI shells.

## Hard invariants (enforced by tests, not by convention)

1. **Multi-tenant.** Every query and every endpoint scopes by
   `tenant_id`. Cross-tenant access 404s with the same shape as
   missing-row.
2. **Idempotent.** Replays of the same `(tenant_id, event_id)` or
   `(tenant_id, idempotency_key)` are no-ops.
3. **Auditable.** Every proposal, policy decision, approval,
   rejection, and execution writes one row to `audit_log` keyed by
   `(tenant_id, action_id)` with a `caused_by_event_id` link. A
   compliance officer can reconstruct any decision.
4. **Takehome-untouched.** `takehome/*/evaluate.py` is never modified.
   Adapters wrap, never duplicate.
5. **Provider-agnostic.** Every LLM call site goes through
   `context_engine.llm.get_client()`; no module reads API keys
   directly. Flipping `MOCK_LLM=true` runs the whole system with zero
   keys.
