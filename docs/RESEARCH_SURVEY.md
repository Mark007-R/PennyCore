# Research Survey: Production AI Infrastructure for Customer Service (2025-2026)

**Status:** Day-1 Phase-1 deliverable, back-filled on Day 4.
**Target question:** What does production-grade AI infrastructure for
customer service look like in 2025-2026, in the regulated-industry
niche that Decagon / Sierra / Parloa play in — and what does PennyCore
need to do to live in that company?

**Methodology.** Five sources surveyed. Public engineering-flavoured
content was fetched and read where available; where the public
material was business / CX-positioning rather than engineering depth,
this survey says so explicitly rather than papering over the gap.
Each source section ends with a "what PennyCore adopts / rejects"
note that ties findings back to concrete choices in the codebase.

---

## 1. Decagon — engineering blog (decagon.ai/blog)

### What was read
Six recent post titles + themes from the Decagon engineering blog:

1. *Why MCP alone isn't enough for reliable agent tool use* — argues
   that the Model Context Protocol is necessary but not sufficient for
   production-grade tool execution; reliability requires additional
   layers above the protocol (retry, validation, idempotency, output
   parsing).
2. *Optimizing GEPA for production: A test-driven approach to prompt
   engineering* — frames prompt engineering as a test-driven discipline:
   evaluation harness first, prompts iterated against frozen test
   suites.
3. *Designing low-latency AI agents through reranker optimization* —
   the reranker is presented as the primary latency lever in a
   retrieval-augmented agent, not the LLM.
4. *Why off-policy training isn't enough: SFT, RL, and the limits of
   imitation* — supervised fine-tuning plateaus on agent behaviour;
   reinforcement learning is required to push past imitation. (Out of
   scope for PennyCore — we don't train.)
5. *Bayesian VAD: Fixing turn detection in production voice pipelines*
   — voice infrastructure detail; orthogonal to PennyCore's chat /
   email / SMS focus today.
6. *Introducing Decagon Labs* — 80%+ of model traffic now runs on
   in-house models; foundation-model-agnostic platform pattern.

### Key takeaways
- **Reranker-as-latency-lever.** The retrieval pipeline's reranker
  stage, not the LLM call, is the dominant latency contributor for
  retrieval-heavy agents. Implication for PennyCore Phase 5 (Day 24):
  the re-ranking experiment isn't just a quality story — it's a
  latency story too. A faster reranker may beat a more accurate one
  on overall p95.
- **Test-driven prompt engineering.** Prompts are code; they need a
  frozen evaluation set and a regression suite. Implication for
  PennyCore Phase 3 (Days 12-15): the 200-query benchmark dataset
  isn't a one-time evaluation harness, it's the regression suite for
  every subsequent prompt change.
- **Tool-call reliability is its own engineering layer.** Beyond MCP
  / structured outputs, production systems need retry-with-backoff,
  output-shape validation, idempotent re-execution. Implication for
  PennyCore Day 9 (LLM planner): the planner's structured output isn't
  trusted directly — it gets validated against `contracts/actions.py`
  before any executor sees it.

### What PennyCore adopts
- **Reranker as a first-class Phase-5 experiment** with latency *and*
  quality measured side-by-side, not just quality.
- **Pydantic-validated planner output** at the LLM-planner boundary
  (Day 9). Loose dicts never reach the executor.
- **Frozen 200-query benchmark** as the regression suite, not a
  one-shot eval.

### What PennyCore rejects
- **Custom-trained models.** Out of scope per the SYSTEM DISCIPLINE
  RULE. PennyCore stays on a single hosted LLM (Anthropic Claude as
  primary, Azure for the takehome adapter).
- **Voice infrastructure.** Out of scope. Channels are chat / email /
  SMS / API only.

---

## 2. Sierra — engineering blog (sierra.ai/blog) and Agent Data Platform

### What was read
Six recent post titles + the *Introducing Agent Data Platform* post
in detail:

1. *Context engineering: the key to great agents* (May 5, 2026) —
   premise: providing the LLM with optimal context at the right time
   is the core engineering challenge of agent systems.
2. *τ-voice: benchmarking real-time voice agents on real-world tasks*
   — 278-task benchmark with realistic audio + personas + noise.
3. *μ-Bench: open multilingual transcription benchmark* — measures
   speaker-intent preservation, not raw word-error-rate.
4. *Golden articles: Evaluating and improving search* — dynamic
   search-eval system that measures performance against real
   production conversations and feeds signals back for continuous
   improvement.
5. *Sierra launches fully PCI-compliant payments* — Level 1 PCI
   compliance inside an AI agent.
6. *Linnaeus and Darwin: Search models that drive higher resolution
   rates* — purpose-built retrieval + reranking models; "up to 16
   percentage points" over standard solutions.

The *Agent Data Platform* post adds: unified structured (CRM,
billing) + unstructured (chats, emails, calls) data layer, cross-
session and cross-channel context, deployed across "chat, phone,
SMS, email, contact center conversations, or via Headless API."

### Key takeaways
- **"Context engineering" as the core challenge.** Not just retrieval
  — the explicit work of deciding *what* the LLM sees on each turn.
  This is exactly the framing PennyCore's `context_engine` package
  takes; the Phase-3 comparison study (5 retrieval strategies on 200
  queries) is the "context engineering" experiment.
- **Unified structured + unstructured data layer.** Customer-service
  agents need both the conversation history (unstructured) and the
  CRM / billing record (structured). PennyCore's schema reflects this
  — `events` + `messages` are unstructured-flavoured, but
  `customer_identities`, `actions`, `audit_log` are structured.
- **Cross-session, cross-channel as a first-class concern.** Sierra's
  ADP unifies "everything your company knows about a customer —
  across sessions, channels, and systems." PennyCore's
  `customer_identities` table (UNIQUE on `(tenant_id, identity_kind,
  identity_value)`) is the same idea: one customer, many channels,
  one identity-resolution oracle.
- **PCI compliance and audit are first-class.** Sierra ships PCI
  Level-1 inside the agent. PennyCore's audit-log invariant (rule 16,
  every action proposal/approval/rejection/execution writes an audit
  row) and tenant-isolation invariant (rule 15) are the same family
  of regulated-industry constraints — without the same scope.
- **Purpose-built retrieval models beat generic vector search by
  double-digit percentage points.** "Up to 16 pp" on resolution rate
  — but they require training data Sierra has and PennyCore does
  not. PennyCore stays with off-the-shelf sentence-transformers for
  Phase 3 and accepts the ceiling.

### What PennyCore adopts
- **"Context engineering" framing** for the Phase-3 comparison study
  — this is the explicit narrative of Days 12-15.
- **Cross-channel customer-identity resolution** as a Day-6
  deliverable (`customer_identities` table is already designed for
  it; Day 6 wires the linker code).
- **Audit + tenant-isolation as invariants enforced at the DB
  layer**, not policed at the application layer. The Day-3 DDL
  already does this.

### What PennyCore rejects
- **Custom retrieval models.** No training in scope; off-the-shelf
  sentence-transformers (384-dim) are the Phase-3 baseline.
- **PCI Level-1 compliance.** Out of scope for a 35-day demo;
  PennyCore claims "audit-trail-friendly" not "PCI-compliant."

---

## 3. Parloa — blog (parloa.com/resources/blog)

### What was read
Visible blog topics from the index page:

- "Key findings from Parloa's first-of-its-kind state of agentic CX
  study" (News)
- "Crossing the AI divide requires AI you can stand behind"
  (Responsible AI)
- "We obsess over winning customers. Why not Customer experience?"
  (CX Economics)
- "Healthcare has an outcomes problem" (News)
- "The Agent Architects at Parloa: a multidisciplinary approach to
  human-centered AI" (Agent Lifecycle)
- "Lessons from CCW 2026 on agentic AI, partners, and hybrid CX"
  (AI Enterprise)
- "AI Is growing up, and so is customer experience" (AI Enterprise)
- "The complexity trap: why your agent implementation is stalling
  before it starts" (Agent Lifecycle)

### Honest gap
Parloa's public blog is **business / CX positioning, not engineering
depth.** None of the surfaced posts cover voice architecture, channel
normalization, customer linking, or multi-tenant infrastructure at a
technical level. The Decagon and Sierra blogs gave usable
architectural signals; Parloa's didn't.

### Salvageable signal
- *"The complexity trap"* — the framing that agent implementations
  fail because of accumulated complexity, not insufficient capability,
  matches PennyCore's SYSTEM DISCIPLINE RULE (no microservice
  fragmentation, two services not eight).
- *"Agent Architects… multidisciplinary approach"* — implicitly
  endorses the "infrastructure engineer + ML person + compliance
  person" team shape. PennyCore is solo; the discipline rule
  substitutes for the team.

### What PennyCore adopts
- The "complexity trap" framing as a discipline anchor for Phase-5
  decisions: when in doubt, take the simpler path.

### What PennyCore rejects
- Nothing specific — no concrete architectural claim was actionable
  from the public posts.

---

## 4. Anthropic — Claude for customer service (claude.com/customers and Anthropic docs)

### What was read
The customer-stories index page on `claude.com/customers` is a
directory of case studies, not technical documentation. Industry
categories include "Customer support" but the index does not surface
architectural detail. The page redirects from the legacy
`anthropic.com/customers` URL.

### Architectural patterns inferred from Anthropic's broader public docs
(general background, not pulled from this fetch):

- **Tool use with structured outputs.** Claude's tool-use API enforces
  JSON-schema'd outputs, which becomes the planner's contract surface
  in PennyCore Day 9.
- **Prompt caching.** Anthropic's API supports prompt caching at
  block boundaries; for an agent that re-sends the same system prompt
  + tool definitions on every turn, this is the obvious cost lever.
  Phase 5 Day 25 (semantic caching) experiment is adjacent — but
  prompt caching at the API layer is a separate, cheap win that
  PennyCore can adopt without an experiment.
- **Streaming + interrupt-friendly responses.** Production customer-
  service systems need to surface partial responses. PennyCore's
  brief assembler returns a static brief, not a streamed one — that's
  fine for the demo but worth noting as a Phase-7+ deferral.

### Key takeaways
- **Tool use as the planner contract.** The orchestrator's LLM
  planner (Day 9) uses Anthropic tool-use to return structured
  proposals. The tool definition lives in `contracts/actions.py`
  vocabulary; the LLM cannot propose an action that isn't in the
  enum.
- **Prompt caching is free latency + cost.** Day 5+ ingestion path
  should use prompt caching for the system prompt + brief-assembly
  template once real LLM calls land.

### What PennyCore adopts
- **Tool-use for the planner**, with the tool schema generated from
  `contracts/actions.py` (single source of truth across DB schema +
  Pydantic + LLM tool definitions).
- **Prompt caching** as a Phase-2 cost lever (no experiment needed —
  it's a config flag).

### What PennyCore rejects
- **Streaming responses.** Out of scope for the demo; the brief
  assembler returns a complete brief.

---

## 5. MultiWOZ — dataset (github.com/budzianowski/multiwoz)

### What was read
The MultiWOZ README and dataset description.

- **Scale.** 10,000 dialogues. 3,406 single-domain + 7,032 multi-
  domain (2-5 domains per dialogue).
- **Annotation.** Each dialogue includes a goal specification, user +
  system utterances, and belief-state annotations (semi-slots, book
  slots, booked entities).
- **Tasks.** Dialog State Tracking (DST) and Response Generation are
  the two primary benchmark tasks.
- **Versions.** 1.0 → 2.0 (original EMNLP paper) → 2.1 (Amazon
  annotation corrections) → 2.2 (additional corrections). Use 2.2
  for new work.
- **Licensing.** MIT — research and commercial use OK with
  attribution.

### Key takeaways
- **MultiWOZ is the right dataset for the Phase-3 customer-history
  benchmark.** Real human-human task-oriented dialogues, multi-turn,
  span 2-5 domains per dialogue. That's the "100+ message customer
  history" slice the Phase-3 comparison study needs.
- **MIT license** = no friction on a public-portfolio repo.
- **Use version 2.2** for the cleanest annotations; 2.1's
  Amazon-corrected data is a fallback if 2.2 has any specific issue
  for our use case.
- **DST and Response Generation are the standard tasks** — but
  PennyCore's task is *retrieval quality given a query*, not DST or
  response generation. We use MultiWOZ for the conversation histories
  and write our own ground-truth queries against them. The dataset's
  belief-state annotations are not directly used.

### What PennyCore adopts
- **MultiWOZ 2.2 as the source of conversation histories** for the
  Phase-3 benchmark. 50 hand-written queries + 150 generated and
  hand-validated against MultiWOZ histories = the 200-query dataset
  the SKILL specifies.

### What PennyCore rejects
- **DST and Response Generation as our metrics.** Our metric is
  retrieval quality (LLM-as-judge on response faithfulness vs ground
  truth), not the standard MultiWOZ tasks.

---

## Synthesis: What production-grade looks like in 2025-2026

Five themes cut across the sources actually engaged with:

1. **Context is the engineering work, not retrieval.** Sierra calls
   it "context engineering"; Decagon's GEPA post treats prompts as
   tested code. Both reject the framing where you "just dump history
   into the LLM." The work is deciding *what* the LLM sees per turn,
   under a token budget, with measurable retrieval quality. PennyCore's
   `context_engine` package and the Phase-3 5-strategy comparison are
   exactly this.

2. **The retrieval pipeline's reranker is the latency bottleneck, not
   the LLM.** Decagon ships a whole post on it. For an agent doing
   100 rps, optimising the reranker beats optimising the model call.
   PennyCore's Phase-5 reranking experiment (Day 24) needs to measure
   latency, not just quality.

3. **Cross-channel + cross-session identity is a first-class data-
   layer concern.** Sierra's Agent Data Platform is built around it.
   PennyCore's `customer_identities` table with the
   `(tenant_id, identity_kind, identity_value)` UNIQUE constraint is
   the same idea — the linker (Day 6) is the code that walks it.

4. **Reliability + audit + multi-tenancy are infrastructure
   invariants, enforced at the layer below business logic.**
   Sierra's PCI compliance, Decagon's "MCP-isn't-enough" post on
   tool-call reliability — both treat reliability + compliance as
   infrastructure, not application code. PennyCore's Day-3 DDL puts
   tenant isolation and idempotency at the database layer, not the
   service layer. Same idea, smaller scope.

5. **Off-the-shelf foundation models + thin wrappers don't reach the
   bar in 2026.** Decagon Labs (80%+ in-house traffic), Sierra's
   Linnaeus/Darwin retrieval models, Anthropic's tool-use protocol —
   all converge on "the foundation-model-as-API isn't enough; the
   value lives in the *infrastructure* you build around it."
   PennyCore explicitly does *not* train models (out of scope), but
   compensates by building deeper infrastructure (idempotency,
   isolation, audit, hybrid retrieval, policy externalization).
   That's the portfolio narrative: "I built the production
   infrastructure that lets a stock-foundation-model agent be safe
   to deploy in regulated industries."

## How this reshapes (or confirms) the PennyCore design

The survey **confirms** rather than reshapes most of the existing
design:

- The 5-strategy retrieval comparison (Phase 3, Days 12-15) is
  Sierra's "context engineering" pattern — confirmed.
- The `customer_identities` cross-channel linker (Day 6) is Sierra's
  ADP pattern — confirmed.
- The audit-log + tenant-isolation invariants (Days 19-20) are
  industry-standard for the regulated niche — confirmed.
- The naive-baseline comparison (Phase 5, Days 27-28) is the
  "everything-into-the-prompt is the wrong default" thesis — both
  Sierra and Decagon implicitly endorse this.

The survey **adds** two concrete refinements:

- **Phase-5 reranker experiment must measure latency, not just
  quality.** Decagon's reranker-latency post is explicit on this.
  Update the Phase-5 plan accordingly when Day 24 starts.
- **Prompt caching at the LLM API layer** is a free Phase-2 cost
  lever. Apply when the LLM dispatch layer lands Day 5; no separate
  experiment needed.

The survey **flags** one honest gap:

- **Parloa's public blog did not yield engineering depth on voice
  channel architecture.** If voice ever enters PennyCore's scope
  (it doesn't, today), this survey would need to extend to the
  underlying voice-pipeline literature (Bayesian VAD, ASR latency,
  turn-taking) which Decagon's blog does cover.

## Sources

1. Decagon engineering blog — <https://decagon.ai/blog>
2. Sierra blog (Agent Data Platform post) —
   <https://sierra.ai/blog/agent-data-platform>
3. Sierra blog index — <https://sierra.ai/blog>
4. Parloa blog — <https://www.parloa.com/resources/blog/>
5. Claude customer stories — <https://claude.com/customers>
6. MultiWOZ dataset — <https://github.com/budzianowski/multiwoz>

## Provenance note

This survey was written on Day 4 (2026-05-07) as a back-fill of the
Day-1 task. The Day-1 report (`reports/day01_phase1_report.md`,
local-only) had staged the 5 sources as URLs but admitted *"these are
reading targets, not finished annotations."* The synthesis was
implicitly rolled into Day-2's `docs/SYSTEM_DESIGN.md` rather than
produced as a standalone artefact. This document closes that gap
without amending the Day-1 report — what shipped on Day 1 stays on
Day 1; the catch-up shipped on Day 4 stays on Day 4.
