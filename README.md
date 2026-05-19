# PennyCore

Production-grade AI infrastructure: a memory layer (`context_engine`) and a
decision layer (`orchestrator`) for customer-service AI agents in regulated
industries (banking, mortgage, insurance).

The project is built around a 35-day plan (May 4 – June 7, 2026) and is
designed to also satisfy an external take-home assessment harness whose
interface contracts live untouched in `takehome/`. Thin adapter modules wrap
the production packages so the system can be graded against the external
scorecard at any time.

## Status

**Day 16 of 35** — Phase 3: Comparison Studies (Days 12-18, in flight).
Phases 1-2 merged to `main`; the context-engine 5-strategy retrieval
study (Days 12-15) and the orchestrator benchmark dataset (Day 16) are
shipped. Day 17 runs the 4 policy-engine strategies; Day 18 picks
champions and wraps Phase 3.

Canonical numbers to date:
- Context-engine retrieval: hybrid emits 41% smaller briefs than
  recency on aggregate while matching recency's fact recall on 183 of
  200 pairs (mock-proxy quality; real-LLM re-judge scheduled Day 18).
- Orchestrator benchmark: 200 `(event, tenant, expected_action_type,
  expected_decision)` tuples across 3 tenants whose policy tables
  intentionally diverge (multi-tenant signal verified by test).
- Takehome scorecard: context-engine 5/5 non-LLM, orchestrator 5/6
  (LLM-gated scenario fails until Day-18 LLM provider switch).

## Layout

- `context_engine/` — memory librarian (event ingestion, cross-channel
  linking, brief assembly, 5 retrieval strategies, LLM dispatch)
- `orchestrator/` — decision-maker (event listener, LLM planner, policy
  engine, approval queue, executor, audit)
- `contracts/` — shared Pydantic models (events, actions, policies,
  briefs, audit, customers)
- `takehome/` — external scorecard compliance (evaluators are NEVER
  modified; thin adapter modules wrap the production packages)
- `benchmarks/` — Phase 3 + Phase 5 comparison-study harnesses
  - `benchmarks/data/` — 200-pair context-engine dataset (Day 12)
  - `benchmarks/data/orchestrator/` — 200-tuple orchestrator dataset
    (Day 16)
- `migrations/` — versioned SQL DDL
- `tests/` — pytest (unit, integration, adversarial); 450+ tests
- `results/` — metrics journal, experiment log, comparison charts +
  takehome scorecard
- `notebooks/` — Phase-3 / Phase-5 analysis notebooks
- `docs/` — system design, API contracts, research survey
- `scripts/` — CLI helpers (migrations, takehome eval runner, diagram
  renderers, local-run / CI shells)

## Running locally

`docker-compose up` boots Postgres + Redis + both FastAPI services. Real
keys go in `.env` (git-ignored); without keys the system runs in
MOCK_LLM mode. See `.env.example` for the variables that need to be set.

The takehome harness lives at `takehome/context-engine/evaluate.py` and
`takehome/orchestrator/evaluate.py` — run them with
`scripts/run_takehome_evals.sh` from the project root.

The full Day-by-Day plan, hard invariants, and per-phase deliverables
all live in the local-only SKILL files (not committed) — the published
artefacts are the source tree, the daily reports / explainers (also
local-only), and `results/EXPERIMENT_LOG.md`.
