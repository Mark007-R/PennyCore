"""Context-engine comparison-study harness — Phase 3 (Days 12-15).

Runs all registered retrieval strategies over the 200-pair benchmark
dataset and records, per pair, the assembled brief's tokens, retrieval
latency, segment-source mix, and the brief itself for downstream
LLM-as-judge scoring (Day 15).

This file is the harness — the "what to measure and where to put the
results" — not the strategies themselves. Strategies live in
``context_engine.retrieval.*`` and register here via
``register_strategy``. On Day 12 only the recency baseline is wired up;
Days 13-15 add semantic, summarized, and hybrid as they ship.

LLM-as-judge scoring is intentionally NOT in this file. The bench
produces a ``results/phase3_context_engine_results.json`` artifact that
holds the assembled briefs; the Day-15 judge script reads that
artifact, calls the LLM (or mock judge) once per (pair, strategy), and
appends a ``quality_score`` column. Keeping the two phases separate
lets us re-judge old runs after prompt tweaks without re-running the
expensive retrieval step.

Smoke mode (``--smoke``) runs only 5 pairs, registers only ``recency``,
and writes the result to ``results/phase3_context_engine_smoke.json``.
Used by the CI / Day-12 verification path.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from benchmarks.dataset_loader import BenchmarkPair, load_pairs
from context_engine.brief_assembly import pack_segments
from context_engine.retrieval.recency import recency_segments
from contracts import BriefSegment

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
DEFAULT_TOKEN_BUDGET = 8000


@dataclass(frozen=True)
class BriefResult:
    """One strategy's output for one pair."""

    pair_id: str
    strategy: str
    bucket: str
    history_message_count: int
    token_budget: int
    brief_tokens_estimate: int
    brief_text: str
    segment_source_counts: dict[str, int]
    retrieval_latency_ms: float
    notes: str

    def to_dict(self) -> dict[str, object]:
        return {
            "pair_id": self.pair_id,
            "strategy": self.strategy,
            "bucket": self.bucket,
            "history_message_count": self.history_message_count,
            "token_budget": self.token_budget,
            "brief_tokens_estimate": self.brief_tokens_estimate,
            "brief_text": self.brief_text,
            "segment_source_counts": dict(self.segment_source_counts),
            "retrieval_latency_ms": round(self.retrieval_latency_ms, 4),
            "notes": self.notes,
        }


# A strategy is anything callable as ``(pair, token_budget) -> list[BriefSegment]``.
# Registration happens at import time of the strategy modules; the harness
# doesn't know what strategies exist beyond what the registry says.
RetrievalFn = Callable[[BenchmarkPair, int], list[BriefSegment]]
_STRATEGIES: dict[str, RetrievalFn] = {}


def register_strategy(name: str, fn: RetrievalFn) -> None:
    """Add a strategy to the registry. Last-write-wins for re-registration.

    Re-registration is allowed (not raised on) so a notebook re-import
    can iterate on a strategy without restarting the kernel.
    """
    _STRATEGIES[name] = fn


def list_strategies() -> list[str]:
    return sorted(_STRATEGIES)


# -----------------------------------------------------------------------------
# Built-in: recency baseline (Day 7 production retrieval, Phase 3 baseline).
# -----------------------------------------------------------------------------


def _recency_strategy(pair: BenchmarkPair, token_budget: int) -> list[BriefSegment]:
    return recency_segments(
        messages=pair.history.messages,
        actions=pair.history.actions,
    )


register_strategy("recency", _recency_strategy)


# -----------------------------------------------------------------------------
# Run loop
# -----------------------------------------------------------------------------


def _run_one(
    *,
    pair: BenchmarkPair,
    strategy_name: str,
    fn: RetrievalFn,
    token_budget: int,
) -> BriefResult:
    t0 = time.perf_counter()
    segments = fn(pair, token_budget)
    brief_text = pack_segments(
        segments,
        token_budget=token_budget,
        header=f"Borrower: {pair.customer_id} (tenant={pair.tenant_id})",
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000

    # The number of segments that actually landed in the brief is what
    # matters for the source-mix breakdown — pre-pack segments are not
    # the same as post-pack content.
    seg_counts: Counter[str] = Counter()
    for seg in segments:
        if seg.body in brief_text:
            seg_counts[seg.source.value] += 1

    return BriefResult(
        pair_id=pair.pair_id,
        strategy=strategy_name,
        bucket=pair.history_bucket,
        history_message_count=pair.history_message_count,
        token_budget=token_budget,
        brief_tokens_estimate=len(brief_text) // 4,
        brief_text=brief_text,
        segment_source_counts=dict(seg_counts),
        retrieval_latency_ms=elapsed_ms,
        notes="",
    )


def run(
    *,
    pairs: Iterable[BenchmarkPair],
    strategies: Iterable[str] | None = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> list[BriefResult]:
    """Run every selected strategy on every pair. Returns flat list of results.

    Caller is responsible for serializing the returned list and for
    invoking the LLM-as-judge step downstream (Day 15).
    """
    if strategies is None:
        strategy_names = list_strategies()
    else:
        strategy_names = list(strategies)
        unknown = set(strategy_names) - set(_STRATEGIES)
        if unknown:
            raise KeyError(f"Unknown strategies: {sorted(unknown)}")

    pair_list = list(pairs)
    out: list[BriefResult] = []
    for sname in strategy_names:
        fn = _STRATEGIES[sname]
        for pair in pair_list:
            out.append(
                _run_one(
                    pair=pair, strategy_name=sname, fn=fn, token_budget=token_budget
                )
            )
    return out


def _summarize(results: list[BriefResult]) -> dict[str, object]:
    """Per-strategy aggregates for a quick eyeball check."""
    by_strategy: dict[str, list[BriefResult]] = {}
    for r in results:
        by_strategy.setdefault(r.strategy, []).append(r)
    summary: dict[str, object] = {}
    for sname, rs in by_strategy.items():
        latencies = sorted(r.retrieval_latency_ms for r in rs)
        n = len(latencies)
        p50 = latencies[n // 2]
        p95 = latencies[max(0, int(n * 0.95) - 1)]
        avg_tokens = sum(r.brief_tokens_estimate for r in rs) / n
        summary[sname] = {
            "n_pairs": n,
            "retrieval_latency_ms": {"p50": round(p50, 4), "p95": round(p95, 4)},
            "avg_brief_tokens": round(avg_tokens, 1),
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="5 pairs, recency only")
    parser.add_argument("--token-budget", type=int, default=DEFAULT_TOKEN_BUDGET)
    parser.add_argument(
        "--strategies",
        type=str,
        default="",
        help="Comma-separated. Empty = all registered.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output JSON path. Defaults to results/phase3_context_engine_*.json",
    )
    args = parser.parse_args()

    pairs = load_pairs()
    if args.smoke:
        pairs = pairs[:5]
        strategies = ["recency"]
        out_path = args.out or RESULTS_DIR / "phase3_context_engine_smoke.json"
    else:
        strategies = (
            [s for s in args.strategies.split(",") if s] if args.strategies else None
        )
        out_path = args.out or RESULTS_DIR / "phase3_context_engine_results.json"

    results = run(
        pairs=pairs, strategies=strategies, token_budget=args.token_budget
    )
    payload = {
        "registered_strategies": list_strategies(),
        "token_budget": args.token_budget,
        "n_pairs": len(pairs),
        "results": [r.to_dict() for r in results],
        "summary": _summarize(results),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))
    print(f"\nWrote {len(results)} results to {out_path}")


if __name__ == "__main__":
    main()
