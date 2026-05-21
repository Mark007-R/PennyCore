"""Recency-only retrieval — promoted to a first-class Phase-3 strategy.

Day 7 shipped :mod:`context_engine.retrieval.recency` as the production
baseline. Day 12 registered it directly inside
:mod:`benchmarks.context_engine_bench` so the harness had at least one
strategy to run on the skeleton. Day 13 promotes it to a peer-of-naive
strategy module so the comparison table treats recency as one of the
five strategies the rest of Phase 3 evaluates, not just a harness-level
default.

The strategy function is a thin wrapper over the production module —
no behavior changes. Re-registering with the same name overwrites the
Day-12 inline registration (last-write-wins, as documented in
:func:`register_strategy`). The recency budget stays at the runner's
CLI default (8K) — no per-strategy override — because that's the
budget production code uses.
"""
from __future__ import annotations

from benchmarks.dataset_loader import BenchmarkPair
from benchmarks.registry import register_strategy
from context_engine.retrieval.recency import recency_segments
from contracts import BriefSegment


def _recency_strategy(
    pair: BenchmarkPair, token_budget: int
) -> list[BriefSegment]:
    # token_budget is honored downstream by pack_segments; recency
    # itself returns every segment and lets the assembler trim.
    return recency_segments(
        messages=pair.history.messages,
        actions=pair.history.actions,
    )


register_strategy("recency", _recency_strategy)
