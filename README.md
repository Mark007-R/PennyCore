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

**Day 1 of 35** — Phase 1: Foundation. Folder scaffold + takehome
`evaluate.py` files staged + `.env` scaffolding in place. See `PROGRESS_LOG.md`
and `reports/` / `explainers/` for daily progress.

## Layout

- `context_engine/` — memory librarian (event ingestion, cross-channel linking, brief assembly)
- `orchestrator/` — decision-maker (event listener, LLM planner, policy engine, approval queue, audit)
- `contracts/` — shared Pydantic models
- `takehome/` — external scorecard compliance (evaluators are NEVER modified)
- `benchmarks/` — Phase 3 + Phase 5 comparison-study harnesses
- `tests/` — pytest (unit, integration, adversarial)
- `reports/` — daily technical reports (engineer audience)
- `explainers/` — daily plain-English explainers (future-self / non-engineer audience)
- `results/` — metrics, experiment logs, comparison charts
- `docs/` — system design, API contracts, demo scenario walkthrough

## Running locally

`docker-compose up` boots Postgres + Redis + both FastAPI services. Real keys
go in `.env` (git-ignored); without keys the system runs in MOCK_LLM mode.
See `.env.example` for the variables that need to be set.
