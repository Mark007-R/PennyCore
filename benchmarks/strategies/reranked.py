"""Reranked retrieval — Day 24 Phase-5 strategy registration.

Thin wrapper that adapts
:func:`context_engine.retrieval.rerank.rerank_segments` to the
harness's ``(pair, token_budget) -> list[BriefSegment]`` signature.

The strategy itself lives in ``context_engine.retrieval.rerank``
(production module) — this file is the registration glue only,
mirroring how Day-14's ``benchmarks/strategies/semantic.py`` wraps
the production ``context_engine.retrieval.semantic``. Keeping the
implementation in the production package lets future production
retrieval paths import the same function the benchmark scores —
no benchmark-only copies that drift.

Why a fixed ``now`` for the benchmark
-------------------------------------
The recency feature in the re-ranker needs a reference timestamp.
Wall-clock ``datetime.now()`` would make benchmark results drift
day to day (Day-24 numbers wouldn't match a Day-28 re-run on the
same dataset). We pass a fixed ``now`` keyed off the newest message
across the benchmark so every Phase-5 re-run produces the same
recency scores. ``2026-05-27T00:00:00`` is a few days after the
benchmark's typical "now" anchor; well within the half-life window
where recency contributes meaningfully.

Budget
------
No per-strategy override. Re-rank respects the harness's default
8K budget so its head-to-head row against semantic is at the same
token ceiling — same downstream cost envelope, just a different
selection signal.
"""
from __future__ import annotations

from datetime import datetime

from benchmarks.dataset_loader import BenchmarkPair
from benchmarks.registry import register_strategy
from context_engine.retrieval.rerank import rerank_segments
from contracts import BriefSegment

# Fixed reference "now" for the recency feature so benchmark numbers
# are byte-stable across days. See module docstring.
_BENCHMARK_NOW = datetime(2026, 5, 27)


def _reranked_strategy(
    pair: BenchmarkPair, token_budget: int
) -> list[BriefSegment]:
    # token_budget is enforced downstream by pack_segments; rerank
    # returns every segment (re-ranked head + semantic-order tail).
    return rerank_segments(
        query=pair.query,
        messages=pair.history.messages,
        actions=pair.history.actions,
        now=_BENCHMARK_NOW,
    )


register_strategy("reranked", _reranked_strategy)
