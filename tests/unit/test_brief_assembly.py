"""Day 7 — brief assembler unit tests.

Covers:
  * `pack_segments` honors token budget under recency ordering.
  * Header is included when it fits and dropped when it doesn't.
  * Skip-don't-stop semantics: a long segment is skipped, shorter
    later segments still fit.
  * Empty input → empty (or header-only) brief.
  * `token_budget <= 0` raises `ValueError`.
  * The packed brief's `len(text) // 4` never exceeds `token_budget`
    (the takehome evaluator's exact estimator).
"""

from __future__ import annotations

import pytest

from context_engine.brief_assembly import pack_segments
from context_engine.retrieval.recency import estimate_tokens
from contracts import BriefSegment, SegmentSource


def _seg(body: str, priority: float = 0.0) -> BriefSegment:
    return BriefSegment(
        source=SegmentSource.RECENT_MESSAGE,
        body=body,
        token_estimate=estimate_tokens(body),
        priority=priority,
    )


class TestPackSegmentsBudget:
    def test_empty_segments_returns_just_header(self) -> None:
        out = pack_segments([], token_budget=200, header="Borrower: x")
        assert out == "Borrower: x"

    def test_empty_segments_no_header_returns_empty(self) -> None:
        out = pack_segments([], token_budget=200)
        assert out == ""

    def test_single_segment_under_budget(self) -> None:
        seg = _seg("hello world")
        out = pack_segments([seg], token_budget=200)
        assert out == "hello world"

    def test_packs_in_caller_order(self) -> None:
        # Caller is responsible for sort order; we just preserve it.
        a = _seg("first segment text", priority=10.0)
        b = _seg("second segment text", priority=5.0)
        out = pack_segments([a, b], token_budget=200)
        assert out == "first segment text\nsecond segment text"

    def test_skip_dont_stop_when_budget_exhausted(self) -> None:
        # Budget = 30 tokens (~120 chars). Three candidates: a giant
        # one that exceeds the whole budget, a small one that fits
        # next, then a medium one that also fits.
        giant = _seg("X" * 400)  # ~100 tokens — too big
        small = _seg("small")    # ~1 token
        medium = _seg("Y" * 80)  # ~20 tokens
        out = pack_segments([giant, small, medium], token_budget=30)
        # `giant` skipped, `small` + `medium` packed
        assert "small" in out
        assert "Y" * 80 in out
        assert "X" * 400 not in out

    def test_token_budget_strictly_honored_under_evaluator_estimator(self) -> None:
        # Build 50 segments of varying sizes, pack into a tight budget,
        # verify the evaluator's `len(text) // 4` measure is under
        # budget. This is the invariant the takehome evaluator's
        # scenario 2 will check.
        segs = [_seg(f"message {i} " + "Z" * (10 + i * 3)) for i in range(50)]
        budget = 100
        brief = pack_segments(segs, token_budget=budget, header="Borrower: bench")
        assert estimate_tokens(brief) <= budget, (
            f"brief is {estimate_tokens(brief)} tokens, budget was {budget}"
        )

    def test_header_dropped_when_budget_too_small(self) -> None:
        # Header is "Borrower: x" = 11 chars = 2 tokens + 1 newline = 3
        # Budget = 1 means even header doesn't fit.
        out = pack_segments([_seg("hi")], token_budget=1, header="Borrower: x")
        # Header dropped; "hi" is 0 tokens (`len("hi") // 4 == 0`) + 1 newline = 1 → fits.
        assert "Borrower: x" not in out

    def test_zero_budget_raises(self) -> None:
        with pytest.raises(ValueError):
            pack_segments([_seg("hi")], token_budget=0)

    def test_negative_budget_raises(self) -> None:
        with pytest.raises(ValueError):
            pack_segments([_seg("hi")], token_budget=-5)

    def test_large_budget_packs_everything(self) -> None:
        segs = [_seg(f"segment {i}") for i in range(20)]
        brief = pack_segments(segs, token_budget=10_000, header="H")
        for i in range(20):
            assert f"segment {i}" in brief
        assert brief.startswith("H\n")


class TestPackSegmentsHeaderReservation:
    def test_header_consumed_before_segments(self) -> None:
        # Tight budget: header should be present even at the cost of
        # dropping a low-priority segment.
        segs = [_seg("X" * 200)]  # ~50 tokens
        out = pack_segments(segs, token_budget=10, header="Borrower: x")
        assert "Borrower: x" in out
        assert "X" * 200 not in out  # didn't fit
