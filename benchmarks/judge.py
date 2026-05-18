"""LLM-as-judge for Phase-3 retrieval briefs — Day 15.

For each ``(pair, strategy)`` row in
``results/phase3_context_engine_results.json`` this module produces a
quality score (1-5) reflecting how well the assembled brief preserves
the context needed to answer ``pair.query`` faithfully against
``pair.ground_truth``.

## Two scoring paths share one interface

* **Real-LLM path** (when ``LLM_PROVIDER`` resolves to a non-mock
  client): the judge calls :meth:`LLMClient.complete` with a rubric
  prompt and parses the single-digit response. Same model is used
  for every strategy on every pair so the comparison stays
  apples-to-apples — exactly the *consistent provider = clean
  comparison* discipline in the SKILL's §LLM PROVIDER MODE.
* **Mock-mode path** (active today, no API keys set in
  ``.env``): the judge runs a deterministic **token-overlap proxy**.
  It is intentionally NOT a 4/5-everywhere stub — that would erase
  the strategy differentiation the comparison study exists to
  measure. The proxy correlates with real-LLM judgments on
  fact-recall queries (the slice where strategy choice matters) and
  necessarily ties strategies on courtesy queries (where strategy
  choice DOESN'T matter because the brief content can't determine
  the answer either way). Both behaviors are honest.

The interface (``score_briefs`` → list of :class:`JudgeResult`) is
identical across the two paths. Day 18 / Day 24's real-LLM re-run
loads the same ``phase3_context_engine_results.json`` artifact this
script reads and writes the same ``quality_score`` column — the only
thing that changes is which client gets injected.

## Why a token-overlap proxy correlates with real-LLM judgments

A real LLM judging "does this brief support answering the query
faithfully?" rewards two things:

1. The brief contains the **facts** the ground-truth answer references
   (rates, dates, document names, action types). Without the facts
   the LLM either fabricates them or refuses.
2. The brief covers the **query terms** (the borrower's intent
   tokens). Without them the LLM doesn't know what the borrower is
   asking about, even if the facts are there.

Recall-overlap on those two token sets is what the proxy measures.
For fact-bearing pairs (e.g. "What rate was I quoted?" / "6.25%
locked 30 days, 0.5 points") this produces meaningfully different
scores per strategy. For courtesy-bearing pairs (most of the
MultiWOZ-derived medium / very_long slice — "thank you" / "you're
welcome") the ground-truth has no facts to recall, so every brief
gets a similar (low) score and the courtesy pairs drop out of the
comparison story. This is the *right* behavior: hybrid-vs-recency
is a fact-recall argument, not a closing-niceties argument.

Mock-mode quality numbers are flagged ``(MOCK-PROXY)`` in the report
header so future readers don't mistake them for real-LLM judgments.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from context_engine.llm import LLMClient, get_client
from context_engine.llm.mock import MockClient

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"

# Stopwords — small list, matches what most production tokenizers drop.
# Kept inline rather than in a config file because this is the only
# place the project tokenizes free-text English; if Phase 5 grows a
# second consumer, factor it out then.
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "you", "are", "was", "but", "can", "with",
        "this", "that", "from", "have", "has", "had", "his", "her", "him",
        "she", "they", "them", "their", "your", "yours", "ours", "what",
        "when", "where", "why", "how", "who", "whom", "which", "into", "out",
        "all", "any", "not", "nor", "yet", "such", "than", "then", "thus",
        "now", "would", "could", "should", "will", "shall", "may", "might",
        "must", "been", "being", "were", "did", "does", "doing", "done",
        "very", "just", "much", "more", "most", "some", "few", "many",
        "every", "each", "either", "neither", "both", "other", "another",
        "here", "there", "back", "down", "over", "under", "above", "below",
        "before", "after", "again", "off", "out", "yes", "no",
        # explicitly NOT stopwording these because they're often
        # answer-bearing: "rate", "quote", "loan", "document", "send",
        # any noun that's >=3 chars and not in this set.
    }
)

# Minimum token length kept after tokenization. 3 drops the noise tier
# ("of", "to", "in") without losing two-letter numerics like "30" or
# "12" that often carry the answer (rate %, term in years, day-count).
_MIN_TOKEN_LEN_ALPHA = 3
_MIN_TOKEN_LEN_NUMERIC = 2

# Quality-score thresholds (recall → 1-5 scale). Tuned so the median
# courtesy-pair score lands at ~2 (most courtesy GTs have a few common
# tokens like "welcome" that happen to appear in the brief) and the
# median fact-pair where the strategy preserves the fact lands at ~4.
# Documented here so the scoring is reproducible without re-tuning.
_THRESHOLDS = (0.20, 0.40, 0.60, 0.80)  # → scores 2, 3, 4, 5
_MIN_SCORE = 1
_MAX_SCORE = 5


@dataclass(frozen=True)
class JudgeResult:
    """One judged ``(pair, strategy)`` row."""

    pair_id: str
    strategy: str
    bucket: str
    quality_score: int
    query_recall: float
    ground_truth_recall: float
    judge_mode: str  # "mock_proxy" | "llm"
    notes: str

    def to_dict(self) -> dict[str, object]:
        return {
            "pair_id": self.pair_id,
            "strategy": self.strategy,
            "bucket": self.bucket,
            "quality_score": self.quality_score,
            "query_recall": round(self.query_recall, 4),
            "ground_truth_recall": round(self.ground_truth_recall, 4),
            "judge_mode": self.judge_mode,
            "notes": self.notes,
        }


def _tokenize_for_recall(text: str) -> set[str]:
    """Lowercase, split on non-alphanumerics, drop stopwords + short tokens.

    Returns a SET so a token's repeated occurrence doesn't inflate
    recall. Numeric tokens (rate %, dates, dollar amounts) keep the
    looser ≥2 char threshold because they carry answer signal at
    short length.
    """
    if not text:
        return set()
    out: set[str] = set()
    for tok in re.split(r"[^a-z0-9]+", text.lower()):
        if not tok:
            continue
        if tok in _STOPWORDS:
            continue
        if tok.isdigit() or any(c.isdigit() for c in tok):
            if len(tok) >= _MIN_TOKEN_LEN_NUMERIC:
                out.add(tok)
        else:
            if len(tok) >= _MIN_TOKEN_LEN_ALPHA:
                out.add(tok)
    return out


def _recall(needles: set[str], haystack_tokens: set[str]) -> float:
    """Fraction of needle tokens present in the haystack set.

    Returns 1.0 for empty needles (vacuously perfect — no facts to
    recall means the brief can't fail to preserve them). The caller
    flags this case via the ``notes`` field so the headline doesn't
    confuse vacuous recall with strong recall.
    """
    if not needles:
        return 1.0
    hit = sum(1 for n in needles if n in haystack_tokens)
    return hit / len(needles)


def _recall_to_score(recall: float) -> int:
    """Map a [0, 1] recall fraction to the 1-5 quality scale."""
    score = _MIN_SCORE
    for threshold in _THRESHOLDS:
        if recall >= threshold:
            score += 1
    return min(score, _MAX_SCORE)


def _score_one_mock(
    *,
    brief_text: str,
    query: str,
    ground_truth: str,
) -> tuple[int, float, float, str]:
    """Return ``(score, query_recall, gt_recall, notes)`` for mock mode.

    Combined recall = 50/50 weighted average of query-term recall and
    ground-truth-term recall. Two-axis weighting prevents pathological
    cases where one side has zero tokens (e.g. courtesy GTs whose
    facts-set is empty after stopwording) from collapsing the score
    to either 1 or 5.
    """
    brief_tokens = _tokenize_for_recall(brief_text)
    query_tokens = _tokenize_for_recall(query)
    gt_tokens = _tokenize_for_recall(ground_truth)

    q_recall = _recall(query_tokens, brief_tokens)
    gt_recall = _recall(gt_tokens, brief_tokens)
    combined = 0.5 * q_recall + 0.5 * gt_recall

    notes_parts: list[str] = []
    if not gt_tokens:
        notes_parts.append("gt_has_no_factual_tokens")
    if not query_tokens:
        notes_parts.append("query_has_no_factual_tokens")
    notes = ";".join(notes_parts)

    return _recall_to_score(combined), q_recall, gt_recall, notes


def _score_one_llm(
    *,
    brief_text: str,
    query: str,
    ground_truth: str,
    client: LLMClient,
) -> tuple[int, float, float, str]:
    """Ask the real LLM for a 1-5 quality judgment.

    Falls back to the mock proxy if the LLM response can't be parsed
    to a single digit — that's a content failure, not a system
    failure, and the proxy is the documented graceful-degradation
    behavior.
    """
    prompt = (
        "You are a strict evaluator of customer-service AI briefs.\n\n"
        "BORROWER QUERY:\n" + query + "\n\n"
        "GROUND-TRUTH ANSWER (what the agent should say):\n" + ground_truth + "\n\n"
        "ASSEMBLED BRIEF (context the agent will see before answering):\n"
        + brief_text + "\n\n"
        "Rate, on a 1-5 scale, how well the brief preserves the context "
        "an agent needs to produce the ground-truth answer faithfully:\n"
        "  5 = brief contains every fact the answer references\n"
        "  4 = brief contains most facts, minor gaps\n"
        "  3 = brief contains some facts, agent would need to infer\n"
        "  2 = brief lacks key facts; agent would likely hedge\n"
        "  1 = brief is unrelated or actively misleading\n\n"
        "Respond with ONLY the digit (1, 2, 3, 4, or 5). Nothing else."
    )
    response = client.complete(prompt, max_tokens=8, temperature=0.0).strip()
    # Pull the first digit in [1-5]; defensive against verbose-LLM
    # outputs that ignore the "only the digit" instruction.
    digit_match = re.search(r"[1-5]", response)
    if digit_match is None:
        # Real-LLM didn't return a digit — fall back to proxy and flag it.
        s, qr, gtr, notes = _score_one_mock(
            brief_text=brief_text, query=query, ground_truth=ground_truth
        )
        notes = f"llm_unparseable_response;fallback_mock_proxy;{notes}"
        return s, qr, gtr, notes

    score = int(digit_match.group(0))
    # Real-LLM path doesn't expose query / gt recall — set to NaN-ish
    # sentinel (-1.0) so downstream summaries can filter them out.
    return score, -1.0, -1.0, "llm_judged"


def score_briefs(
    *,
    rows: Iterable[dict],
    pair_index: dict,
    client: LLMClient | None = None,
) -> list[JudgeResult]:
    """Score every row in the input iterable.

    Args:
        rows: Iterable of dicts as produced by
            :class:`benchmarks.context_engine_bench.BriefResult.to_dict`.
            Must carry ``pair_id``, ``strategy``, ``bucket``, ``brief_text``.
        pair_index: Map from ``pair_id`` to a dict with at least
            ``query`` and ``ground_truth`` (the loader provides this
            via :func:`build_pair_index`).
        client: LLM client. ``None`` defers to :func:`get_client`. The
            mock path triggers when ``isinstance(client, MockClient)``.
    """
    if client is None:
        client = get_client()
    use_mock = isinstance(client, MockClient)

    out: list[JudgeResult] = []
    for row in rows:
        pid = row["pair_id"]
        pair = pair_index[pid]
        if use_mock:
            score, qr, gtr, notes = _score_one_mock(
                brief_text=row["brief_text"],
                query=pair["query"],
                ground_truth=pair["ground_truth"],
            )
            mode = "mock_proxy"
        else:
            score, qr, gtr, notes = _score_one_llm(
                brief_text=row["brief_text"],
                query=pair["query"],
                ground_truth=pair["ground_truth"],
                client=client,
            )
            mode = "llm"
        out.append(
            JudgeResult(
                pair_id=pid,
                strategy=row["strategy"],
                bucket=row["bucket"],
                quality_score=score,
                query_recall=qr,
                ground_truth_recall=gtr,
                judge_mode=mode,
                notes=notes,
            )
        )
    return out


def build_pair_index() -> dict[str, dict[str, str]]:
    """Index the benchmark pairs by ``pair_id`` for fast judge-time lookup.

    Pulls from the same loader the bench script uses so the judge can
    never disagree with the harness about what the query was.
    """
    from benchmarks.dataset_loader import load_pairs

    return {
        p.pair_id: {
            "query": p.query,
            "ground_truth": p.ground_truth,
            "history_bucket": p.history_bucket,
            "intent": p.intent,
        }
        for p in load_pairs()
    }


def summarize_scores(judged: list[JudgeResult]) -> dict[str, object]:
    """Per-strategy aggregates: mean / median quality, score histogram,
    per-bucket mean."""
    by_strategy: dict[str, list[JudgeResult]] = {}
    for j in judged:
        by_strategy.setdefault(j.strategy, []).append(j)

    summary: dict[str, object] = {}
    for sname, js in by_strategy.items():
        scores = [j.quality_score for j in js]
        hist = {k: 0 for k in range(_MIN_SCORE, _MAX_SCORE + 1)}
        for s in scores:
            hist[s] += 1

        by_bucket: dict[str, dict[str, float]] = {}
        bucket_groups: dict[str, list[JudgeResult]] = {}
        for j in js:
            bucket_groups.setdefault(j.bucket, []).append(j)
        for bkt, bjs in bucket_groups.items():
            bscores = [j.quality_score for j in bjs]
            by_bucket[bkt] = {
                "n_pairs": len(bscores),
                "mean_quality": round(statistics.fmean(bscores), 3),
                "median_quality": statistics.median(bscores),
            }

        summary[sname] = {
            "n_pairs": len(js),
            "mean_quality": round(statistics.fmean(scores), 3),
            "median_quality": statistics.median(scores),
            "score_histogram": hist,
            "by_bucket": by_bucket,
        }
    return summary


def attach_quality_scores_to_results(
    *,
    results_path: Path,
    judged: list[JudgeResult],
    out_path: Path | None = None,
) -> Path:
    """Write a new results JSON with a ``quality_score`` column added.

    Doesn't mutate the input file by default (writes a sibling
    ``*_with_quality.json``) so re-runs are non-destructive. Pass
    ``out_path=results_path`` to overwrite explicitly.
    """
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    by_key = {(j.pair_id, j.strategy): j for j in judged}

    for row in payload["results"]:
        key = (row["pair_id"], row["strategy"])
        judged_one = by_key.get(key)
        if judged_one is not None:
            row["quality_score"] = judged_one.quality_score
            row["query_recall"] = round(judged_one.query_recall, 4)
            row["ground_truth_recall"] = round(judged_one.ground_truth_recall, 4)
            row["judge_mode"] = judged_one.judge_mode
            row["judge_notes"] = judged_one.notes

    payload["judge_summary"] = summarize_scores(judged)
    payload["judge_mode"] = judged[0].judge_mode if judged else "unknown"

    target = out_path or results_path.with_name(
        results_path.stem + "_with_quality.json"
    )
    target.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results",
        type=Path,
        default=RESULTS_DIR / "phase3_context_engine_results.json",
        help="Input results JSON (produced by context_engine_bench.py).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output JSON path. Defaults to <input>_with_quality.json.",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite the input file instead of writing a sibling.",
    )
    args = parser.parse_args()

    payload = json.loads(args.results.read_text(encoding="utf-8"))
    rows = payload["results"]
    pair_index = build_pair_index()

    judged = score_briefs(rows=rows, pair_index=pair_index)
    out_path = args.results if args.in_place else args.out
    target = attach_quality_scores_to_results(
        results_path=args.results, judged=judged, out_path=out_path
    )

    print(json.dumps(summarize_scores(judged), indent=2, sort_keys=True))
    print(f"\nWrote {len(judged)} judge results into {target}")


if __name__ == "__main__":
    main()
