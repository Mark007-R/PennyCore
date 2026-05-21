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

**Day 18 of 35** — Phase 3 complete. Phases 1-3 merged to `main`; Phase 4
(Hardening, Days 19-23) opens tomorrow.

Phase-3 champions (full numbers in
`results/phase3_day18_consolidated.json`):

- **Context-engine retrieval champion: `hybrid`** — 41% fewer brief
  tokens than recency on aggregate at parity fact recall on 183 of 200
  pairs (mock proxy). Real-LLM re-judge scheduled for Phase 5 / Day 27.
- **Orchestrator policy champion: `declarative` (dict / YAML)** — 100%
  correctness on 200 scenarios, 0.6 µs p50 latency, $0 per 100
  decisions, 5/5 auditability + 5/5 maintainability. Wins outright
  against `python_rules`, `naive_llm` (54% correct, 0/5 on `reject`),
  and `llm_judge` (1.000 correct but $0.111/100 dec, ~11× slower).

Phase-3 dataset shape: 5 retrieval strategies × 200 pairs +
4 policy strategies × 200 scenarios = 9 strategies head-to-head on 400
total inputs. Takehome scorecard: context-engine 5/5 non-LLM,
orchestrator 5/6 (LLM-gated scenario fails until the Phase-5 LLM
provider switch).

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
