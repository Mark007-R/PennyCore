"""Naive baseline: dump the full customer history into the LLM prompt.

This is the *relevant counterfactual* for the Phase-3 comparison study
(SKILL "How to beat the naive baseline" section, Day-15 head-to-head
table row 1). It's what most 2025-2026 AI-customer-service tutorials
ship: take every message and every prior action a customer ever sent,
concatenate them into one giant string, and send the whole thing to the
LLM. No retrieval ranking. No relevance signal. No token budget beyond
the model's context-window cap.

Two design knobs that make this an honest baseline rather than a
strawman:

1. **Big-but-finite budget (50K tokens).** The strategy registers
   ``token_budget=50000`` with the harness so the runner sizes
   ``pack_segments`` accordingly. A "real" naive deployment would
   send the whole history up to the model context window — 200K on
   ``claude-sonnet-4-6``. The 50K cap is the choice the SKILL's
   narrative ("dumping 50K tokens vs my 8K-token retrieved brief")
   uses to keep the cost contrast at a realistic 6×, which matches
   what mid-size AI startups actually spend in prod. Using 200K
   would have made naive's cost story look worse than it really is
   in the wild.

2. **Same segment shape as production retrieval.** Naive uses the
   same :class:`contracts.BriefSegment` model, the same
   ``format_message_segment`` / ``format_action_segment`` renderers
   as the Day-7 recency baseline, and the same ``pack_segments``
   assembler. The ONLY differences from recency are (a) no ``+1.0``
   priority boost on actions — naive doesn't reorder anything — and
   (b) the 50K budget instead of 8K. Sharing the renderers means
   the LLM-as-judge step (Day 15) compares strategies on retrieval
   quality, not on formatting noise.

Order in the brief is **chronological-ascending** (oldest first).
Tutorials almost always concatenate history in conversation order so
the LLM reads "from the start"; recency-strategy briefs are
newest-first because they're explicitly ranked. Sorting ascending also
means that if a pathologically long history ever exceeded the 50K cap
(it doesn't, in this dataset — the longest very_long pair tops out at
~12K tokens under our estimator), ``pack_segments``'s skip-don't-stop
semantics would drop the *newest* messages, not the *oldest*. That's
the wrong truncation direction; we accept it because (a) it never
fires on this dataset, and (b) the alternative (sort descending) would
make the brief read newest-first, which doesn't match the "naive
tutorial pattern" we're benchmarking. Documented here so future days
don't "fix" it without thinking.
"""
from __future__ import annotations

from benchmarks.dataset_loader import BenchmarkPair
from benchmarks.registry import register_strategy
from context_engine.retrieval.recency import (
    estimate_tokens,
    format_action_segment,
    format_message_segment,
)
from contracts import BriefSegment, SegmentSource

# The naive baseline's own budget. Chosen to match the SKILL's
# "50K vs 8K" comparison framing — see module docstring §1.
NAIVE_TOKEN_BUDGET = 50_000


def naive_segments(
    *,
    messages,
    actions=(),
) -> list[BriefSegment]:
    """Build a chronologically-ordered segment list with no relevance signal.

    Differences from
    :func:`context_engine.retrieval.recency.recency_segments`:

    * No ``+1.0`` priority boost on actions. Naive treats actions and
      messages identically — they're sorted purely by ``timestamp``.
    * Sort is **ascending** (oldest first), not descending. The brief
      reads in conversation order, which is the tutorial-pattern this
      baseline is benchmarking.

    Empty inputs return ``[]``. Caller is responsible for the budget;
    in practice this is :func:`benchmarks.context_engine_bench._run_one`
    using the 50K budget registered with the harness.
    """
    segments: list[BriefSegment] = []

    for msg in messages:
        body = format_message_segment(msg)
        segments.append(
            BriefSegment(
                source=SegmentSource.RECENT_MESSAGE,
                body=body,
                token_estimate=estimate_tokens(body),
                priority=msg.timestamp.timestamp(),
                metadata={"channel": msg.channel, "sender": msg.sender},
            )
        )

    for act in actions:
        body = format_action_segment(act)
        segments.append(
            BriefSegment(
                source=SegmentSource.PRIOR_ACTION,
                body=body,
                token_estimate=estimate_tokens(body),
                priority=act.timestamp.timestamp(),  # no boost — naive doesn't reorder
                metadata={"action_type": act.action_type},
            )
        )

    # Ascending — oldest first. Chronological reading order is what
    # tutorials produce when they `"".join(history)`.
    segments.sort(key=lambda s: s.priority)
    return segments


def _naive_strategy(
    pair: BenchmarkPair, token_budget: int
) -> list[BriefSegment]:
    # ``token_budget`` is passed by the harness (resolved from this
    # strategy's registered NAIVE_TOKEN_BUDGET); naive doesn't read it
    # because it never trims segments at retrieval time. The assembler
    # enforces the budget when packing.
    return naive_segments(
        messages=pair.history.messages,
        actions=pair.history.actions,
    )


register_strategy("naive_dump", _naive_strategy, token_budget=NAIVE_TOKEN_BUDGET)
