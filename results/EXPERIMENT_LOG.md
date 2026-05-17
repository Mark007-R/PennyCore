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
    0.24 ms); 2.5× at p95 (3.54 vs 1.40 ms). FNV hash + cosine per
    segment is cheap (~3 µs each) but ~280 segments × 4 µs = ~1 ms
    on a very_long history.
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
