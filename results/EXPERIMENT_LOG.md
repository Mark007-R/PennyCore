# Phase-3 Experiment Log — context-engine comparison study

Running journal of every comparison run on the Phase-3 benchmark
dataset (200 `(customer_history, query, ground_truth)` pairs,
`benchmarks/data/manifest.json`). New entries go at the top —
chronological. Per-day technical reports live in `reports/dayNN_*`;
this file is the cross-day index so anyone (including future-me) can
trace the strategy table back to the specific run that produced a row.

Schema for each entry:

```
## Day NN — YYYY-MM-DD — <one-line headline>
- Strategies run: <list>
- Token budgets: <per-strategy>
- Quality scoring: <yes/no (LLM-as-judge lands Day 15)>
- Results artifact: <path>
- Key numbers: <terse>
- Verdict so far: <terse>
```

---

## Day 15 — 2026-05-18 — Hybrid completes the 5-strategy field + quality column lights up (mock proxy)

- **Strategies run:** `naive_dump`, `recency`, `semantic`, `summarized`,
  `hybrid` (full 5-way comparison).
- **Token budgets:** naive=50K; recency / semantic / summarized /
  hybrid = 8K each.
- **Quality scoring:** **YES — mock-mode token-recall proxy.**
  The Day-15 `benchmarks.judge` module ships with two paths
  sharing one interface; the mock-proxy path is the only one
  active today (no `ANTHROPIC_API_KEY` in `.env`). Proxy
  definition: `quality = 0.5*recall(query_tokens in brief) +
  0.5*recall(gt_tokens in brief)`, mapped to 1-5 via thresholds
  `(0.20, 0.40, 0.60, 0.80)`. **Bias note:** fact-recall is
  biased toward verbatim-preserving strategies; compression
  strategies (summarized, hybrid cold-tail) score lower under
  this proxy than under a real LLM-as-judge. Day-18 / Phase-5
  re-run with `LLM_PROVIDER=anthropic` produces the canonical
  Quality column.
- **Results artifacts:**
  - `results/phase3_context_engine_results.json` (overwritten —
    1000 rows: 200 pairs × 5 strategies; every row now carries
    `quality_score`, `query_recall`, `ground_truth_recall`,
    `judge_mode`, `judge_notes` + a top-level `judge_summary`
    block).
  - `results/phase3_day15_analysis.json` (new — per-strategy
    quality aggregates + hybrid vs recency / summarized per-pair
    beat/match/lose buckets + the proxy bias note).
- **Key numbers (200-pair aggregates):**

  | Strategy    | Budget | Avg brief tokens | Lat p50 (ms) | Lat p95 (ms) | Mean quality (mock proxy) |
  |-------------|-------:|-----------------:|-------------:|-------------:|--------------------------:|
  | naive_dump  | 50,000 |          1,449.6 |         0.24 |         0.87 |                     3.035 |
  | recency     |  8,000 |          1,421.7 |         0.22 |         1.02 |                     3.035 |
  | semantic    |  8,000 |          1,421.7 |         0.69 |         4.76 |                     3.035 |
  | summarized  |  8,000 |            211.0 |         0.12 |         0.43 |                     1.685 |
  | hybrid      |  8,000 |            838.4 |         0.42 |         1.42 |                     2.910 |

  - **Hybrid vs recency per-pair (quality):** match **183**,
    beat **0**, lose **17**. Loss bucket: 11 of 30 long pairs +
    6 of 10 very_long pairs. Tie everywhere else.
  - **Hybrid vs summarized per-pair (quality):** beat **134**,
    match **66**, lose **0** — strict dominance under the proxy.
  - **Naive / recency / semantic quality histograms are
    bit-identical:** 28 / 41 / 55 / 48 / 28 for scores 1-5.
    They emit byte-identical briefs on 190/200 pairs, so the
    proxy ties them. Day-14 already showed this for tokens
    and content; Day 15 confirms it for the mock-mode quality
    column.
  - **Token compression at parity quality (short + medium):**
    hybrid emits 474 tokens (short) and 1,123 tokens (medium)
    vs recency's 474 / 1,127 — essentially identical because
    these histories have no cold tail. On long: hybrid 1,338
    vs recency 3,014 (56% smaller); on very_long: hybrid 1,274
    vs recency 7,887 (84% smaller). The "compress only when
    there's something to compress" pattern is the hybrid
    feature, not a side effect.
- **Verdict so far:** Three findings, in priority order:
  1. **Hybrid is the cost-frontier strategy under mock-mode
     quality.** Emits 41% smaller briefs than recency on
     aggregate while matching recency's fact recall on 183 of
     200 pairs (91.5%). The 17 pairs where it loses are all on
     long / very_long buckets and the loss is the mock-summary's
     fault, not the strategy's — a real LLM summary is expected
     to close that gap (Day 18 re-judge).
  2. **The mock-mode quality proxy is honest but biased.** It
     correctly identifies naive / recency / semantic as
     indistinguishable on this dataset (they emit byte-identical
     briefs on 190/200 pairs). It correctly punishes mock-mode
     summarization (which is a 200-char head, not a real
     summary). It under-states what a real LLM would credit
     a coherent summary with. All numbers are flagged
     `judge_mode: "mock_proxy"` in every row.
  3. **The judge interface is provider-agnostic by design.**
     Day-18 / Phase-5 swap-in is one config change
     (`LLM_PROVIDER=anthropic` in `.env`); the same module
     re-reads the same `phase3_context_engine_results.json`
     artifact and replaces the `quality_score` column with
     real-LLM judgments. No re-retrieval needed (brief text is
     preserved per row).

---

## Day 14 — 2026-05-17 — Semantic + summarized join the 4-strategy field

- **Strategies run:** `naive_dump`, `recency`, `semantic`, `summarized`
  (full 4-way comparison; hybrid ships Day 15)
- **Token budgets:** naive=50K, recency/semantic/summarized=8K
- **Quality scoring:** no — Day-15 LLM-as-judge step still pending;
  retrieval-only comparison (tokens, latency, content divergence,
  compression ratio).
- **Results artifacts:**
  - `results/phase3_context_engine_results.json` (overwritten, now
    800 rows: 200 pairs × 4 strategies, brief text preserved)
  - `results/phase3_day14_analysis.json` (new — per-pair Jaccard
    overlap and per-bucket compression breakdown for the
    semantic/summarized cohort)
- **Key numbers (200-pair aggregates):**

  | Strategy    | Budget | Avg brief tokens | Sat % | Lat p50 (ms) | Lat p95 (ms) |
  |-------------|-------:|-----------------:|------:|-------------:|-------------:|
  | naive_dump  | 50,000 |          1,449.6 |   2.9 |         0.24 |         1.15 |
  | recency     |  8,000 |          1,421.7 |  17.8 |         0.24 |         1.40 |
  | semantic    |  8,000 |          1,421.7 |  17.8 |         0.80 |         3.54 |
  | summarized  |  8,000 |            211.0 |   2.6 |         0.16 |         0.68 |

  - **Semantic vs recency Jaccard (line-level overlap of briefs):**
    1.000 on short/medium/long (190 pairs — both pack the entire
    history under 8K, so the ranking signal changes nothing about
    *what* gets in); **0.922 mean, 0.783 min** on very_long
    (10 pairs — 9/10 diverge because the budget saturates and the
    ranking signal finally has work to do).
  - **Summarized tokens saved vs recency, by bucket:**
    short **263**, medium **913**, long **2,803**, very_long
    **7,699** tokens per pair on average. Total across 200 pairs:
    **242,135 tokens saved**. Compression ratio recency → summarized:
    **6.7×** on aggregate, **42×** on very_long.
  - **Semantic latency premium:** 3.4× recency at p50 (0.80 vs
    0.24 ms); 2.5× at p95 (3.54 vs 1.40 ms). On very_long histories
    (302-314 messages per pair) semantic p50 is 7.83 ms vs recency's
    2.35 ms — a 5.48 ms delta, which works out to ~18 µs per extra
    message processed (delta ÷ message count, not an isolated
    microbench).
- **Verdict so far:** Three findings, in priority order:
  1. **Semantic earns its keep only when the budget saturates.**
     On 95% of pairs, semantic and recency emit the *same brief* —
     the ranking signal is invisible because everything fits. The
     comparison story is decided on the 10 very_long pairs (Day 15
     LLM-as-judge will measure which strategy's *selection* on
     those pairs wins on response quality).
  2. **Summarized's compression is real but mock-inflated.** A
     7× token reduction is correct under mock mode where the
     summary is 200 chars of input echo. A real LLM summary would
     be denser (more information per token) but also longer —
     probably 600-1000 tokens, narrowing the ratio to ~2-3×. The
     PATTERN holds either way: summarized never saturates the
     budget, never drops content, and stays cheapest in latency.
  3. **Latency is not the dominant cost on this dataset.** All four
     strategies finish in <10ms p95 on a single thread. The
     Day-15 LLM-as-judge step adds 100-2000 ms per pair from the
     LLM call — retrieval latency is rounding error against that.

---

## Day 12 — 2026-05-15 — Recency baseline ran on the 200-pair benchmark (back-filled 2026-05-18)

- **Strategies run:** `recency` only (the Day-7 production baseline,
  promoted into the Phase-3 harness as the row-1 reference number
  the Day-13/14/15 strategies fight against).
- **Token budgets:** recency = 8K (production setting, locked Day 7).
- **Quality scoring:** no — LLM-as-judge module not built until
  Day 15. Day 12 is the dataset + harness build day; this row is
  retrieval-only (tokens, latency, budget saturation per bucket).
- **Results artifacts:**
  - `results/phase3_context_engine_results.json` (200 rows: 200
    pairs × 1 strategy; overwritten on Days 13/14/15 as new
    strategies joined the harness).
  - `results/phase3_context_engine_smoke.json` (5-pair smoke
    fingerprint, preserved across days for CI).
  - `benchmarks/data/{customer_histories,queries}.jsonl` +
    `manifest.json` (the canonical 200-pair dataset itself —
    seed=42, sha256-locked).
- **Key numbers (per-bucket recency baseline):**

  | Bucket    |   n | avg msgs | avg tokens | budget sat | p50 ms | p95 ms |
  |-----------|----:|---------:|-----------:|-----------:|-------:|-------:|
  | short     | 100 |    17.4  |     474.3  |    5.9 %   | 0.137  | 0.234  |
  | medium    |  60 |    38.6  |   1,126.9  |   14.1 %   | 0.293  | 0.354  |
  | long      |  30 |   109.1  |   3,014.2  |   37.7 %   | 0.832  | 0.989  |
  | very_long |  10 |   307.0  |   7,886.8  |   98.6 %   | 2.339  | 2.578  |

  Overall: **0.24 ms p50 / 1.06 ms p95 / 1,421.7 avg tokens** per
  brief.
- **Verdict so far:** Three observations the rest of Phase 3 builds on:
  1. **Recency latency scales linearly with history length** (×17.1
     messages → ×17.1 ms from short to very_long). The Day-13/14/15
     strategies have a real latency budget to spend on embedding
     lookups + summarization calls; they don't have to be free,
     they have to stay under the 50 ms retrieval allowance from
     the Phase-2 latency budget.
  2. **Very_long is where Phase 3 gets decided.** Recency already
     saturates the 8K budget there (98.6%); smarter strategies
     can't win by packing *more*, they have to win by packing the
     *right* messages. That's the entire reason semantic + hybrid
     exist.
  3. **The 8K budget choice (locked Day 7) holds up.** Short pairs
     fit comfortably (no waste); very_long pairs are at the cliff
     edge (every slot competes, so retrieval ranking matters).
     Day 13 confirms this on the naive vs recency cohort; Day 14
     reproduces it for semantic; Day 15 stresses it for the hybrid
     champion.

---

## Day 13 — 2026-05-16 — Naive-dump vs recency on 200 pairs

- **Strategies run:** `naive_dump`, `recency`
- **Token budgets:** naive=50K (model-context-cap framing), recency=8K
  (production setting, locked Day 7)
- **Quality scoring:** no — LLM-as-judge lands Day 15. Retrieval-only
  comparison today (tokens, latency, budget saturation).
- **Results artifact:** `results/phase3_context_engine_results.json`
  (400 rows: 200 pairs × 2 strategies, full brief text included for
  Day-15 re-judge)
- **Key numbers:**
  - Naive avg brief: **1449.6 tokens** (50K budget, 2.9% sat) ;
    p50 latency 0.237 ms, p95 0.936 ms.
  - Recency avg brief: **1421.7 tokens** (8K budget, 17.8% sat) ;
    p50 latency 0.233 ms, p95 0.929 ms.
  - Per-pair delta (naive − recency) is **0 tokens on
    short/medium/long** (95% of pairs) and **+558 tokens on
    very_long** (10 pairs). 9/10 very_long pairs lost content under
    recency's 8K budget; max single-pair drop was 1,035 tokens.
  - Recency very_long saturation: **98.6%** — cliff edge. Naive
    very_long saturation: 16.9% — comfortable headroom.
  - Total input tokens for 200 pairs: naive **289,926**, recency
    **284,344** → token ratio 1.0196 → naive's input-cost premium
    is **2% above recency at any per-token input price** (cost is
    linear in tokens). No specific per-token dollar rate is asserted
    here; the dollar-per-100q figure depends on the published input
    rate of the model used in production and is pinned alongside the
    Day-15 LLM-as-judge run. The 6× framing the SKILL narrative
    hints at would require a strategy that shrinks below recency's
    1.4K-token average — likely Day-15 hybrid, not Day-13 naive.
- **Verdict so far:** Recency holds on tokens AND content-fit across
  95% of the cohort (no content dropped); the only battleground is
  the very_long slice where recency is one big history away from
  truncation. Day 14's semantic + summarized strategies should fight
  for that 558-token-per-pair headroom — replacing dropped recency
  segments with semantically relevant or summarized older content.
