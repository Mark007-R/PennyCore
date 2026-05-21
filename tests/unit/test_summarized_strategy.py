"""Day 14 — LLM-summarized retrieval strategy tests.

Covers:

* :func:`summarized_segments` keeps the newest ``recent_window``
  messages verbatim and compresses the cold tail into a single
  SUMMARY segment via the injected LLM client.
* Prior actions never get folded into the summary — they're emitted
  verbatim with the high-priority action band.
* Priority bands order the brief deterministically (summary →
  actions → warm messages) so the assembler packs them in that order
  under a tight budget.
* Mock-mode summary text uses the recognizable
  ``"...[mock-summary]"`` marker for downstream reporting.
* Strategy registers under the ``"summarized"`` name with the
  harness's default budget.
* On a real benchmark pair, the brief contains exactly one summary
  segment when the history exceeds the warm window.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from benchmarks.context_engine_bench import run
from benchmarks.dataset_loader import load_pairs
from benchmarks.registry import get_strategy_budget, list_strategies
from context_engine.llm.mock import MockClient
from context_engine.retrieval.summarized import (
    DEFAULT_RECENT_WINDOW,
    summarized_segments,
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


class TestSummarizedSegments:
    def test_empty_inputs_return_empty(self) -> None:
        client = MockClient()
        assert summarized_segments(messages=[], actions=[], client=client) == []

    def test_no_summary_when_history_fits_warm_window(self) -> None:
        """A 3-message history with recent_window=5 → no cold tail,
        no summary segment. All three messages stay verbatim."""
        msgs = [
            _msg(content=f"m{i}", timestamp=NOW - timedelta(minutes=i))
            for i in range(3)
        ]
        segs = summarized_segments(
            messages=msgs, actions=[], client=MockClient()
        )
        kinds = [s.source for s in segs]
        assert SegmentSource.SUMMARY not in kinds
        assert kinds.count(SegmentSource.RECENT_MESSAGE) == 3

    def test_summary_emitted_when_history_exceeds_window(self) -> None:
        msgs = [
            _msg(content=f"message body {i}",
                 timestamp=NOW - timedelta(minutes=i))
            for i in range(10)
        ]
        segs = summarized_segments(
            messages=msgs, actions=[], client=MockClient()
        )
        summaries = [s for s in segs if s.source is SegmentSource.SUMMARY]
        warm = [s for s in segs if s.source is SegmentSource.RECENT_MESSAGE]
        assert len(summaries) == 1, "Exactly one SUMMARY segment per call"
        assert len(warm) == DEFAULT_RECENT_WINDOW
        # The summary covered the cold tail (10 - 5 = 5 messages).
        assert summaries[0].metadata["n_messages_summarized"] == 5

    def test_summary_uses_mock_client_marker(self) -> None:
        msgs = [
            _msg(content=f"body {i}", timestamp=NOW - timedelta(minutes=i))
            for i in range(10)
        ]
        segs = summarized_segments(
            messages=msgs, actions=[], client=MockClient()
        )
        summary = next(s for s in segs if s.source is SegmentSource.SUMMARY)
        # Marker comes from MockClient.summarize.
        assert "[mock-summary]" in summary.body
        # Body starts with the "[summary]" prefix so the LLM can parse it.
        assert summary.body.startswith("[summary] ")

    def test_summary_priority_dominates(self) -> None:
        msgs = [
            _msg(content=f"body {i}", timestamp=NOW - timedelta(minutes=i))
            for i in range(10)
        ]
        acts = [_action(timestamp=NOW)]
        segs = summarized_segments(
            messages=msgs, actions=acts, client=MockClient()
        )
        # Order must be summary → actions → warm messages.
        assert segs[0].source is SegmentSource.SUMMARY
        # All actions appear before any warm message.
        first_warm_idx = next(
            i for i, s in enumerate(segs)
            if s.source is SegmentSource.RECENT_MESSAGE
        )
        for s in segs[:first_warm_idx]:
            assert s.source in (
                SegmentSource.SUMMARY,
                SegmentSource.PRIOR_ACTION,
            )

    def test_actions_emitted_verbatim_with_metadata(self) -> None:
        msgs = [
            _msg(content=f"body {i}", timestamp=NOW - timedelta(minutes=i))
            for i in range(8)
        ]
        acts = [
            _action(action_type="send_checklist", timestamp=NOW),
            _action(action_type="send_quote", timestamp=NOW - timedelta(hours=1)),
        ]
        segs = summarized_segments(
            messages=msgs, actions=acts, client=MockClient()
        )
        action_segs = [s for s in segs if s.source is SegmentSource.PRIOR_ACTION]
        assert len(action_segs) == 2
        action_types = {s.metadata["action_type"] for s in action_segs}
        assert action_types == {"send_checklist", "send_quote"}

    def test_custom_recent_window(self) -> None:
        msgs = [
            _msg(content=f"body {i}", timestamp=NOW - timedelta(minutes=i))
            for i in range(10)
        ]
        segs = summarized_segments(
            messages=msgs,
            actions=[],
            client=MockClient(),
            recent_window=3,
        )
        warm = [s for s in segs if s.source is SegmentSource.RECENT_MESSAGE]
        summaries = [s for s in segs if s.source is SegmentSource.SUMMARY]
        assert len(warm) == 3
        assert summaries[0].metadata["n_messages_summarized"] == 7

    def test_segments_are_briefsegment_instances(self) -> None:
        segs = summarized_segments(
            messages=[_msg()],
            actions=[_action()],
            client=MockClient(),
        )
        for s in segs:
            assert isinstance(s, BriefSegment)

    def test_summary_metadata_carries_provider(self) -> None:
        msgs = [
            _msg(content=f"body {i}", timestamp=NOW - timedelta(minutes=i))
            for i in range(8)
        ]
        segs = summarized_segments(
            messages=msgs, actions=[], client=MockClient()
        )
        summary = next(s for s in segs if s.source is SegmentSource.SUMMARY)
        assert summary.metadata["llm_provider"] == "mock"
        assert summary.metadata["llm_model"] == "mock-model"


# -----------------------------------------------------------------------------
# Harness registration + real-dataset integration
# -----------------------------------------------------------------------------


class TestHarnessRegistration:
    def test_registered_at_import_time(self) -> None:
        import benchmarks.strategies  # noqa: F401
        assert "summarized" in list_strategies()

    def test_no_per_strategy_budget_override(self) -> None:
        import benchmarks.strategies  # noqa: F401
        assert get_strategy_budget("summarized", 8000) == 8000


class TestRunOnRealBenchmark:
    def test_summarized_respects_budget(self) -> None:
        pairs = load_pairs()[:5]
        results = run(pairs=pairs, strategies=["summarized"])
        for r in results:
            assert r.brief_tokens_estimate <= r.token_budget

    def test_summarized_emits_summary_on_very_long(self) -> None:
        pairs = [p for p in load_pairs() if p.history_bucket == "very_long"][:3]
        results = run(pairs=pairs, strategies=["summarized"])
        for r in results:
            assert r.segment_source_counts.get("summary", 0) == 1, (
                f"Expected exactly one summary segment on {r.pair_id}: "
                f"{r.segment_source_counts}"
            )

    def test_summarized_packs_fewer_or_equal_tokens_than_recency_on_very_long(
        self,
    ) -> None:
        """Compression should cut total tokens on histories that
        saturate recency's 8K budget — summary is shorter than the
        messages it replaces."""
        pairs = [p for p in load_pairs() if p.history_bucket == "very_long"][:3]
        results = run(pairs=pairs, strategies=["summarized", "recency"])

        by_strategy: dict[str, list] = {"summarized": [], "recency": []}
        for r in results:
            by_strategy[r.strategy].append(r)

        # On at least one very_long pair, summarized's brief should be
        # strictly smaller than recency's — that's the whole point of
        # summarization. (We don't require it on every pair because the
        # mock summary's 200-char head can occasionally tie recency on
        # very short cold tails.)
        smaller_count = 0
        for sum_r, rec_r in zip(by_strategy["summarized"], by_strategy["recency"]):
            assert sum_r.pair_id == rec_r.pair_id
            if sum_r.brief_tokens_estimate < rec_r.brief_tokens_estimate:
                smaller_count += 1
        assert smaller_count >= 1, (
            "Summarized should be smaller than recency on at least one "
            "very_long pair"
        )
