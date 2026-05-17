"""LLM-summarized retrieval — Day 14 Phase-3 strategy registration.

Wraps :func:`context_engine.retrieval.summarized.summarized_segments`
for the benchmark harness. Uses the default warm window (5 messages)
and lets the production
:func:`context_engine.llm.get_client` resolve the active LLM provider
— under the current benchmark configuration (no API keys set) that
returns the :class:`context_engine.llm.MockClient`, which produces a
deterministic 200-char ``"...[mock-summary]"`` summary.

Budget: no per-strategy override — same 8K default as recency /
semantic. Comparison is on what got into the brief at the same
ceiling, not on different ceilings.

Day-15 LLM-as-judge re-runs save the assembled brief into the results
JSON, so a future run can re-score this strategy against a real LLM
without re-retrieving (the brief text is the artifact).
"""
from __future__ import annotations

from benchmarks.dataset_loader import BenchmarkPair
from benchmarks.registry import register_strategy
from context_engine.retrieval.summarized import summarized_segments
from contracts import BriefSegment


def _summarized_strategy(
    pair: BenchmarkPair, token_budget: int
) -> list[BriefSegment]:
    # Cold tail is summarized; warm window + actions stay verbatim.
    # ``client=None`` defers to ``get_client()`` so the registered
    # strategy uses whichever provider is active at harness-run time.
    return summarized_segments(
        messages=pair.history.messages,
        actions=pair.history.actions,
    )


register_strategy("summarized", _summarized_strategy)
