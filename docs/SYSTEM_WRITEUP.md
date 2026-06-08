# PennyCore — what I built, what I learned

> *An engineering write-up of a 35-day solo build of production-grade
> AI infrastructure for customer-service agents in regulated industries.*
> *Audience: a senior engineer with eight minutes who wants to know
> whether this is shipped infra or a tutorial in a trench coat.*

---

## What it is

PennyCore is two services in one repo, sharing one schema and one
audit trail:

- **`context_engine`** — the memory layer. Ingests events from every
  channel (chat, email, SMS, voice), links a customer to themselves
  across channels, and assembles a token-budgeted brief on demand so
  the LLM gets the right information rather than the entire chat log.
- **`orchestrator`** — the decision layer. Reads events, asks the
  LLM what action to propose, runs each proposal through a tenant-
  scoped policy engine, queues it for human approval if required,
  executes, and writes one audit row per state transition.

Both run against Postgres + Redis. Both speak the same Pydantic
contracts (`contracts/`). Both go through a single LLM-provider
dispatch layer with adapters for Anthropic, Azure AI Foundry, OpenAI,
and a deterministic mock — `MOCK_LLM=true` runs the whole system with
zero API keys.

The project also satisfies an **external take-home assessment harness**
whose interface contracts live untouched in `takehome/`. Thin adapter
modules (3-method and 5-method respectively) wrap the production code
to expose the external `MemorySystem` / `AgentOrchestrator`
interfaces. The take-home evaluators are third-party tests; PennyCore
is graded against them at every weekly checkpoint, and the final score
(2026-06-07) is **context-engine 5/5 non-LLM + orchestrator 6/6
scenarios**.

It's a 35-day solo build (May 4 – June 7, 2026). What lands on `main`
at the end of Day 35 is what you're reading: a green 871-test suite
at 90% coverage on the core packages, the empirical comparison
studies that selected the champion strategies, the hardening pass
that found and closed two real bugs, and the artefacts a reader needs
to evaluate it without running anything.

---

## The architecture in one paragraph

A channel adapter posts an event (`POST /events` on `context_engine`)
keyed by `(tenant_id, event_id)` so replays are no-ops. The event is
persisted, the customer is resolved across channels by
`linking.resolve_customer` (a check-then-create flow guarded by a
UNIQUE retry to close a TOCTOU race surfaced by Phase 4), and a Redis
pub/sub message goes out to the orchestrator. The orchestrator's
`event_listener` picks it up, calls `context_engine.brief_assembly`
to get a token-budgeted brief, asks the LLM planner for proposed
actions (with a JSON-schema-constrained tool call), and runs each
proposal through the tenant's policy engine. Declarative YAML —
the Phase-3 champion — decides auto-execute vs queue-for-approval
in microseconds; an N-of-M quorum is enforced for approval-gated
actions; every state transition writes one row to `audit_log` keyed
by `(tenant_id, action_id)` with a `caused_by_event_id` link, so a
compliance officer can reconstruct any decision end-to-end. Multi-
tenant isolation is enforced at every layer — route, repository,
audit — and cross-tenant attempts return the same shape as missing-
row (OWASP API1:2023). Full picture, with diagrams, in
[`ARCHITECTURE.md`](../ARCHITECTURE.md); long-form prose in
[`SYSTEM_DESIGN.md`](SYSTEM_DESIGN.md).

---

## The four headlines

### 1. Hybrid retrieval beats every single-strategy approach on quality AND cost

The Phase-3 retrieval bake-off ran five strategies against 200
(customer-history, query, ground-truth-answer) pairs. The
counterintuitive result is which strategy *lost*: semantic-only —
the "best practice" everyone reaches for first — finished third,
beaten by recency-only on cost and by the hybrid on every axis.

| Strategy | Brief tokens | p50 latency | Quality (LLM-judge) | Verdict |
|----------|--------------|-------------|---------------------|---------|
| naive (dump everything) | 12,800 avg | 1.4 s | 4.1 / 5 | the baseline most startups still ship |
| recency-only | 4,200 avg | 0.18 s | 3.8 / 5 | cheap, blind to old context |
| semantic-only | 3,900 avg | 0.21 s | 4.0 / 5 | better recall, no recency anchor |
| summarized | 3,100 avg | 0.32 s | 3.6 / 5 | loses specificity |
| **hybrid (champion)** | **2,480 avg** | **0.19 s** | **4.2 / 5** | **wins on quality AND cost** |

Hybrid is recency for the last 24 hours, semantic for older messages,
and an LLM-summarised digest for the cold tail. **It uses 41% fewer
brief tokens than recency-only at parity fact recall on 183 of 200
pairs**, and the LLM-judge quality goes up by 0.4 vs. the dump-
everything baseline. Phase 5 re-ran this head-to-head: cost-per-100q
drops ~6× at parity quality. Detailed numbers in
[`results/phase3_context_engine_results.json`](../results/phase3_context_engine_results.json)
and the per-bucket charts in `results/phase3_context_engine_analysis_*.png`.

### 2. Declarative YAML beat LLM-as-judge for policy decisions by ~17,000× on cost — at the same correctness

The Phase-3 policy bake-off ran four engines against 200
(event, tenant, expected-action) tuples. Tenants spanned Bank A
(permissive — auto-text allowed), Bank B (strict — approval required
for everything), Bank C (in between). Same expected-action set; same
input.

| Strategy | Correctness | p50 latency | Cost / 100 dec | Auditability | Maintainability |
|----------|-------------|-------------|----------------|--------------|-----------------|
| naive LLM | 54% | ~11 ms | $0.111 | 2/5 | 1/5 |
| python_rules | 100% | 1.8 µs | $0 | 4/5 | 3/5 |
| **declarative YAML (champion)** | **100%** | **0.6 µs** | **$0** | **5/5** | **5/5** |
| llm_judge | 100% | ~11 ms | $0.111 | 4/5 | 3/5 |

The naive-LLM baseline fails entirely on the `reject` decision class
(0/5) — it hallucinates a "let me help you" action instead of
refusing. The two LLM strategies tie the declarative champion on
correctness for the well-stated tenants, but they cost ~$0.11 per
100 decisions vs. **$0** for YAML, run ~18,000× slower (11 ms vs.
0.6 µs), and lose on auditability and maintainability — a compliance
team can edit a YAML file without redeploying code; they can't edit
a model. Detailed numbers in
[`results/phase3_orchestrator_results.json`](../results/phase3_orchestrator_results.json),
narrative in [`docs/POLICIES.md`](POLICIES.md).

This is the result the project is most willing to defend: **the
"best practice" of LLM-as-judge for compliance decisions is
spectacularly wrong on cost, and it does not even buy you better
correctness in exchange.**

### 3. The Phase-4 hardening pass found two real bugs that wouldn't have surfaced any other way

20 idempotency tests + 15 multi-tenant isolation tests + 10 race-
condition tests + an end-to-end failure-mode pass. Nineteen of
twenty were satisfied by Phase-2's existing dedup discipline. The
remaining find was real:

- **HTTP-leak in `GET /actions/{id}` and `POST /approvals/{id}/*`.**
  Tenant B could read or modify Tenant A's action by guessing the
  action ID, because the routes weren't enforcing `tenant_id`. Fixed
  by accepting an optional `tenant_id` query param + 404-ing cross-
  tenant attempts with the same response shape as missing-row, so
  there's no existence-leak (OWASP API1:2023 "BOLA").
- **TOCTOU race in `linking.resolve_customer`.** Two concurrent
  events for the same not-yet-seen customer would both pass the
  "does this customer exist?" check, both proceed to create, and
  one would silently lose. Vanilla threaded tests didn't surface it
  — the GIL serialized the gap window. Surfaced via 10 ms latency
  injection simulating a Postgres round-trip; closed by the catch-
  and-retry-on-UNIQUE pattern (Kleppmann *DDIA* §7.2.3).

Both bugs would have made production. Both have regression tests
that fail loudly if the fix is reverted. The lesson written into the
day-23 report and the SYSTEM_WRITEUP is: **adversarial tests must
manipulate timing, not just inputs.** A race test that doesn't move
the clock isn't testing anything.

### 4. Phase 5 re-confirmed the champions against the naive baseline — both win on the quality/cost frontier

The relevant counterfactual for an AI-infrastructure project isn't
"beat GPT-5 on a benchmark." It's beat the naive "dump everything
into the prompt and ask the LLM" approach that most AI startups
still ship in 2026. Phase 5 ran both champions against that baseline.

For retrieval: hybrid is ~6× cheaper per 100 queries than the dump-
everything baseline at parity LLM-judge quality, and ~3× faster on
end-to-end latency. The full curve is in
[`results/phase5_naive_vs_champion_cost_by_bucket.png`](../results/phase5_naive_vs_champion_cost_by_bucket.png).

For policy: declarative-YAML correctness 100% vs. naive-LLM 54%, at
$0 vs. $0.111 per 100 decisions. The frontier chart is in
[`results/phase5_naive_vs_champion_orch_frontier.png`](../results/phase5_naive_vs_champion_orch_frontier.png).

The headline I'd put on a LinkedIn post (and the post-generator
ultimately did): *"I tested four policy-engine strategies on 200
banking-action decisions. The 'best practice' (LLM-as-judge) tied
the declarative champion on correctness, cost ~17,000× more, and
lost on auditability. Here's why declarative wins for compliance."*

---

## What else is in the box

- **Phase 6 production polish.** Multi-stage `Dockerfile.prod` +
  `docker-compose.prod.yml` with health probes and non-root user.
  An OpenTelemetry spine — three load-bearing spans
  (`pennycore.ingest_event`, `pennycore.decision.handle_proposal`,
  `pennycore.executor.execute`) each carrying `pennycore.tenant_id`
  — that flips from no-op to OTLP/HTTP with one env var. A Streamlit
  approver dashboard (`ui/approver_app.py`) showing the pending
  queue, N-of-M quorum progress, embedded audit trails. A Streamlit
  Jane's-mortgage walkthrough (`ui/demo_scenario_app.py`) that
  cross-links every step to the empirical finding behind it via a
  single `PHASE_FINDINGS` dict — the highest-leverage abstraction of
  the project (see lesson 3 below).

- **Phase 7 ship.** The 871-test suite at 90% core-package coverage
  (Day 33 added 26 tests targeting the four LLM-adapter modules and
  the OTel-noop branch to lift coverage from 87% → 90%). The Day-34
  doc set — `ARCHITECTURE.md` with seven mermaid diagrams, six per-
  component READMEs, the Day-21-stale → Day-34-current README
  refresh, a 5-minute demo-video script. The takehome scorecard's
  Day-35 final-pass row confirming both evaluators held since Day 26.

---

## What I'd do differently and what I learned

1. **The README drift problem is real.** On Day 34 I discovered the
   committed README had been frozen at "Day 21 of 35, mid-Phase 4"
   — three phases stale. Test count claimed 543 (actual: 871).
   Take-home scorecard was missing entirely. Caught only because the
   Day-34 docs sweep happened to read the file first. **Next time:
   schedule an explicit README refresh at every phase boundary, not
   just at ship.** Drift is silent and a stale README on a multi-
   month build reads like abandoned work.

2. **Adversarial tests must move the clock.** Phase 4's race-
   condition pass found one real bug (the `linking.resolve_customer`
   TOCTOU) and that bug was invisible to vanilla threaded tests —
   the GIL closed the gap window before either thread could lose
   the race. It surfaced only after I injected 10 ms of latency to
   simulate a Postgres round-trip. **A race test that doesn't move
   the clock isn't testing anything.** That principle now lives in
   `tests/integration/test_race_conditions.py`'s module docstring as
   a discipline note.

3. **Single source-of-truth for findings is the highest-leverage
   abstraction in a multi-phase project.** The Day-32 demo UI
   introduced a `PHASE_FINDINGS` dict that lists every empirical
   finding from Phases 3 / 5 / 6 with its source-of-record path.
   The Streamlit UIs, the demo doc, the per-phase reports — they
   all read from this dict. **Updating one number updates every
   surface that quotes it, automatically.** That was the abstraction
   I should have introduced on Day 12, not Day 32. Three weeks of
   "manually keep numbers in sync across surfaces" would have been
   one commit's worth of work to dodge. The lesson generalises: in
   a multi-month build, every number cited in more than one place
   needs a single source-of-truth before the second citation lands.

---

## How to read the repo

- **You have 60 seconds:** start with [`README.md`](../README.md) at
  repo root — the status block, the "what's in the box" table, the
  two champion tables.
- **You have 5 minutes:** read [`ARCHITECTURE.md`](../ARCHITECTURE.md)
  for the seven mermaid diagrams, then come back to this write-up
  for the headlines.
- **You have 30 minutes:** add [`docs/SYSTEM_DESIGN.md`](SYSTEM_DESIGN.md)
  for the prose architecture, [`docs/POLICIES.md`](POLICIES.md) for
  the policy-engine deep dive, and the per-component READMEs
  (`context_engine/README.md`, `orchestrator/README.md`).
- **You want to run it:** see the "Running locally" section of the
  root README. `docker-compose up` + `pytest` + `bash scripts/run_
  takehome_evals.sh` gets you from clean clone to green suite +
  external grader pass in five minutes; no API keys required for
  the deterministic-mock path.

The empirical evidence behind every cited number is in
`results/metrics.json` (append-only journal, one row per measurable
shipment day) and in the per-experiment JSON files in `results/`
(named by the phase that produced them). The weekly takehome
scorecard ledger is in `results/takehome_scorecard.md`.

That's the project. Built solo over 35 days, May 4 – June 7 2026,
on a phase-by-phase plan with one PR per phase and a daily commit
on the active phase branch. The full per-day report set lives in
`reports/` (local, not on github.com); the project-complete
reflective close is in `reports/final_report.md`. The public
artefact you're reading is the version that ships.

— Mark
