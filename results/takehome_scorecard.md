# Takehome scorecard — running tally

Captured against the unmodified upstream `evaluate.py` files in
`takehome/context-engine/` and `takehome/orchestrator/`. The
"effective pass rate" column counts scenario 6 as N/A in our
environment (it requires a working `OPENAI_API_KEY` + `openai` SDK
installed in the takehome venv — `LLM_PROVIDER=azure` and the
project-root `.env` route around it for the production stack but
scenario 6 reads `OPENAI_API_KEY` directly from env, ignoring our
provider switch).

## context-engine

| Date | Day | Scenarios passed | Effective pass rate | Notes |
|------|-----|------------------|---------------------|-------|
| 2026-05-10 | 7 | 5/6 (1, 2, 3, 4, 5) | **5/5 non-LLM** | scenario 6 fails: `openai` SDK absent in takehome venv. Adapter exit code 0 (passed >= total - 1). |

### Day 7 — 2026-05-10 — first run

```
Loaded: PennyCoreMemorySystem

  [PASS] 1. Basic ingest and context assembly
         Key borrower facts present in context
  [PASS] 2. Token budget is respected
         Context fits budget (~163 tokens, budget 200)
  [PASS] 3. Cross-channel continuity
         Context unifies information across chat, email, and SMS
  [PASS] 4. Action memory
         Actions are tracked and surfaced in context
  [PASS] 5. Recency prioritization
         Recent conversation is prioritized under tight token budgets
  [FAIL] 6. LLM integration
         LLM call failed: No module named 'openai'

  Result: 5/6 scenarios passed   (exit 0)
```

**Self-rubric estimate against `SCORECARD.md` (Day 7 baseline — recency-only):**

| Section | Criteria | Self-score |
|---------|----------|------------|
| Memory Architecture | A1 storage model | 2 (typed `BriefSegment`, indexed by borrower) |
| | A2 multi-tier / prioritized | 1 (recency-only today; ranked) |
| | A3 summarization / compression | 0 (no summarization yet — Phase 5 lands LLM-summarized strategy) |
| | A4 staleness / decay | 1 (timestamp priority is implicit decay) |
| | A5 extensibility | 2 (BriefSegment + Protocol — semantic / hybrid drop in without core changes) |
| Context Assembly | B1 budget enforcement | 2 (defensive end-to-end check + per-segment skip-don't-stop) |
| | B2 token estimation | 1 (matches evaluator's `chars // 4`; tiktoken is Phase 5) |
| | B3 prioritization | 1 (recency only Day 7; multi-signal lands Phase 3) |
| | B4 context structure | 2 (channel-prefixed lines + action-prefixed lines + header) |
| | B5 budget allocation | 1 (header reserved; greedy after) |
| Cross-Channel | C1 unified view | 2 (one borrower, all channels in one timeline) |
| | C2 channel-aware context | 2 (`[chat]` / `[email]` / `[sms]` markers in every segment) |
| | C3 channel metadata | 2 (subject + attachments rendered for email) |
| Action Memory | D1 action storage | 2 (full Action with details + timestamp; queryable via `get_actions`) |
| | D2 actions in context | 2 (formatted `[action] <ts> <type> (k=v;...)`) |
| | D3 deduplication awareness | 1 (priority boost surfaces actions; explicit dedup pass not yet) |
| Implementation | E1 evaluate.py pass rate | 2 (5/5 non-LLM) |
| | E2 LLM scenario | 0 (skipped — `openai` SDK / live key absent) |
| | E3 code quality | 2 (typed, docstrings, module split) |
| | E4 error handling | 1 (zero/negative budget raises; unknown borrower returns header-only — more edge-case tests Day 19+) |
| | E5 LLM usage | 0 (no LLM in adapter today — Phase 5 adds summarization) |

**Estimated core score: 29 / 42 (Day 7 baseline)**
**Estimated bonus:** F1 +2 (DESIGN.md exists), F4 +1 (token cost discipline). Total ~32.

Phase 5 work (semantic retrieval, LLM-summarized strategy, hybrid champion) is what bumps A2/A3/B2/B3/E2/E5 from 0-1 toward 2 and pushes the score into the 36-40 range.

## orchestrator

(Not started — Day 10 deliverable.)
