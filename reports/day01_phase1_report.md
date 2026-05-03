# Day 01 — Phase 1: Domain research + project scaffold — PennyCore
**Date:** 2026-05-04
**Day:** 01 of 35
**LLM mode:** mock (no system code yet — no LLM calls today)

## Objective
Bootstrap the PennyCore repo: folder scaffold, `.env` scaffolding for both the
main system and the takehome adapters, the takehome `evaluate.py` files staged
unchanged, and the Day 1 docs (this report + the explainer). End state: a
clean repo on `main` that future days branch off of.

## Research & References
The Phase 1 research target is "what does production-grade AI infrastructure
for customer service look like in 2025-2026, in the regulated-industry niche
that Decagon / Sierra / Parloa play in?" Today's session staged five reading
targets (deeper notes will land in `docs/SYSTEM_DESIGN.md` on Day 2). The
short list:

1. **Decagon engineering blog** — concierge-agent architecture, retrieval-augmented
   memory, and how they decompose "agent" into ingestion + brief + planner + executor.
   (https://decagon.ai/blog)
2. **Sierra engineering posts** — multi-tenant agent platforms, the
   approval-queue pattern for compliance-sensitive actions, and audit-log
   design. (https://sierra.ai/blog)
3. **Parloa product / engineering writing** — voice-channel ingestion, channel
   normalization, and cross-channel customer linking.
4. **Anthropic "Claude for customer service" reference architecture** — tool-use
   contracts, structured-output planners, the LLM-as-policy pattern and its
   pitfalls. (Used as the conceptual baseline for the planner module that
   lands Day 9.)
5. **MultiWOZ dataset** — the benchmark corpus the Phase 3 context-engine
   comparison study will draw from. 10K multi-turn dialogues across multiple
   domains; relevant for the "customer history with 100+ messages" slice.
   (https://github.com/budzianowski/multiwoz)

These are reading targets, not finished annotations. Tomorrow's
`SYSTEM_DESIGN.md` synthesizes the relevant pieces into the architecture doc.

## Setup
- Component(s) touched: repo root, `takehome/`, top-level scaffold dirs only.
  No `context_engine/` or `orchestrator/` Python written today (those start
  Day 5 per the schedule).
- Tests added: 0 (no Python yet)
- Migrations: 0

## Implementation
### What was built
- Folder scaffold (24 directories): `docs/`, `contracts/`, `context_engine/`
  (with `llm/` and `retrieval/` subdirs), `orchestrator/` (with `policy/`),
  `takehome/context-engine/`, `takehome/orchestrator/`,
  `migrations/versions/`, `benchmarks/data/`, `data/samples/`, `data/local/`,
  `notebooks/`, `results/samples/`, `reports/`, `explainers/`,
  `tests/{unit,integration,adversarial}/`, `ui/`, `scripts/`.
- Takehome files downloaded from
  `(external assessment repo — redacted)` via raw URLs (curl):
  - `takehome/context-engine/evaluate.py` (564 LOC, untouched)
  - `takehome/orchestrator/evaluate.py` (687 LOC, untouched)
  - Plus `requirements.txt`, `TASK.md`, `SCORECARD.md` for both halves.
- `.env.example` + `.env` at project root (Anthropic-primary,
  Azure/OpenAI/mock fallbacks, Postgres + Redis defaults). `.env` is
  git-ignored.
- `.env.example` + `.env` at `takehome/context-engine/` and
  `takehome/orchestrator/` (Azure-flavored — adapter is locked to
  `LLM_PROVIDER=azure` per the SKILL spec). The takehome's upstream
  `.env.example` only listed `OPENAI_API_KEY` — extended to include the
  `AZURE_OPENAI_*` variables our adapter will read on Day 7 / Day 10.
- `README.md` (project overview), `POSTS_LOG.md` (empty — gated until Day 5),
  `data/README.md`, `takehome/*/DESIGN.md` (adapter rationale).
- Existing `.gitignore` already had `.env`, `takehome/*/.env`, and the
  three SKILL filenames listed — no changes needed.
- `PROGRESS_LOG.md` initialized with today's entry.

### Decisions made and rejected alternatives
- **Anthropic Claude as the main-system primary, Azure OpenAI for the
  takehome adapter only.** Considered using a single provider (OpenAI) end
  to end. Rejected: the SKILL explicitly calls for Claude as primary so the
  comparison-study LLM-as-judge runs are clean (one provider for both system
  AND judge), but the takehome's "OpenAI API" requirement is non-negotiable.
  Splitting the env (`./.env` for the main system, `takehome/*/.env` for the
  adapter) keeps both contracts satisfied without conditional code in the
  dispatch layer.
- **Two `.env` scopes (project root + takehome subdirs), not one global
  `.env`.** A single global file would bleed Azure config into the main system
  and risk the dispatch layer accidentally picking the wrong provider.
  Per-directory `python-dotenv` loads keep the boundaries clean and make the
  takehome adapter's "always Azure" rule a config invariant, not a code
  invariant.
- **Curl + raw GitHub URLs to fetch the takehome files**, not `git submodule`
  or `git subtree`. Submodules add a permanent foreign-repo dependency and
  surface in `git status` forever; we only need a snapshot of the evaluator
  files and they're under SKILL Rule 17 (never modified). A flat copy is
  simpler and obviously correct.
- **Folder scaffold today, not stub Python files.** Tempting to drop empty
  `__init__.py` files into every package now. Held off — Day 5 creates the
  `context_engine/llm/` modules with real content; empty placeholders today
  would just be churn for tomorrow's commits.

### Code quality checks
- Tests added: 0 (nothing to test on Day 1)
- Coverage on touched modules: N/A
- Type-checking clean: N/A (no Python)
- Lint clean: N/A
- Pre-commit secret scan: clean (verified before staging)

## Takehome scorecard
Not run today. First takehome run is Day 7 (context-engine adapter ships) and
Day 10 (orchestrator adapter ships). The unmodified `evaluate.py` files are
staged and verified executable (`python -c "import ast; ast.parse(open(...).read())"`
parses cleanly).

## Key Findings
1. The takehome's `.env.example` only lists `OPENAI_API_KEY`. Our adapter
   needs Azure-specific config (endpoint, deployment, api_version) that the
   upstream template doesn't mention — easy to miss until Day 7. Captured
   in our extended takehome `.env.example` so Day 7 doesn't get blocked on
   config archaeology.
2. The two `evaluate.py` files together are 1,251 LOC defining `MemorySystem`
   (Protocol, runtime-checkable) and `AgentOrchestrator` (ABC). Both lean on
   `dataclass(frozen=True)` for the data types. Worth re-reading Day 6 / Day 9
   before writing the adapters so the wrapper matches the contract exactly.

## Failures / Edge Cases Encountered
None. Day 1 is intentionally low-risk — folder scaffold + downloads + docs.

## Sample Outputs / Demo Artifacts Saved
- `takehome/context-engine/evaluate.py` (564 LOC, sha `0493b46…`)
- `takehome/orchestrator/evaluate.py` (687 LOC, sha `8c2f656…`)
- Project tree visible via `tree -L 2` after this commit lands.

## Next Day
- Day 2: write `docs/SYSTEM_DESIGN.md` — components, data flow, schemas at
  high level, key invariants (idempotency, multi-tenant isolation,
  auditability). Phase 1 PR opens on Day 2 (`phase/1-foundation` branch).

## References Used Today
- external takehome assessment repo: (external assessment repo — redacted)
- Anthropic Python SDK docs: https://docs.anthropic.com (LLM dispatch design)
- python-dotenv docs: https://github.com/theskumar/python-dotenv (`.env` loading conventions)

## Code Changes
- Created: project folder scaffold (24 dirs)
- Created: `README.md`, `POSTS_LOG.md`, `data/README.md`,
  `takehome/{context-engine,orchestrator}/DESIGN.md`
- Created: `.env.example`, `.env` (project root)
- Created/replaced: `takehome/{context-engine,orchestrator}/.env.example`,
  `takehome/{context-engine,orchestrator}/.env`
- Downloaded: `takehome/context-engine/{evaluate.py,requirements.txt,TASK.md,SCORECARD.md}`
- Downloaded: `takehome/orchestrator/{evaluate.py,requirements.txt,TASK.md,SCORECARD.md}`
- Created: `reports/day01_phase1_report.md`, `explainers/day01_explainer.md`
- Initialized: `PROGRESS_LOG.md`
