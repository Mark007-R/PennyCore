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
from typing import Iterable

from benchmarks.dataset_loader import BenchmarkPair, load_pairs
from benchmarks.registry import (
    RetrievalFn,
    _STRATEGIES,
    _STRATEGY_BUDGETS,
    get_strategy_budget,
    list_strategies,
    register_strategy,
)
from context_engine.brief_assembly import pack_segments

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
DEFAULT_TOKEN_BUDGET = 8000

# Re-export the public registry surface so existing callers (including
# the Day-12 test file) that did
# ``from benchmarks.context_engine_bench import register_strategy``
# keep working unchanged. The single source of truth still lives in
# :mod:`benchmarks.registry`.
__all__ = [
    "BriefResult",
    "DEFAULT_TOKEN_BUDGET",
    "RetrievalFn",
    "get_strategy_budget",
    "list_strategies",
    "main",
    "register_strategy",
    "run",
]


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
    # Per-strategy budget override (Day 13). The runner's ``token_budget``
    # argument is the floor for strategies that don't have a registered
    # override; naive_dump bumps to 50K, recency keeps 8K.
    effective_budget = get_strategy_budget(strategy_name, token_budget)
    t0 = time.perf_counter()
    segments = fn(pair, effective_budget)
    brief_text = pack_segments(
        segments,
        token_budget=effective_budget,
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
        token_budget=effective_budget,
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
    """Per-strategy aggregates for a quick eyeball check.

    Adds Day-13 fields: ``effective_token_budget`` (the strategy's
    own budget, which may differ from the runner's default for naive),
    ``budget_saturation_pct`` (avg_brief_tokens / budget), and per-
    bucket latency + token rollups so the comparison table can lift
    these directly without re-parsing the per-pair results.
    """
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
        effective_budget = rs[0].token_budget  # constant across pairs per strategy
        sat_pct = (avg_tokens / effective_budget) * 100 if effective_budget else 0.0

        by_bucket: dict[str, dict[str, float]] = {}
        bucket_groups: dict[str, list[BriefResult]] = {}
        for r in rs:
            bucket_groups.setdefault(r.bucket, []).append(r)
        for bkt, bkt_rs in bucket_groups.items():
            bkt_lats = sorted(x.retrieval_latency_ms for x in bkt_rs)
            bn = len(bkt_lats)
            bp50 = bkt_lats[bn // 2]
            bp95 = bkt_lats[max(0, int(bn * 0.95) - 1)]
            bavg_tok = sum(x.brief_tokens_estimate for x in bkt_rs) / bn
            by_bucket[bkt] = {
                "n_pairs": bn,
                "avg_brief_tokens": round(bavg_tok, 1),
                "retrieval_latency_ms_p50": round(bp50, 4),
                "retrieval_latency_ms_p95": round(bp95, 4),
                "budget_saturation_pct": round(
                    (bavg_tok / effective_budget) * 100 if effective_budget else 0.0,
                    1,
                ),
            }

        summary[sname] = {
            "n_pairs": n,
            "effective_token_budget": effective_budget,
            "retrieval_latency_ms": {"p50": round(p50, 4), "p95": round(p95, 4)},
            "avg_brief_tokens": round(avg_tokens, 1),
            "budget_saturation_pct": round(sat_pct, 1),
            "by_bucket": by_bucket,
        }
    return summary


def _ensure_strategies_imported() -> None:
    """Import the strategies subpackage so registration side-effects fire.

    Library callers (tests, notebooks) generally import a specific
    strategy and trigger their own registration. The CLI runner does
    not — so the runner calls this once before consulting the
    registry. Idempotent: a second import is a no-op (Python caches).
    """
    import benchmarks.strategies  # noqa: F401 — side-effect register


def main() -> None:
    _ensure_strategies_imported()
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
