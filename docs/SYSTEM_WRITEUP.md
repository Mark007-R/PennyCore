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

## Measurement honesty (read this before the numbers)

The benchmarks ran in `MOCK_LLM` mode — no live LLM key worked during
the runs. What that means for the numbers below:

- **Real, computed from execution on the real datasets:** brief token
  counts, retrieval/decision latency, correctness vs. expected-action
  sets, and cost (real token counts × published Sonnet prices). The
  200-pair retrieval set is 150 MultiWOZ-derived + 50 synthetic; the
  200-tuple policy set is synthetic across three tenants.
- **Mock-proxy, not a real LLM judge:** the quality scores are a
  deterministic token-overlap recall heuristic. It rewards verbatim
  text, so compression strategies score *lower* than a real judge
  would rate them (the results files carry an explicit
  `proxy_bias_note`).
- **Latency is local compute only** for the policy engines — the
  LLM-backed engines never made a network call, so their µs latencies
  are local overhead, not real end-to-end LLM latency.

Every number below is the actual value in
`results/phase3_day18_consolidated.json` and `results/phase5_*.json`.

## The headlines

### 1. Hybrid retrieval wins on the cost / quality-per-token frontier — cheaper at tied quality, not "faster and better"

The Phase-3 retrieval bake-off ran five strategies against 200
(customer-history, query, ground-truth-answer) pairs.

| Strategy | Brief tokens (mean) | Quality (mock-proxy, 1–5) |
|----------|--------------------:|--------------------------:|
| naive (dump everything) | 1,449.6 | 3.04 |
| recency-only | 1,421.7 | 3.04 |
| semantic-only | 1,421.7 | 3.04 |
| summarized | 211.0 | 1.69 |
| **hybrid (champion)** | **838.4** | **2.91** |

Hybrid is recency for the last 24 hours, semantic for older messages,
and a summarised digest for the cold tail. **It uses 42% fewer brief
tokens than naive and 41% fewer than recency, at parity fact recall on
183 of 200 pairs.** Note the honest wrinkle: under the mock-proxy
judge (which rewards verbatim text) hybrid's quality (2.91) sits just
*below* the verbatim strategies (3.04), because it compresses. The
champion is chosen on the **quality-per-1K-tokens** frontier — hybrid
3.47 vs recency 2.13 (+63%) — not on raw proxy quality. Phase 5
re-scored the fact-bearing subset: **naive and hybrid tie at 3.9/5**,
hybrid costs **24% less per 100 queries** ($0.585 vs $0.769), but is
**slightly slower** at p50 (0.48 ms vs 0.24 ms) because it does more
retrieval work. Numbers in
[`results/phase5_naive_vs_champion_context_engine.json`](../results/phase5_naive_vs_champion_context_engine.json).

### 2. Declarative YAML matches LLM-as-judge correctness at zero marginal cost

The Phase-3 policy bake-off ran four engines against 200
(event, tenant, expected-action) tuples across three tenants
(permissive / strict / mixed). Same expected-action set; same input.

| Strategy | Correctness | p50 latency (µs, mock/local) | Cost/100 (mock) | Cost/100 (prod proj.) | Auditability | Maintainability |
|----------|------------:|------------------------------:|----------------:|----------------------:|-------------:|----------------:|
| naive LLM | 54% | 1.8 | $0.139 | $0.57 | 2/5 | 4/5 |
| python_rules | 100% | 0.7 | $0 | $0 | 3/5 | 2/5 |
| **declarative YAML (champion)** | **100%** | **0.6** | **$0** | **$0** | **5/5** | **5/5** |
| llm_judge | 100% | 6.7 | $0.111 | $0.57 | 4/5 | 4/5 |

The naive-LLM baseline drops to **54% correctness and misses all 5
`reject` scenarios (0/5)** — it routes high-risk actions to
auto-execute, the silent-execution failure regulators audit for. The
two LLM engines tie the declarative champion on correctness for the
well-stated tenants, but they carry a **marginal LLM cost** — $0 for
declarative vs ~$0.57 per 100 decisions *projected* under real Sonnet
pricing (mock-run payload cost was $0.111–0.139) — and lose on
auditability and maintainability (a compliance team edits a YAML file
without a redeploy; they can't edit a model). Local decision latency
is ~11× lower (0.6 vs 6.7 µs), but that's local compute, not network.
Numbers in
[`results/phase3_day18_consolidated.json`](../results/phase3_day18_consolidated.json),
narrative in [`docs/POLICIES.md`](POLICIES.md).

This is the result the project is most willing to defend: **for clear
compliance policies, an LLM-as-judge is pure cost and audit overhead —
it does not buy better correctness than a declarative table.** The
right framing is "zero marginal cost vs a real per-decision LLM bill,"
not a headline multiplier (declarative's cost is $0, so a ratio is
undefined — earlier drafts wrongly cited "~17,000×"; that's been
removed).

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

For retrieval: hybrid is **24% cheaper** per 100 queries than the
dump-everything baseline ($0.585 vs $0.769) at **tied** mock-proxy
quality (3.9/5 both) — but **slightly slower** at p50 (the cost win
comes from fewer tokens, not less work). The per-bucket cost curve is
in
[`results/phase5_naive_vs_champion_cost_by_bucket.png`](../results/phase5_naive_vs_champion_cost_by_bucket.png);
the win widens on long histories (naive's very-long bucket costs
$2.87/100q vs hybrid's $0.72).

For policy: declarative-YAML correctness **100% vs. naive-LLM 54%**
(a 46-point gap), at **$0 vs ~$0.57 per 100 decisions** projected. The
frontier chart is in
[`results/phase5_naive_vs_champion_orch_frontier.png`](../results/phase5_naive_vs_champion_orch_frontier.png).

The honest headline for a post: *"I tested four policy-engine
strategies on 200 banking-action decisions. The 'best practice'
(LLM-as-judge) tied the declarative table on correctness — but cost a
real per-decision LLM bill, lost on auditability, and the naive
'just ask the LLM' variant silently auto-executed every action it
should have rejected. For clear compliance policies, declarative
wins."*

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
