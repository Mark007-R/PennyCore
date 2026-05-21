"""Day 13 — naive-dump strategy + per-strategy budget override tests.

Covers:

* :func:`naive_segments` returns every message and every action as
  :class:`BriefSegment` instances, sorted oldest-first by priority.
* Naive does NOT apply the ``+1.0`` action-priority boost (a
  contrast with the recency baseline).
* Naive shares the message/action formatters with recency so the
  brief contents are byte-equal modulo ordering.
* Naive registers itself with the harness at import time with the
  50K-token budget.
* The harness's ``register_strategy(..., token_budget=...)``
  override is honored end-to-end: naive runs at 50K while recency
  runs at 8K in the same invocation.
* On a real benchmark pair, naive packs strictly more tokens than
  recency on histories long enough to saturate the 8K budget.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from benchmarks import context_engine_bench
from benchmarks.context_engine_bench import (
    _STRATEGY_BUDGETS,
    get_strategy_budget,
    list_strategies,
    register_strategy,
    run,
)
from benchmarks.dataset_loader import BenchmarkPair, load_pairs
from benchmarks.strategies.naive import (
    NAIVE_TOKEN_BUDGET,
    naive_segments,
)
from context_engine.retrieval.recency import (
    format_action_segment,
    format_message_segment,
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


class TestNaiveSegments:
    def test_empty_inputs_return_empty(self) -> None:
        assert naive_segments(messages=[], actions=[]) == []

    def test_messages_sorted_oldest_first(self) -> None:
        old = _msg(content="old", timestamp=NOW - timedelta(hours=2))
        mid = _msg(content="mid", timestamp=NOW - timedelta(hours=1))
        new = _msg(content="new", timestamp=NOW)
        segs = naive_segments(messages=[new, old, mid], actions=[])
        bodies = [s.body for s in segs]
        assert bodies[0].endswith("old"), "Naive must read chronologically — oldest first"
        assert bodies[1].endswith("mid")
        assert bodies[2].endswith("new")

    def test_no_action_priority_boost(self) -> None:
        """Recency boosts actions by +1.0 at the same timestamp. Naive does NOT.

        At equal timestamps the action and message will tie on priority.
        Stable sort preserves insertion order; the test asserts the
        priorities are identical (which proves no boost was applied)
        rather than asserting a particular ordering of the tied items.
        """
        msg = _msg(content="msg-text", timestamp=NOW)
        act = _action(action_type="send_checklist", timestamp=NOW)
        segs = naive_segments(messages=[msg], actions=[act])
        assert len(segs) == 2
        assert segs[0].priority == segs[1].priority, (
            "Naive must not apply the +1.0 action boost — priorities "
            "must tie at equal timestamps"
        )

    def test_segment_count_equals_messages_plus_actions(self) -> None:
        msgs = [_msg(timestamp=NOW - timedelta(minutes=i)) for i in range(5)]
        acts = [_action(timestamp=NOW - timedelta(minutes=i)) for i in range(3)]
        segs = naive_segments(messages=msgs, actions=acts)
        assert len(segs) == 8

    def test_uses_same_formatters_as_recency(self) -> None:
        """Brief contents must be byte-equal to recency formatters so the
        LLM-as-judge step compares strategies on retrieval, not formatting."""
        msg = _msg(channel="email", sender="agent", content="hi",
                   metadata={"subject": "Documents"})
        act = _action(
            action_type="request_document",
            details={"document_type": "w2"},
        )
        segs = naive_segments(messages=[msg], actions=[act])
        bodies = [s.body for s in segs]
        assert format_message_segment(msg) in bodies
        assert format_action_segment(act) in bodies

    def test_segment_metadata_preserved(self) -> None:
        msg = _msg(channel="email", sender="agent")
        act = _action(action_type="send_quote")
        segs = naive_segments(messages=[msg], actions=[act])
        msg_seg = next(s for s in segs if s.source is SegmentSource.RECENT_MESSAGE)
        act_seg = next(s for s in segs if s.source is SegmentSource.PRIOR_ACTION)
        assert msg_seg.metadata["channel"] == "email"
        assert msg_seg.metadata["sender"] == "agent"
        assert act_seg.metadata["action_type"] == "send_quote"

    def test_token_estimate_matches_evaluator_estimator(self) -> None:
        msg = _msg(content="x" * 100)
        segs = naive_segments(messages=[msg], actions=[])
        assert segs[0].token_estimate == len(segs[0].body) // 4
        assert segs[0].token_estimate > 0

    def test_segments_are_briefsegment_instances(self) -> None:
        segs = naive_segments(messages=[_msg()], actions=[_action()])
        for s in segs:
            assert isinstance(s, BriefSegment)


# -----------------------------------------------------------------------------
# Harness — per-strategy budget override
# -----------------------------------------------------------------------------


class TestHarnessBudgetOverride:
    def test_naive_dump_registered_at_import_time(self) -> None:
        assert "naive_dump" in list_strategies()

    def test_naive_budget_is_50k(self) -> None:
        # The 50K cap is the SKILL narrative's "50K vs 8K" framing.
        assert NAIVE_TOKEN_BUDGET == 50_000
        assert get_strategy_budget("naive_dump", 8000) == 50_000

    def test_recency_inherits_runner_default(self) -> None:
        # No per-strategy override registered — runner's default applies.
        assert get_strategy_budget("recency", 8000) == 8000
        assert get_strategy_budget("recency", 16_000) == 16_000

    def test_register_strategy_rejects_nonpositive_budget(self) -> None:
        def _stub(_pair: BenchmarkPair, _budget: int) -> list[BriefSegment]:
            return []

        with pytest.raises(ValueError, match="must be positive"):
            register_strategy("bad_strategy", _stub, token_budget=0)
        with pytest.raises(ValueError, match="must be positive"):
            register_strategy("bad_strategy", _stub, token_budget=-1)
        # Make sure no stale entry leaked from the failed registrations.
        context_engine_bench._STRATEGIES.pop("bad_strategy", None)
        _STRATEGY_BUDGETS.pop("bad_strategy", None)

    def test_passing_none_clears_existing_override(self) -> None:
        """Re-registering without a budget should restore the runner-default
        path — important for the notebook-iteration use case."""
        def _stub(_pair: BenchmarkPair, _budget: int) -> list[BriefSegment]:
            return []

        try:
            register_strategy("override_then_clear", _stub, token_budget=1234)
            assert get_strategy_budget("override_then_clear", 8000) == 1234
            register_strategy("override_then_clear", _stub)
            assert get_strategy_budget("override_then_clear", 8000) == 8000
        finally:
            context_engine_bench._STRATEGIES.pop("override_then_clear", None)
            _STRATEGY_BUDGETS.pop("override_then_clear", None)


# -----------------------------------------------------------------------------
# Integration on the real benchmark dataset
# -----------------------------------------------------------------------------


class TestRunOnRealBenchmark:
    """Two-pair smoke run that asserts the cost contrast — naive's brief
    must be strictly bigger than recency's on long histories.

    Limited to the very_long bucket (10 pairs) because that's the only
    slice where recency hits its 8K budget. On short pairs, both
    strategies pack the entire history under their respective budgets,
    so naive and recency are token-equal (with reordered contents).
    """

    def test_naive_emits_more_tokens_than_recency_on_very_long(self) -> None:
        pairs = [p for p in load_pairs() if p.history_bucket == "very_long"][:3]
        results = run(pairs=pairs, strategies=["naive_dump", "recency"])

        by_strategy: dict[str, list] = {"naive_dump": [], "recency": []}
        for r in results:
            by_strategy[r.strategy].append(r)
        assert len(by_strategy["naive_dump"]) == 3
        assert len(by_strategy["recency"]) == 3

        # Pair-by-pair: naive's brief must be at least as large as
        # recency's, and strictly larger on at least one pair.
        bigger_count = 0
        for naive_r, rec_r in zip(by_strategy["naive_dump"], by_strategy["recency"]):
            assert naive_r.pair_id == rec_r.pair_id
            assert naive_r.token_budget == 50_000
            assert rec_r.token_budget == 8000
            assert naive_r.brief_tokens_estimate >= rec_r.brief_tokens_estimate
            if naive_r.brief_tokens_estimate > rec_r.brief_tokens_estimate:
                bigger_count += 1
        assert bigger_count >= 1, (
            "Naive must beat recency on tokens for at least one very_long "
            "pair — recency is supposed to saturate its 8K budget here"
        )

    def test_both_strategies_respect_their_own_budgets(self) -> None:
        pairs = load_pairs()[:5]
        results = run(pairs=pairs, strategies=["naive_dump", "recency"])
        for r in results:
            assert r.brief_tokens_estimate <= r.token_budget, (
                f"{r.strategy} on {r.pair_id} exceeded its own budget "
                f"({r.brief_tokens_estimate} > {r.token_budget})"
            )
