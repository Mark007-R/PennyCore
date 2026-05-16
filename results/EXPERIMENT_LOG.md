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
