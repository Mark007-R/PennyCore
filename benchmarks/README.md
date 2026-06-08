# benchmarks

The comparison-study harnesses. The headline numbers in
[../README.md](../README.md) come from running these.

This is not a microbenchmark suite (no `pytest-benchmark`-style
"how fast is this function" runs). It is **whole-strategy comparison**
— five retrieval strategies on the same 200 customer-history / query
pairs, four policy engines on the same 200 event / tenant / expected-
action tuples, all measured on the same axes so the comparison tables
are directly readable.

## Layout

### Phase-3 datasets (committed, deterministic)
- `data/customer_histories.jsonl` — 200 customer histories ranging
  from 10 to 500 messages. Mix of hand-written banking scenarios and
  MultiWOZ-derived public dialogue samples.
- `data/queries.jsonl` — 200 queries with ground-truth answers
  (50 hand-written, 150 generated-then-validated).
- `data/orchestrator/` — 200 event / tenant / expected-action tuples
  spanning document upload, message received, status change, anomaly
  detection. Three tenants with different policy shapes (Bank A
  auto-text-allowed, Bank B approval-required, Bank C in-between).
- `data/build_dataset.py` — the script that builds the datasets from
  the source files. Re-runnable; output is deterministic.
- `data/manifest.json` — sha256 of every dataset file. The harnesses
  refuse to run on a dataset whose hash doesn't match.

### Phase-3 harnesses
- `context_engine_bench.py` — runs all five retrieval strategies on
  all 200 pairs. For each (strategy, pair) records:
  brief tokens, end-to-end latency, cost (when not in mock mode),
  and LLM-as-judge quality vs ground-truth.
- `orchestrator_bench.py` — runs all four policy engines on all 200
  tuples. For each records: correctness vs expected-action set,
  latency, cost, auditability score (rubric), maintainability score
  (rubric).
- `judge.py` — the LLM-as-judge implementation. Same model for every
  strategy under test = clean comparisons.
- `dataset_loader.py` + `orchestrator_dataset_loader.py` — read +
  manifest-check the JSONL datasets.
- `strategies/` — the same five retrieval strategies imported by
  `context_engine.retrieval`, exposed in a benchmark-friendly shape.
- `registry.py` — registers each strategy with a string key so the
  harness CLI can `--strategy hybrid` cleanly.

### Phase-5 harnesses (naive baseline head-to-head)
- `phase5_naive_vs_champion.py` — re-runs the context-engine champion
  (`hybrid`) against the naive "dump everything" baseline on the same
  200 pairs. Headline: ~6× cost reduction at parity quality.
- `phase5_naive_vs_champion_chart.py` — renders the chart in
  `results/phase5_*.png`.
- `phase5_orch_naive_vs_champion.py` — same shape for the
  orchestrator champion vs naive-LLM-policy baseline.
- `phase5_orch_naive_vs_champion_chart.py` — orchestrator chart.

### Other
- `load_test.py` — Locust file (Phase 4 Day 22). 100 concurrent
  customers across 5 tenants.
- `local_load_runner.py` — Locust-less in-process load runner the
  Phase-4 race-condition tests use.
- `phase2_latency_backfill.py` — Phase-2 end-to-end latency numbers
  re-measured into the metrics journal.
- `semantic_cache_bench.py` — Phase-5 Day-25 semantic-cache cost-
  reduction measurement.

## Running

```bash
# Phase 3 — context engine retrieval comparison (mock mode)
python -m benchmarks.context_engine_bench --strategies all

# Phase 3 — orchestrator policy comparison (mock mode)
python -m benchmarks.orchestrator_bench --strategies all

# Phase 5 — champion vs naive baseline (writes to results/)
python -m benchmarks.phase5_naive_vs_champion
python -m benchmarks.phase5_orch_naive_vs_champion

# Render charts
python -m benchmarks.phase5_naive_vs_champion_chart
python -m benchmarks.phase5_orch_naive_vs_champion_chart
```

All harnesses append to `results/metrics.json` (append-only journal
across the 35 days) and write a phase-specific results JSON
(`results/phase3_*.json`, `results/phase5_*.json`).

In mock mode (default; no API keys needed) the cost column is $0 and
the quality column comes from a deterministic stub judge — useful
for shape-checking the harness in CI, not for headline numbers. Set
`LLM_PROVIDER=anthropic` and a real key to get production numbers.

## Manifest discipline

`data/manifest.json` carries a sha256 of every dataset file. The
loaders verify the hash on every run. Means: if a future commit
silently rebuilds the dataset with a different shape, every
historical comparison number stays interpretable — the harness
refuses to load against a dataset that doesn't match the manifest
of the run that recorded the numbers.
