# PennyCore Progress Log

Daily entries. Each day's full report lives in `reports/dayNN_phaseN_report.md`
and the plain-English version in `explainers/dayNN_explainer.md`.

---

### 2026-05-04 | PennyCore | Day 01 — Phase 1: Domain research + project scaffold

**Executive Summary:** Bootstrapped the PennyCore repo — folder scaffold,
takehome `evaluate.py` files staged unchanged, `.env` scaffolding for both
the main system (Anthropic-primary) and the takehome adapter (Azure OpenAI),
Day 1 docs written. Initial commit ships to `main` directly per the
phase-PR workflow's Day 1 exception.

**LLM mode:** mock (no system code yet — no LLM calls)

**What was built:**
- Folder scaffold: `docs/`, `contracts/`, `context_engine/{llm,retrieval}/`,
  `orchestrator/policy/`, `takehome/{context-engine,orchestrator}/`,
  `migrations/versions/`, `benchmarks/data/`, `data/{samples,local}/`,
  `notebooks/`, `results/samples/`, `reports/`, `explainers/`,
  `tests/{unit,integration,adversarial}/`, `ui/`, `scripts/`.
- Takehome evaluator + supporting files downloaded from the external
  assessment repo (curl + raw URLs): `evaluate.py`, `requirements.txt`,
  `TASK.md`, `SCORECARD.md` for both context-engine and orchestrator.
  SKILL Rule 17 invariant: `evaluate.py` files NEVER modified.
- `.env.example` + `.env` at project root (Anthropic-primary, multi-provider).
- `.env.example` + `.env` at each `takehome/*/` subdir (Azure-flavored —
  adapter is locked to `LLM_PROVIDER=azure`).
- `README.md`, `POSTS_LOG.md`, `data/README.md`, `takehome/*/DESIGN.md`.
- Verified `.gitignore` excludes `.env`, `takehome/*/.env`, and the three
  SKILL files.

**Tests added:** 0 (no Python yet — first tests land Day 5)
**Coverage delta:** N/A

**Takehome scorecard:** Not run today. First run Day 7 (context-engine) /
Day 10 (orchestrator).

**Experiments Run:** None (Phase 1 has no benchmarks).

**Key Findings:**
1. The takehome's upstream `.env.example` only lists `OPENAI_API_KEY` — our
   adapter needs the full `AZURE_OPENAI_*` set. Captured in the extended
   takehome `.env.example` so Day 7 isn't blocked on config archaeology.
2. The two `evaluate.py` files together are 1,251 LOC. They define
   `MemorySystem` (Protocol, runtime-checkable) and `AgentOrchestrator` (ABC).
   Worth re-reading on Day 6 / Day 9 before writing the adapters.

**What Didn't Work:** N/A — Day 1 is intentionally low-risk.

**Files Created/Modified:**
- New: project folder scaffold (24 dirs)
- New: `README.md`, `POSTS_LOG.md`, `data/README.md`
- New: `.env.example`, `.env` (project root)
- New: `takehome/{context-engine,orchestrator}/.env`, `DESIGN.md`
- Replaced: `takehome/{context-engine,orchestrator}/.env.example`
  (extended upstream OpenAI-only template with Azure vars)
- Downloaded (untouched): `takehome/context-engine/{evaluate.py,requirements.txt,TASK.md,SCORECARD.md}`
- Downloaded (untouched): `takehome/orchestrator/{evaluate.py,requirements.txt,TASK.md,SCORECARD.md}`
- New: `reports/day01_phase1_report.md`, `explainers/day01_explainer.md`
- New: `PROGRESS_LOG.md` (this file)

**Tomorrow:** Day 2 — write `docs/SYSTEM_DESIGN.md` (architecture, data flow,
key invariants). Phase 1 PR opens (`phase/1-foundation` branch off main).

**Post-worthy?** No (project Day < 5 — Gate B blocks all posts in early
foundation phase)
**Post type:** N/A
**Post angle:** N/A
