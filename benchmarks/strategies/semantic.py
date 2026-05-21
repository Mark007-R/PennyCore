"""Semantic retrieval — Day 14 Phase-3 strategy registration.

Thin wrapper that adapts
:func:`context_engine.retrieval.semantic.semantic_segments` to the
harness's ``(pair, token_budget) -> list[BriefSegment]`` signature.

The strategy itself lives in ``context_engine.retrieval.semantic``
(production module) — this file is the registration glue only,
mirroring how Day-13's ``benchmarks/strategies/recency.py`` wraps the
production ``context_engine.retrieval.recency``. Keeping
strategy-implementation code in the production package means the
Day-7 / Day-24 production retrieval path can re-import the exact
same function the benchmark scores — no benchmark-only copies that
drift.

Budget: no per-strategy override. Semantic respects the harness's
default 8K budget so its head-to-head row against recency is at the
same token ceiling — same downstream cost envelope, just a
different selection signal.
"""
from __future__ import annotations

from benchmarks.dataset_loader import BenchmarkPair
from benchmarks.registry import register_strategy
from context_engine.retrieval.semantic import semantic_segments
from contracts import BriefSegment


def _semantic_strategy(
    pair: BenchmarkPair, token_budget: int
) -> list[BriefSegment]:
    # token_budget is enforced downstream by pack_segments; semantic
    # returns every segment and lets the assembler trim by priority.
    return semantic_segments(
        query=pair.query,
        messages=pair.history.messages,
        actions=pair.history.actions,
    )


register_strategy("semantic", _semantic_strategy)
