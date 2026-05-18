"""Day 15 — LLM-as-judge tests.

Covers the mock-proxy path end-to-end (the only path active under the
current mock-mode benchmark configuration) and the public interface
shared with the future real-LLM path.

Test surface:

* Tokenizer drops stopwords and short alpha tokens; keeps numeric
  tokens at ≥2 chars (rate %, day count, dates).
* Recall is a set-based fraction; identical tokens repeated don't
  inflate the score.
* Recall → quality mapping respects the 5 thresholds; empty needles
  return vacuous-recall = 1.0.
* Mock proxy on a fact-bearing brief produces a higher score than on
  an unrelated brief.
* Score-briefs round-trips ``brief_text`` + ``query`` + ``ground_truth``
  through the judge interface to a deterministic
  :class:`JudgeResult` list.
* ``summarize_scores`` aggregates per-strategy and per-bucket.
* ``attach_quality_scores_to_results`` adds the quality columns
  without mutating the input.
* The real-LLM path falls back to the mock proxy when the LLM
  returns an unparseable response.
"""
from __future__ import annotations

import json
from pathlib import Path

from benchmarks.judge import (
    JudgeResult,
    _recall,
    _recall_to_score,
    _score_one_llm,
    _score_one_mock,
    _tokenize_for_recall,
    attach_quality_scores_to_results,
    score_briefs,
    summarize_scores,
)
from context_engine.llm.mock import MockClient


class TestTokenizer:
    def test_lowercases_and_splits(self) -> None:
        out = _tokenize_for_recall("Rate Quote 6.25% locked")
        assert "rate" in out
        assert "quote" in out
        assert "25" in out
        assert "locked" in out

    def test_drops_stopwords(self) -> None:
        out = _tokenize_for_recall("the and for you are")
        assert out == set()

    def test_keeps_numerics_at_two_chars(self) -> None:
        out = _tokenize_for_recall("30 days at 6.25")
        assert "30" in out
        assert "25" in out
        assert "days" in out

    def test_drops_short_alpha_tokens(self) -> None:
        out = _tokenize_for_recall("a I we to")
        assert out == set()

    def test_empty_string_returns_empty(self) -> None:
        assert _tokenize_for_recall("") == set()


class TestRecall:
    def test_full_overlap_returns_one(self) -> None:
        assert _recall({"rate", "30"}, {"rate", "30", "extra"}) == 1.0

    def test_no_overlap_returns_zero(self) -> None:
        assert _recall({"rate"}, {"other"}) == 0.0

    def test_partial_overlap(self) -> None:
        assert _recall({"a", "b", "c", "d"}, {"a", "b"}) == 0.5

    def test_empty_needles_returns_one_vacuously(self) -> None:
        """Empty fact set → vacuously perfect recall. Caller flags
        this case via the ``notes`` field so the headline doesn't
        confuse vacuous recall with strong recall."""
        assert _recall(set(), {"anything"}) == 1.0


class TestRecallToScore:
    def test_recall_zero_maps_to_one(self) -> None:
        assert _recall_to_score(0.0) == 1

    def test_recall_one_maps_to_five(self) -> None:
        assert _recall_to_score(1.0) == 5

    def test_thresholds_step_up(self) -> None:
        assert _recall_to_score(0.10) == 1
        assert _recall_to_score(0.25) == 2
        assert _recall_to_score(0.45) == 3
        assert _recall_to_score(0.65) == 4
        assert _recall_to_score(0.85) == 5

    def test_recall_at_threshold_promotes(self) -> None:
        """Recall exactly at threshold should promote (>=)."""
        assert _recall_to_score(0.20) == 2
        assert _recall_to_score(0.40) == 3


class TestScoreOneMock:
    def test_brief_with_facts_scores_high(self) -> None:
        score, q_recall, gt_recall, notes = _score_one_mock(
            brief_text="Loan officer quoted 6.25 percent rate locked for 30 days",
            query="confirm rate quoted",
            ground_truth="rate 6.25 percent locked 30 days",
        )
        assert score >= 4
        assert gt_recall >= 0.8
        assert "gt_has_no_factual_tokens" not in notes

    def test_brief_without_facts_scores_low(self) -> None:
        score, q_recall, gt_recall, notes = _score_one_mock(
            brief_text="unrelated content about parking",
            query="confirm rate quoted",
            ground_truth="rate 6.25 percent locked 30 days",
        )
        assert score <= 2
        assert gt_recall <= 0.2

    def test_courtesy_gt_flagged(self) -> None:
        _, _, _, notes = _score_one_mock(
            brief_text="anything",
            query="thanks bye",
            ground_truth="and the for you",  # all stopwords
        )
        assert "gt_has_no_factual_tokens" in notes


class TestScoreBriefs:
    def test_returns_one_result_per_row(self) -> None:
        rows = [
            {
                "pair_id": "p1",
                "strategy": "recency",
                "bucket": "short",
                "brief_text": "rate 6.25 percent",
            },
        ]
        pair_index = {
            "p1": {"query": "rate", "ground_truth": "6.25 percent"},
        }
        out = score_briefs(rows=rows, pair_index=pair_index, client=MockClient())
        assert len(out) == 1
        assert out[0].pair_id == "p1"
        assert out[0].strategy == "recency"
        assert out[0].judge_mode == "mock_proxy"

    def test_deterministic_under_mock(self) -> None:
        rows = [
            {
                "pair_id": "p1",
                "strategy": "recency",
                "bucket": "short",
                "brief_text": "anything",
            },
        ]
        pair_index = {"p1": {"query": "q", "ground_truth": "rate quote"}}
        first = score_briefs(rows=rows, pair_index=pair_index, client=MockClient())
        second = score_briefs(rows=rows, pair_index=pair_index, client=MockClient())
        assert first[0].quality_score == second[0].quality_score


class TestSummarize:
    def test_per_strategy_aggregates(self) -> None:
        results = [
            JudgeResult(
                pair_id=f"p{i}",
                strategy="recency",
                bucket="short",
                quality_score=4,
                query_recall=0.5,
                ground_truth_recall=0.5,
                judge_mode="mock_proxy",
                notes="",
            )
            for i in range(3)
        ] + [
            JudgeResult(
                pair_id=f"p{i}",
                strategy="hybrid",
                bucket="short",
                quality_score=5,
                query_recall=0.9,
                ground_truth_recall=0.9,
                judge_mode="mock_proxy",
                notes="",
            )
            for i in range(3)
        ]
        summary = summarize_scores(results)
        assert summary["recency"]["mean_quality"] == 4.0
        assert summary["hybrid"]["mean_quality"] == 5.0
        assert summary["recency"]["score_histogram"][4] == 3
        assert summary["hybrid"]["score_histogram"][5] == 3


class TestAttachQualityScores:
    def test_adds_quality_columns_without_mutating_input(self, tmp_path: Path) -> None:
        payload = {
            "results": [
                {
                    "pair_id": "p1",
                    "strategy": "recency",
                    "bucket": "short",
                    "brief_text": "rate 6.25 percent",
                },
            ],
            "n_pairs": 1,
        }
        results_path = tmp_path / "results.json"
        results_path.write_text(json.dumps(payload), encoding="utf-8")

        judged = [
            JudgeResult(
                pair_id="p1",
                strategy="recency",
                bucket="short",
                quality_score=4,
                query_recall=0.5,
                ground_truth_recall=0.6,
                judge_mode="mock_proxy",
                notes="",
            )
        ]
        out_path = attach_quality_scores_to_results(
            results_path=results_path, judged=judged
        )

        # Default writes to a sibling file, not in-place.
        assert out_path != results_path
        assert out_path.exists()

        out_payload = json.loads(out_path.read_text(encoding="utf-8"))
        row = out_payload["results"][0]
        assert row["quality_score"] == 4
        assert row["judge_mode"] == "mock_proxy"
        assert "judge_summary" in out_payload


class _DigitClient:
    """Test double returning a fixed single-digit string from complete()."""

    name = "digit-stub"
    model = "digit-stub-model"

    def __init__(self, response: str) -> None:
        self._response = response

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        return self._response

    def summarize(self, text: str, *, max_tokens: int = 256) -> str:
        return "noop"


class TestRealLLMPath:
    def test_parses_single_digit_response(self) -> None:
        score, qr, gtr, notes = _score_one_llm(
            brief_text="x",
            query="q",
            ground_truth="gt",
            client=_DigitClient("4"),
        )
        assert score == 4
        assert notes == "llm_judged"

    def test_unparseable_response_falls_back_to_mock(self) -> None:
        score, qr, gtr, notes = _score_one_llm(
            brief_text="rate 6.25",
            query="rate quoted",
            ground_truth="rate 6.25 quote",
            client=_DigitClient("I cannot answer that."),
        )
        assert score >= 3
        assert "fallback_mock_proxy" in notes

    def test_verbose_llm_response_still_parsed(self) -> None:
        score, _, _, notes = _score_one_llm(
            brief_text="x",
            query="q",
            ground_truth="gt",
            client=_DigitClient("Score: 3 — partial recall"),
        )
        assert score == 3
        assert notes == "llm_judged"
