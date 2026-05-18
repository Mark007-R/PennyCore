"""Hybrid retrieval — Day 15 Phase-3 strategy registration.

Wraps :func:`context_engine.retrieval.hybrid.hybrid_segments` for the
benchmark harness. Uses the production defaults (24h warm cutoff, 72h
cold cutoff) and lets :func:`context_engine.llm.get_client` resolve
the active LLM provider — under the current benchmark configuration
(no API keys set) that returns the
:class:`context_engine.llm.MockClient` for the cold-tail summary
step. The mid-range semantic scoring is dependency-free (hashed BoW,
shared with Day-14 semantic) so the mid range stays provider-
independent.

Budget: no per-strategy override — same 8K default as recency /
semantic / summarized. The whole point of hybrid is to outperform
those at the same ceiling; a different budget would muddy the
comparison.

Day-15 LLM-as-judge re-runs save the assembled brief into the
results JSON, so a future run with a real LLM provider can re-score
hybrid alongside summarized without re-retrieving (the brief text is
the artifact).
"""
from __future__ import annotations

from benchmarks.dataset_loader import BenchmarkPair
from benchmarks.registry import register_strategy
from context_engine.retrieval.hybrid import hybrid_segments
from contracts import BriefSegment


def _hybrid_strategy(
    pair: BenchmarkPair, token_budget: int
) -> list[BriefSegment]:
    return hybrid_segments(
        query=pair.query,
        messages=pair.history.messages,
        actions=pair.history.actions,
    )


register_strategy("hybrid", _hybrid_strategy)
