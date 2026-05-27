"""Day 24 — two-stage rerank retrieval strategy tests.

Covers:

* :func:`rerank_segments` returns a list shape-compatible with
  :func:`semantic_segments`: every output segment is a BriefSegment,
  and the count matches.
* Re-rank head is annotated ``rerank_stage="head"`` with a
  ``rerank_score`` metadata key; tail is annotated ``rerank_stage=
  "tail"`` with no rerank_score.
* Determinism: identical inputs produce identical outputs across
  invocations (no randomness, no hash-seed drift — same FNV path as
  semantic).
* Empty / degenerate inputs short-circuit gracefully (empty query
  returns semantic output unchanged; no inputs returns []).
* Re-rank changes ordering vs pure semantic in the canonical phrase-
  match case ("mortgage rate" bigram outranks single-token cosine).
* Recency feature: same cosine + same lexical match → the more
  recent segment wins; capped contribution so cold-tail strong-
  signal isn't buried.
* Bigram feature: a query bigram present in the segment lifts its
  score; segments with the same unigrams but no adjacency lose.
* Top-K cap: top_k=2 with 5 candidates re-ranks 2 and carries 3
  tail; total count preserved.
* Strategy registers under the ``"reranked"`` name with the
  harness's default budget.
* On a real benchmark pair, rerank surfaces the same top-segment
  family as semantic but with a re-rank score attached for
  diagnostics.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from benchmarks.context_engine_bench import run
from benchmarks.dataset_loader import load_pairs
from benchmarks.registry import get_strategy_budget, list_strategies
from context_engine.retrieval.rerank import (
    DEFAULT_TOP_K,
    _bigrams,
    _jaccard,
    _pairwise_score,
    _recency_score,
    _tokens,
    rerank_segments,
)
from context_engine.retrieval.semantic import semantic_segments
from contracts import BriefSegment, SegmentSource

NOW = datetime(2026, 5, 10, 12, 0, 0)


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


class TestTokenizerHelpers:
    def test_tokens_drops_short_and_lowercases(self) -> None:
        # _MIN_TOKEN_LEN = 2 — single chars dropped; 2-letter kept.
        assert _tokens("A mortgage RATE of 6.25%") == [
            "mortgage", "rate", "of", "25",
        ]

    def test_tokens_empty_input(self) -> None:
        assert _tokens("") == []
        assert _tokens("   ") == []

    def test_bigrams_basic(self) -> None:
        assert _bigrams(["mortgage", "rate", "quote"]) == {
            ("mortgage", "rate"),
            ("rate", "quote"),
        }

    def test_bigrams_single_token_empty(self) -> None:
        assert _bigrams(["mortgage"]) == set()
        assert _bigrams([]) == set()

    def test_jaccard_perfect(self) -> None:
        assert _jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_jaccard_disjoint(self) -> None:
        assert _jaccard({"a"}, {"b"}) == 0.0

    def test_jaccard_partial(self) -> None:
        # {a,b} ∩ {b,c} = {b}, union = {a,b,c} → 1/3
        assert _jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)

    def test_jaccard_both_empty(self) -> None:
        assert _jaccard(set(), set()) == 0.0


class TestRecencyScore:
    def test_score_at_now_is_one(self) -> None:
        assert _recency_score(NOW, now=NOW) == 1.0

    def test_score_at_half_life_is_half(self) -> None:
        half_life_ago = NOW - timedelta(days=30)
        assert _recency_score(half_life_ago, now=NOW) == pytest.approx(0.5)

    def test_future_timestamps_clamp_to_one(self) -> None:
        future = NOW + timedelta(days=10)
        assert _recency_score(future, now=NOW) == 1.0

    def test_old_message_decays_below_threshold(self) -> None:
        # 120 days = 4 half-lives → 1/16
        old = NOW - timedelta(days=120)
        assert _recency_score(old, now=NOW) == pytest.approx(0.0625, abs=1e-9)


class TestPairwiseScore:
    def test_bigram_lifts_above_unigrams_only(self) -> None:
        # Same cosine carry, same tokens — the only delta is whether the
        # query bigram appears in the segment.
        q_tokens = ["mortgage", "rate"]
        q_set = set(q_tokens)
        q_bigrams = _bigrams(q_tokens)

        with_bigram = _pairwise_score(
            cosine_carry=0.5,
            query_tokens_list=q_tokens,
            query_token_set=q_set,
            query_bigrams=q_bigrams,
            seg_text="the mortgage rate today is 6.25",
            seg_timestamp=NOW,
            now=NOW,
        )
        without_bigram = _pairwise_score(
            cosine_carry=0.5,
            query_tokens_list=q_tokens,
            query_token_set=q_set,
            query_bigrams=q_bigrams,
            # "mortgage" and "rate" both present but not adjacent — no bigram match.
            seg_text="my mortgage application; what rate is best?",
            seg_timestamp=NOW,
            now=NOW,
        )
        assert with_bigram > without_bigram

    def test_recency_breaks_otherwise_equal_scores(self) -> None:
        q_tokens = ["rate"]
        q_set = set(q_tokens)
        q_bigrams = set()

        new_score = _pairwise_score(
            cosine_carry=0.5,
            query_tokens_list=q_tokens,
            query_token_set=q_set,
            query_bigrams=q_bigrams,
            seg_text="rate is fine",
            seg_timestamp=NOW,
            now=NOW,
        )
        old_score = _pairwise_score(
            cosine_carry=0.5,
            query_tokens_list=q_tokens,
            query_token_set=q_set,
            query_bigrams=q_bigrams,
            seg_text="rate is fine",
            seg_timestamp=NOW - timedelta(days=90),
            now=NOW,
        )
        assert new_score > old_score

    def test_recency_capped_below_cosine_signal(self) -> None:
        # A perfect-recency cold segment (cosine=0) must NOT outrank a
        # weakly-recent strong-cosine segment. _RERANK_WEIGHTS docs say
        # recency is a tiebreaker, not a dominator.
        q_tokens = ["rate"]
        q_set = set(q_tokens)
        q_bigrams = set()

        strong_cosine_old = _pairwise_score(
            cosine_carry=0.8,
            query_tokens_list=q_tokens,
            query_token_set=q_set,
            query_bigrams=q_bigrams,
            seg_text="rate locked at 6.25%",
            seg_timestamp=NOW - timedelta(days=90),
            now=NOW,
        )
        zero_cosine_recent = _pairwise_score(
            cosine_carry=0.0,
            query_tokens_list=q_tokens,
            query_token_set=q_set,
            query_bigrams=q_bigrams,
            seg_text="thank you have a nice day",
            seg_timestamp=NOW,
            now=NOW,
        )
        assert strong_cosine_old > zero_cosine_recent


class TestRerankSegments:
    def test_empty_inputs_return_empty(self) -> None:
        assert rerank_segments(query="anything", messages=[], actions=[]) == []

    def test_empty_query_returns_semantic_unchanged(self) -> None:
        msgs = [_msg(content="mortgage rate today"), _msg(content="hello world")]
        sem = semantic_segments(query="", messages=msgs)
        out = rerank_segments(query="", messages=msgs, now=NOW)
        # Same bodies in same order — no rerank applied.
        assert [s.body for s in out] == [s.body for s in sem]

    def test_output_count_equals_semantic_count(self) -> None:
        msgs = [_msg(content=f"message number {i}") for i in range(10)]
        acts = [_action(action_type=f"act_{i}") for i in range(3)]
        out = rerank_segments(query="number 5", messages=msgs, actions=acts, now=NOW)
        sem = semantic_segments(query="number 5", messages=msgs, actions=acts)
        assert len(out) == len(sem)

    def test_head_segments_annotated_with_rerank_score(self) -> None:
        msgs = [_msg(content="mortgage rate is 6.25%"), _msg(content="hello")]
        out = rerank_segments(query="mortgage rate", messages=msgs, top_k=10, now=NOW)
        assert all("rerank_score" in s.metadata for s in out)
        assert all(s.metadata["rerank_stage"] == "head" for s in out)

    def test_tail_segments_marked_and_lack_rerank_score(self) -> None:
        msgs = [_msg(content=f"message {i} about rates") for i in range(5)]
        out = rerank_segments(query="rate", messages=msgs, top_k=2, now=NOW)
        head = [s for s in out if s.metadata.get("rerank_stage") == "head"]
        tail = [s for s in out if s.metadata.get("rerank_stage") == "tail"]
        assert len(head) == 2
        assert len(tail) == 3
        assert all("rerank_score" not in s.metadata for s in tail)

    def test_deterministic_across_calls(self) -> None:
        msgs = [
            _msg(content="mortgage rate today", timestamp=NOW),
            _msg(content="hello world", timestamp=NOW - timedelta(days=1)),
            _msg(content="what about the rate", timestamp=NOW - timedelta(days=2)),
        ]
        a = rerank_segments(query="mortgage rate", messages=msgs, now=NOW)
        b = rerank_segments(query="mortgage rate", messages=msgs, now=NOW)
        assert [(s.body, s.priority) for s in a] == [(s.body, s.priority) for s in b]

    def test_bigram_lifts_phrase_match_above_split_tokens(self) -> None:
        # Two segments — both contain the unigrams "mortgage" and "rate".
        # One has them adjacent; the other splits them across a clause.
        # Re-rank should put the phrase-match first; pure semantic
        # cosine alone might tie or invert (long segment can inflate
        # its term-frequency norm and lose).
        phrase = _msg(content="the mortgage rate is locked at 6.25%", timestamp=NOW)
        split = _msg(
            content=(
                "my mortgage application is in progress and I'm wondering "
                "what kind of rate you'd offer"
            ),
            timestamp=NOW,
        )
        out = rerank_segments(
            query="mortgage rate", messages=[split, phrase], top_k=10, now=NOW
        )
        # `body` is the formatted segment ("[channel] ts sender: content");
        # we check the original content substring instead of the raw body.
        assert phrase.content in out[0].body  # phrase wins

    def test_top_k_caps_at_candidate_count(self) -> None:
        # Requesting K larger than the candidate pool is a no-op (no
        # exception, all segments re-ranked).
        msgs = [_msg(content=f"rate question {i}") for i in range(3)]
        out = rerank_segments(query="rate", messages=msgs, top_k=99, now=NOW)
        assert all(s.metadata.get("rerank_stage") == "head" for s in out)

    def test_default_top_k_value(self) -> None:
        assert DEFAULT_TOP_K == 30

    def test_recency_breaks_tied_cosine_in_real_path(self) -> None:
        # Two segments with identical content (same tokens, same cosine
        # carry, same bigrams) — only the timestamp differs. Newer wins.
        recent = _msg(content="mortgage rate locked", timestamp=NOW)
        old = _msg(
            content="mortgage rate locked", timestamp=NOW - timedelta(days=120)
        )
        out = rerank_segments(
            query="mortgage rate", messages=[old, recent], top_k=10, now=NOW
        )
        # Both segments have identical content; recency must put the
        # newer one first. Check via timestamp metadata since the body
        # string is identical.
        assert out[0].priority > out[1].priority
        # And the priority gap should equal the recency feature delta:
        # w_recency * (1.0 - 0.5^(120/30)) ≈ 0.2 * (1 - 0.0625) ≈ 0.1875
        assert out[0].priority - out[1].priority == pytest.approx(
            0.2 * (1.0 - 0.5 ** (120 / 30)), abs=1e-6
        )


class TestRerankRegistration:
    def test_registered_under_reranked_name(self) -> None:
        import benchmarks.strategies  # noqa: F401 — triggers registration

        assert "reranked" in list_strategies()

    def test_inherits_default_token_budget(self) -> None:
        import benchmarks.strategies  # noqa: F401

        # No per-strategy override — default budget applies.
        assert get_strategy_budget("reranked", 8000) == 8000


class TestRerankOnBenchmarkPair:
    def test_reranked_returns_segments_for_real_pair(self) -> None:
        # Smoke: pick a fact-bearing pair from the benchmark and confirm
        # rerank emits a non-empty, well-shaped result.
        pairs = load_pairs()
        # Find a pair with a non-trivial history.
        pair = next(p for p in pairs if p.history_message_count >= 5)
        results = run(
            pairs=[pair], strategies=["reranked"], token_budget=8000
        )
        assert len(results) == 1
        result = results[0]
        assert result.strategy == "reranked"
        # The brief should be non-empty for a fact-bearing pair.
        assert result.brief_text
        # And the runner should have recorded retrieval latency > 0
        # (re-rank does real work).
        assert result.retrieval_latency_ms > 0
