"""Day 27 (Phase 5) — Context-engine naive baseline vs hybrid champion.

The Day-18 Phase-3 wrap locked hybrid as the context-engine champion under
the mock-proxy LLM-as-judge, with an explicit caveat:

    "Real LLM-as-judge re-run is scheduled for Phase 5 / Day 27."

This is that re-run. It does two things the Phase-3 harness deliberately
left for here:

1. **Cost in USD.** The Phase-3 harness reports tokens; the comparison
   story everyone actually cares about is *dollars per 100 queries*. We
   apply documented model pricing to the same brief tokens to produce
   real cost numbers (and a realistic answer-completion budget so the
   ratio isn't pure input-only).

2. **Real-LLM judge on the fact-bearing slice.** The Phase-3 mock-proxy
   correlates with real-LLM judgments on fact-recall queries but ties
   strategies on courtesy turns (where the brief can't determine the
   answer either way). To honor the Day-18 caveat without burning the
   budget on courtesy ties, we run real-LLM judge only on the 50
   fact-bearing mortgage pairs — the slice where retrieval choice
   meaningfully shifts answer quality.

The output is `results/phase5_naive_vs_champion_context_engine.json` plus
two charts in `results/`. The numbers feed the Day-27 report and the
Day-28 Phase-5 wrap.

## Cost model

Documented Anthropic pricing as of project run-time (2026-Q2):

* `claude-sonnet-4-6` — $3.00 / M input tokens, $15.00 / M output tokens.
* Falls back to `MOCK-COST` rates ($0) when `LLM_PROVIDER=mock` or no key
  is set, with the caveat flagged in the JSON header.

The input token count per query is approximated as:

    input_tokens = brief_tokens_estimate + FIXED_SYSTEM_PROMPT_TOKENS + query_tokens

with `FIXED_SYSTEM_PROMPT_TOKENS=200` (a realistic customer-service system
prompt) and `query_tokens` from the dataset (4-char/token estimator
matching the rest of the project). Output is fixed at
`ASSUMED_RESPONSE_TOKENS=180` — a typical mortgage-servicer reply is
2-3 sentences with a rate / date / next-step.

These constants are documented in-module so the cost numbers are
reproducible from the JSON header alone.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv

# Some shells (including this project's) export an empty ANTHROPIC_API_KEY
# at session start; `load_dotenv` without override leaves the empty value
# in place, which silently downgrades the run to MockClient. override=True
# makes .env the source of truth — matches the SKILL's "API keys live in
# .env" invariant.
load_dotenv(override=True)

# Strategy registration side-effects must fire before we consult the
# registry — same pattern the Phase-3 harness uses.
import benchmarks.strategies  # noqa: E402,F401
from benchmarks.context_engine_bench import (  # noqa: E402
    BriefResult,
    _run_one,
)
from benchmarks.dataset_loader import BenchmarkPair, load_pairs  # noqa: E402
from benchmarks.judge import (  # noqa: E402
    JudgeResult,
    _score_one_llm,
    _score_one_mock,
    build_pair_index,
)
from benchmarks.registry import _STRATEGIES, get_strategy_budget  # noqa: E402
from context_engine.llm import get_client  # noqa: E402
from context_engine.llm.mock import MockClient  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
OUT_PATH = RESULTS_DIR / "phase5_naive_vs_champion_context_engine.json"

# Cost model — documented in module docstring §Cost model.
PRICING_USD_PER_M = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.00},
    "mock": {"input": 0.0, "output": 0.0},
}
FIXED_SYSTEM_PROMPT_TOKENS = 200
ASSUMED_RESPONSE_TOKENS = 180

# Strategies in scope for Day 27. Naive is the relevant counterfactual;
# hybrid is the Day-18 champion. Recency is included as the "cheap
# retrieval" reference so the cost frontier has a known anchor.
STRATEGIES_IN_SCOPE = ["naive_dump", "hybrid", "recency"]

# The slice where retrieval choice meaningfully shifts answer quality.
FACT_BEARING_INTENTS = frozenset(
    {
        "rate_lookup",
        "checklist_followup",
        "status_check",
        "refi_question",
        "closing_logistics",
        "handoff_followup",
        "preapproval_expiry",
        "appraisal_outcome",
        "down_payment_breakdown",
        "pmi_question",
    }
)


@dataclass
class CostBreakdown:
    """Per-strategy USD cost rollup at the documented model rates."""

    avg_input_tokens: float
    avg_output_tokens: float
    usd_per_query: float
    usd_per_100q: float
    model: str
    rates_used: dict[str, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "avg_input_tokens": round(self.avg_input_tokens, 1),
            "avg_output_tokens": round(self.avg_output_tokens, 1),
            "usd_per_query": round(self.usd_per_query, 6),
            "usd_per_100q": round(self.usd_per_100q, 4),
            "model": self.model,
            "rates_used": dict(self.rates_used),
        }


def _estimate_query_tokens(query: str) -> int:
    return max(1, len(query) // 4)


def _compute_cost(
    *,
    brief_results: list[BriefResult],
    pair_index: dict[str, dict[str, str]],
    model: str,
) -> CostBreakdown:
    rates = PRICING_USD_PER_M[model]
    input_per_pair = []
    for r in brief_results:
        q_tokens = _estimate_query_tokens(pair_index[r.pair_id]["query"])
        input_per_pair.append(
            r.brief_tokens_estimate + FIXED_SYSTEM_PROMPT_TOKENS + q_tokens
        )
    avg_in = statistics.fmean(input_per_pair)
    avg_out = float(ASSUMED_RESPONSE_TOKENS)
    usd_per_q = (avg_in / 1_000_000) * rates["input"] + (
        avg_out / 1_000_000
    ) * rates["output"]
    return CostBreakdown(
        avg_input_tokens=avg_in,
        avg_output_tokens=avg_out,
        usd_per_query=usd_per_q,
        usd_per_100q=usd_per_q * 100,
        model=model,
        rates_used=dict(rates),
    )


def _run_strategy(
    *,
    strategy_name: str,
    pairs: Iterable[BenchmarkPair],
    token_budget: int,
) -> list[BriefResult]:
    fn = _STRATEGIES[strategy_name]
    effective_budget = get_strategy_budget(strategy_name, token_budget)
    return [
        _run_one(
            pair=p,
            strategy_name=strategy_name,
            fn=fn,
            token_budget=effective_budget,
        )
        for p in pairs
    ]


def _judge_subset(
    *,
    brief_results: list[BriefResult],
    pair_index: dict[str, dict[str, str]],
    fact_pair_ids: frozenset[str],
    client_to_use: object,
    use_mock: bool,
) -> list[JudgeResult]:
    """Real-LLM (or proxy) judge over the fact-bearing slice only."""
    out: list[JudgeResult] = []
    for r in brief_results:
        if r.pair_id not in fact_pair_ids:
            continue
        query = pair_index[r.pair_id]["query"]
        gt = pair_index[r.pair_id]["ground_truth"]
        if use_mock:
            score, qr, gtr, notes = _score_one_mock(
                brief_text=r.brief_text, query=query, ground_truth=gt
            )
            mode = "mock_proxy"
        else:
            score, qr, gtr, notes = _score_one_llm(
                brief_text=r.brief_text,
                query=query,
                ground_truth=gt,
                client=client_to_use,  # type: ignore[arg-type]
            )
            mode = "llm"
        out.append(
            JudgeResult(
                pair_id=r.pair_id,
                strategy=r.strategy,
                bucket=r.bucket,
                quality_score=score,
                query_recall=qr,
                ground_truth_recall=gtr,
                judge_mode=mode,
                notes=notes,
            )
        )
    return out


def _summarize_quality(judged: list[JudgeResult]) -> dict[str, object]:
    by_strategy: dict[str, list[JudgeResult]] = {}
    for j in judged:
        by_strategy.setdefault(j.strategy, []).append(j)
    out: dict[str, object] = {}
    for s, js in by_strategy.items():
        scores = [j.quality_score for j in js]
        out[s] = {
            "n_judged": len(js),
            "mean_quality": round(statistics.fmean(scores), 3),
            "median_quality": float(statistics.median(scores)),
            "score_histogram": {
                k: sum(1 for x in scores if x == k) for k in range(1, 6)
            },
        }
    return out


def _summarize_strategy(
    *,
    brief_results: list[BriefResult],
    cost: CostBreakdown,
    quality_summary: dict[str, object] | None,
    pair_index: dict[str, dict[str, str]],
    model: str,
) -> dict[str, object]:
    latencies = sorted(r.retrieval_latency_ms for r in brief_results)
    n = len(latencies)
    summary: dict[str, object] = {
        "n_pairs": n,
        "effective_token_budget": brief_results[0].token_budget,
        "avg_brief_tokens": round(
            statistics.fmean(r.brief_tokens_estimate for r in brief_results), 1
        ),
        "retrieval_latency_ms_p50": round(latencies[n // 2], 4),
        "retrieval_latency_ms_p95": round(
            latencies[max(0, int(n * 0.95) - 1)], 4
        ),
        "cost": cost.to_dict(),
    }
    # Per-bucket rollup so the report can show where the gain compounds —
    # short pairs barely move the needle, very_long pairs drive most of
    # the dollar savings.
    by_bucket: dict[str, dict[str, float]] = {}
    bucket_groups: dict[str, list[BriefResult]] = {}
    for r in brief_results:
        bucket_groups.setdefault(r.bucket, []).append(r)
    for bkt, brs in bucket_groups.items():
        bkt_cost = _compute_cost(
            brief_results=brs, pair_index=pair_index, model=model
        )
        bkt_lats = sorted(r.retrieval_latency_ms for r in brs)
        bn = len(bkt_lats)
        by_bucket[bkt] = {
            "n_pairs": bn,
            "avg_brief_tokens": round(
                statistics.fmean(r.brief_tokens_estimate for r in brs), 1
            ),
            "retrieval_latency_ms_p50": round(bkt_lats[bn // 2], 4),
            "usd_per_100q": round(bkt_cost.usd_per_100q, 4),
        }
    summary["by_bucket"] = by_bucket
    if quality_summary is not None:
        summary["quality_on_fact_subset"] = quality_summary
    return summary


def _per_pair_winners(
    *,
    judged_by_pair: dict[str, dict[str, JudgeResult]],
) -> dict[str, dict[str, int]]:
    """Hybrid-vs-naive per-pair tally on the judged subset.

    Returns ``{"hybrid_beats_naive": N, "ties": N, "naive_beats_hybrid": N}``.
    """
    h_wins = ties = n_wins = 0
    for pid, by_s in judged_by_pair.items():
        if "naive_dump" not in by_s or "hybrid" not in by_s:
            continue
        h = by_s["hybrid"].quality_score
        n = by_s["naive_dump"].quality_score
        if h > n:
            h_wins += 1
        elif h < n:
            n_wins += 1
        else:
            ties += 1
    return {
        "hybrid_beats_naive": h_wins,
        "ties": ties,
        "naive_beats_hybrid": n_wins,
    }


def _resolve_model_label(*, cost_model_override: str | None) -> str:
    """Identify the cost-model row to use.

    Order:
    1. ``--cost-model`` CLI override (always wins — lets the Day-27 report
       project Sonnet cost even when the live client is Mock).
    2. Live client's ``.model`` attribute if it matches a documented rate.
    3. Default to ``claude-sonnet-4-6`` (the SKILL's documented system
       model) so the projected cost row is always populated.

    The choice is recorded in the JSON header so the assumption is
    auditable from the artifact alone.
    """
    if cost_model_override and cost_model_override in PRICING_USD_PER_M:
        return cost_model_override
    client = get_client()
    if not isinstance(client, MockClient):
        model_attr = getattr(client, "model", None)
        if model_attr in PRICING_USD_PER_M:
            return str(model_attr)
    return "claude-sonnet-4-6"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--token-budget",
        type=int,
        default=8000,
        help="Runner default budget; per-strategy overrides still apply.",
    )
    parser.add_argument(
        "--judge-subset-cap",
        type=int,
        default=50,
        help=(
            "Maximum fact-bearing pairs to run real-LLM judge over. "
            "Smaller values save API spend; default 50 = the full fact slice."
        ),
    )
    parser.add_argument(
        "--force-mock-judge",
        action="store_true",
        help="Use the deterministic mock-proxy judge even if a real LLM is wired.",
    )
    parser.add_argument(
        "--cost-model",
        type=str,
        default="claude-sonnet-4-6",
        help=(
            "Pricing row to project cost against. Defaults to "
            "claude-sonnet-4-6 so cost numbers reflect the SKILL's "
            "documented system model even when the live client is Mock."
        ),
    )
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    args = parser.parse_args()

    all_pairs = load_pairs()
    pair_index = build_pair_index()
    fact_pair_ids = frozenset(
        p.pair_id for p in all_pairs if p.intent in FACT_BEARING_INTENTS
    )
    # Cap respects intent order — the dataset is built so intents cycle
    # through the buckets; capping by sort-order keeps the bucket mix.
    fact_pair_ids = frozenset(sorted(fact_pair_ids)[: args.judge_subset_cap])

    model_label = _resolve_model_label(cost_model_override=args.cost_model)
    judge_client = get_client()
    use_mock_judge = args.force_mock_judge or isinstance(judge_client, MockClient)
    if use_mock_judge:
        # Auth-failure resilience: if the live key is invalid, hybrid's
        # cold-tail summarisation would 401 mid-run. Forcing MOCK_LLM via
        # env (re-importing the dispatch layer isn't worth it — the
        # strategies read the env at call-time via get_client) keeps the
        # hybrid retrieval deterministic + offline.
        import os as _os

        _os.environ["MOCK_LLM"] = "true"

    t_total_start = time.perf_counter()
    by_strategy_results: dict[str, list[BriefResult]] = {}
    by_strategy_cost: dict[str, CostBreakdown] = {}
    judged: list[JudgeResult] = []

    for sname in STRATEGIES_IN_SCOPE:
        print(f"[{sname}] running over {len(all_pairs)} pairs ...")
        brief_results = _run_strategy(
            strategy_name=sname,
            pairs=all_pairs,
            token_budget=args.token_budget,
        )
        cost = _compute_cost(
            brief_results=brief_results,
            pair_index=pair_index,
            model=model_label,
        )
        by_strategy_results[sname] = brief_results
        by_strategy_cost[sname] = cost
        if sname in {"naive_dump", "hybrid"}:
            t0 = time.perf_counter()
            print(
                f"[{sname}] judging {len(fact_pair_ids)} fact-bearing pairs "
                f"(mode={'mock_proxy' if use_mock_judge else 'llm'}) ..."
            )
            judged.extend(
                _judge_subset(
                    brief_results=brief_results,
                    pair_index=pair_index,
                    fact_pair_ids=fact_pair_ids,
                    client_to_use=judge_client,
                    use_mock=use_mock_judge,
                )
            )
            print(
                f"[{sname}] judging finished in "
                f"{(time.perf_counter() - t0):.1f}s"
            )

    judged_by_pair: dict[str, dict[str, JudgeResult]] = {}
    for j in judged:
        judged_by_pair.setdefault(j.pair_id, {})[j.strategy] = j

    quality_summary = _summarize_quality(judged)
    quality_by_strategy: dict[str, dict[str, object]] = {}
    for sname in {"naive_dump", "hybrid"}:
        q = quality_summary.get(sname)
        if q is not None:
            quality_by_strategy[sname] = q  # type: ignore[assignment]

    per_strategy_summary = {
        sname: _summarize_strategy(
            brief_results=by_strategy_results[sname],
            cost=by_strategy_cost[sname],
            quality_summary=quality_by_strategy.get(sname),
            pair_index=pair_index,
            model=model_label,
        )
        for sname in STRATEGIES_IN_SCOPE
    }

    # Headline ratios — cost saved and quality preserved vs naive.
    n_cost = by_strategy_cost["naive_dump"].usd_per_100q
    h_cost = by_strategy_cost["hybrid"].usd_per_100q
    r_cost = by_strategy_cost["recency"].usd_per_100q
    n_q = quality_summary.get("naive_dump", {}).get("mean_quality")  # type: ignore[union-attr]
    h_q = quality_summary.get("hybrid", {}).get("mean_quality")  # type: ignore[union-attr]
    headline = {
        "champion_locked": "hybrid",
        "naive_usd_per_100q": round(n_cost, 4),
        "hybrid_usd_per_100q": round(h_cost, 4),
        "recency_usd_per_100q": round(r_cost, 4),
        "hybrid_cost_reduction_pct_vs_naive": round(
            (1 - h_cost / n_cost) * 100, 1
        )
        if n_cost
        else None,
        "hybrid_latency_speedup_x_vs_naive": round(
            per_strategy_summary["naive_dump"]["retrieval_latency_ms_p50"]
            / max(per_strategy_summary["hybrid"]["retrieval_latency_ms_p50"], 1e-6),
            2,
        ),
        "hybrid_quality_delta_vs_naive": (
            round(h_q - n_q, 3) if (h_q is not None and n_q is not None) else None
        ),
        "judge_mode": "mock_proxy" if use_mock_judge else "llm",
        "judge_model": model_label if not use_mock_judge else "mock",
        "n_judged_per_strategy": len(fact_pair_ids),
    }

    payload = {
        "day": 27,
        "phase": 5,
        "title": "Naive baseline vs hybrid champion — context-engine",
        "model_label": model_label,
        "pricing_used_usd_per_M": PRICING_USD_PER_M[model_label],
        "fixed_system_prompt_tokens": FIXED_SYSTEM_PROMPT_TOKENS,
        "assumed_response_tokens": ASSUMED_RESPONSE_TOKENS,
        "judge_mode": "mock_proxy" if use_mock_judge else "llm",
        "judge_model": model_label if not use_mock_judge else "mock",
        "n_pairs_total": len(all_pairs),
        "n_pairs_judged": len(fact_pair_ids),
        "fact_bearing_intents": sorted(FACT_BEARING_INTENTS),
        "strategies_in_scope": STRATEGIES_IN_SCOPE,
        "headline": headline,
        "per_strategy_summary": per_strategy_summary,
        "per_pair_quality_tally": _per_pair_winners(
            judged_by_pair=judged_by_pair
        ),
        "judge_subset_pair_ids": sorted(fact_pair_ids),
        "total_runtime_s": round(time.perf_counter() - t_total_start, 2),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
    print("\n=== HEADLINE ===")
    print(json.dumps(headline, indent=2))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
