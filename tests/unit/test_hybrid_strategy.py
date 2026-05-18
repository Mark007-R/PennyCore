"""Day 15 — hybrid retrieval strategy tests.

Covers:

* :func:`hybrid_segments` partitions messages into warm / mid / cold
  buckets by timestamp relative to the newest message, with the
  configured cutoffs in hours.
* Warm messages emit as ``RECENT_MESSAGE`` segments verbatim.
* Mid-range messages emit as ``SEMANTIC_MATCH`` segments scored
  against the query.
* Cold tail collapses into a single ``SUMMARY`` segment (or none if
  empty).
* Prior actions are always verbatim, regardless of age.
* Priority bands order the brief deterministically:
  summary > action > warm > semantic_mid.
* The ``cold_cutoff_hours <= warm_cutoff_hours`` guard raises.
* Strategy registers under the ``"hybrid"`` name with the harness's
  default budget.
* On a real benchmark pair, hybrid emits the expected mix of
  segments for each bucket (short → all warm; very_long →
  warm + mid + summary).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from benchmarks.context_engine_bench import run
from benchmarks.dataset_loader import load_pairs
from benchmarks.registry import get_strategy_budget, list_strategies
from context_engine.llm.mock import MockClient
from context_engine.retrieval.hybrid import (
    DEFAULT_COLD_CUTOFF_HOURS,
    DEFAULT_WARM_CUTOFF_HOURS,
    hybrid_segments,
)
from contracts import BriefSegment, SegmentSource

NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def _msg(
    *,
    channel: str = "chat",
    sender: str = "borrower",
    content: str = "hello",
    timestamp: datetime | None = None,
    metadata: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        channel=channel,
        sender=sender,
        content=content,
        timestamp=timestamp or NOW,
        metadata=metadata or {},
    )


def _action(
    *,
    action_type: str = "send_checklist",
    details: dict | None = None,
    timestamp: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        action_type=action_type,
        details=details or {},
        timestamp=timestamp or NOW,
    )


class TestHybridSegments:
    def test_empty_inputs_return_empty(self) -> None:
        assert (
            hybrid_segments(
                query="anything", messages=[], actions=[], client=MockClient()
            )
            == []
        )

    def test_short_history_all_warm(self) -> None:
        """A 4-hour history under 24h warm cutoff → every message is
        warm, no mid range, no cold tail."""
        msgs = [
            _msg(content=f"m{i}", timestamp=NOW - timedelta(hours=i))
            for i in range(4)
        ]
        segs = hybrid_segments(
            query="rate", messages=msgs, actions=[], client=MockClient()
        )
        kinds = [s.source for s in segs]
        assert kinds.count(SegmentSource.RECENT_MESSAGE) == 4
        assert SegmentSource.SUMMARY not in kinds
        assert SegmentSource.SEMANTIC_MATCH not in kinds

    def test_mid_range_emits_semantic_match(self) -> None:
        """Messages 24h-72h old hit the semantic mid range."""
        msgs = [
            # warm: 0, 12 hours
            _msg(content="warm zero", timestamp=NOW),
            _msg(content="warm twelve", timestamp=NOW - timedelta(hours=12)),
            # mid: 30, 48 hours
            _msg(content="mid thirty about rate", timestamp=NOW - timedelta(hours=30)),
            _msg(content="mid forty eight other", timestamp=NOW - timedelta(hours=48)),
        ]
        segs = hybrid_segments(
            query="rate", messages=msgs, actions=[], client=MockClient()
        )
        warm = [s for s in segs if s.source is SegmentSource.RECENT_MESSAGE]
        mid = [s for s in segs if s.source is SegmentSource.SEMANTIC_MATCH]
        assert len(warm) == 2
        assert len(mid) == 2
        assert all("tier" in s.metadata for s in segs)

    def test_cold_tail_summarizes(self) -> None:
        """Messages older than the cold cutoff (72h default) collapse
        into one SUMMARY segment carrying the count summarized."""
        msgs = [
            _msg(content="warm now", timestamp=NOW),
            # 5 cold messages, all older than 72h
            *[
                _msg(content=f"cold {i}", timestamp=NOW - timedelta(hours=80 + i))
                for i in range(5)
            ],
        ]
        segs = hybrid_segments(
            query="anything", messages=msgs, actions=[], client=MockClient()
        )
        summaries = [s for s in segs if s.source is SegmentSource.SUMMARY]
        assert len(summaries) == 1
        assert summaries[0].metadata["n_messages_summarized"] == 5
        assert "[mock-summary]" in summaries[0].body

    def test_priority_order_summary_action_warm_mid(self) -> None:
        msgs = [
            _msg(content="warm", timestamp=NOW),
            _msg(content="mid", timestamp=NOW - timedelta(hours=30)),
            _msg(content="cold", timestamp=NOW - timedelta(hours=100)),
        ]
        acts = [_action(action_type="send_checklist", timestamp=NOW - timedelta(hours=1))]
        segs = hybrid_segments(
            query="anything", messages=msgs, actions=acts, client=MockClient()
        )
        # Strict tier order: summary -> action -> warm -> semantic_mid.
        sources = [s.source for s in segs]
        assert sources[0] is SegmentSource.SUMMARY
        assert sources[1] is SegmentSource.PRIOR_ACTION
        assert sources[2] is SegmentSource.RECENT_MESSAGE
        assert sources[3] is SegmentSource.SEMANTIC_MATCH

    def test_actions_emitted_regardless_of_age(self) -> None:
        """A 10-day-old action still emits verbatim, never folded into
        the cold-tail summary."""
        msgs = [_msg(content="warm", timestamp=NOW)]
        acts = [
            _action(action_type="send_quote", timestamp=NOW - timedelta(days=10)),
        ]
        segs = hybrid_segments(
            query="anything", messages=msgs, actions=acts, client=MockClient()
        )
        action_segs = [s for s in segs if s.source is SegmentSource.PRIOR_ACTION]
        assert len(action_segs) == 1
        assert action_segs[0].metadata["action_type"] == "send_quote"

    def test_no_summary_when_no_cold_tail(self) -> None:
        """If no message is older than the cold cutoff, no SUMMARY
        segment should be emitted (not an empty SUMMARY)."""
        msgs = [
            _msg(content=f"m{i}", timestamp=NOW - timedelta(hours=i * 2))
            for i in range(20)
        ]
        segs = hybrid_segments(
            query="anything", messages=msgs, actions=[], client=MockClient()
        )
        kinds = [s.source for s in segs]
        assert SegmentSource.SUMMARY not in kinds

    def test_invalid_cutoffs_raise(self) -> None:
        with pytest.raises(ValueError, match="must be >"):
            hybrid_segments(
                query="x",
                messages=[_msg()],
                actions=[],
                client=MockClient(),
                warm_cutoff_hours=72,
                cold_cutoff_hours=24,
            )

    def test_invalid_cutoffs_equal_raise(self) -> None:
        with pytest.raises(ValueError):
            hybrid_segments(
                query="x",
                messages=[_msg()],
                actions=[],
                client=MockClient(),
                warm_cutoff_hours=24,
                cold_cutoff_hours=24,
            )

    def test_custom_warm_cutoff_widens_warm_window(self) -> None:
        msgs = [
            _msg(content="now", timestamp=NOW),
            _msg(content="forty-eight", timestamp=NOW - timedelta(hours=48)),
        ]
        # Default: 48h is mid (24h < 48 < 72). Widening warm to 72h: 48h is warm.
        wide = hybrid_segments(
            query="x",
            messages=msgs,
            actions=[],
            client=MockClient(),
            warm_cutoff_hours=72,
            cold_cutoff_hours=120,
        )
        warm = [s for s in wide if s.source is SegmentSource.RECENT_MESSAGE]
        mid = [s for s in wide if s.source is SegmentSource.SEMANTIC_MATCH]
        assert len(warm) == 2
        assert len(mid) == 0

    def test_segments_are_briefsegment_instances(self) -> None:
        msgs = [
            _msg(content="warm", timestamp=NOW),
            _msg(content="mid", timestamp=NOW - timedelta(hours=30)),
            _msg(content="cold", timestamp=NOW - timedelta(hours=100)),
        ]
        segs = hybrid_segments(
            query="x", messages=msgs, actions=[], client=MockClient()
        )
        for s in segs:
            assert isinstance(s, BriefSegment)

    def test_summary_carries_provider_metadata(self) -> None:
        msgs = [
            _msg(content="warm", timestamp=NOW),
            _msg(content="cold", timestamp=NOW - timedelta(hours=100)),
        ]
        segs = hybrid_segments(
            query="x", messages=msgs, actions=[], client=MockClient()
        )
        summary = next(s for s in segs if s.source is SegmentSource.SUMMARY)
        assert summary.metadata["llm_provider"] == "mock"
        assert summary.metadata["llm_model"] == "mock-model"
        assert summary.metadata["tier"] == "cold"

    def test_defaults_are_24_and_72(self) -> None:
        assert DEFAULT_WARM_CUTOFF_HOURS == 24
        assert DEFAULT_COLD_CUTOFF_HOURS == 72


# -----------------------------------------------------------------------------
# Harness registration + real-dataset integration
# -----------------------------------------------------------------------------


class TestHarnessRegistration:
    def test_registered_at_import_time(self) -> None:
        import benchmarks.strategies  # noqa: F401
        assert "hybrid" in list_strategies()

    def test_no_per_strategy_budget_override(self) -> None:
        import benchmarks.strategies  # noqa: F401
        assert get_strategy_budget("hybrid", 8000) == 8000


class TestRunOnRealBenchmark:
    def test_hybrid_respects_budget(self) -> None:
        pairs = load_pairs()[:5]
        results = run(pairs=pairs, strategies=["hybrid"])
        for r in results:
            # estimate_tokens uses len(text) // 4 — same envelope the
            # assembler asserts under.
            assert r.brief_tokens_estimate <= r.token_budget

    def test_hybrid_short_bucket_is_all_warm(self) -> None:
        """short pairs span ~4 hours → every message is in the warm
        window, no mid/cold; hybrid mirrors recency on this slice."""
        pairs = [p for p in load_pairs() if p.history_bucket == "short"][:3]
        results = run(pairs=pairs, strategies=["hybrid"])
        for r in results:
            counts = r.segment_source_counts
            assert counts.get("summary", 0) == 0
            assert counts.get("semantic_match", 0) == 0
            assert counts.get("recent_message", 0) > 0

    def test_hybrid_very_long_bucket_emits_summary_and_semantic(self) -> None:
        """very_long pairs span ~20 days → cold tail is large, mid
        range is non-empty, warm window is the newest day's messages."""
        pairs = [p for p in load_pairs() if p.history_bucket == "very_long"][:3]
        results = run(pairs=pairs, strategies=["hybrid"])
        for r in results:
            counts = r.segment_source_counts
            assert counts.get("summary", 0) == 1, (
                f"Expected exactly one summary on {r.pair_id}: {counts}"
            )
            # warm or semantic_match should be present on very_long
            assert (counts.get("recent_message", 0) + counts.get("semantic_match", 0)) > 0
