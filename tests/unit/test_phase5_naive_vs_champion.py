"""Shape + headline tests for the Day-27 naive-vs-champion benchmark.

The Day-27 harness (`benchmarks.phase5_naive_vs_champion`) is the
Phase-5 re-run that locks in the context-engine champion against the
naive everything-in-the-prompt baseline with realistic USD cost. These
tests assert the artifact shape the Day-27 report depends on and the
headline directional finding (hybrid is cheaper than naive, especially
on the long-history slice).

The tests run the harness in-process at small scale via the public
helper functions — they don't shell out to the CLI and they don't
write to ``results/`` so they're CI-safe.
"""

from __future__ import annotations

import os

import pytest

from benchmarks import phase5_naive_vs_champion as bench
from benchmarks.dataset_loader import load_pairs
from benchmarks.judge import build_pair_index


@pytest.fixture(autouse=True)
def _force_mock_llm():
    """Force the hybrid retrieval's cold-tail summariser to MockClient.

    The bench runs end-to-end through ``context_engine.llm.get_client``;
    on this machine the live ``.env`` key is invalid, so without forcing
    mock mode any test that touches hybrid retrieval would 401. Set the
    env after the bench imports (its ``load_dotenv(override=True)`` runs
    at import time, this fixture runs per-test).
    """
    prior = os.environ.get("MOCK_LLM")
    os.environ["MOCK_LLM"] = "true"
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("MOCK_LLM", None)
        else:
            os.environ["MOCK_LLM"] = prior


def test_pricing_table_documents_sonnet_and_haiku() -> None:
    # Documented in module docstring §Cost model — both rows are
    # required for the report's projected-cost numbers.
    assert "claude-sonnet-4-6" in bench.PRICING_USD_PER_M
    assert "claude-haiku-4-5-20251001" in bench.PRICING_USD_PER_M
    sonnet = bench.PRICING_USD_PER_M["claude-sonnet-4-6"]
    assert sonnet["input"] > 0 and sonnet["output"] > sonnet["input"]


def test_fact_bearing_intents_match_dataset() -> None:
    """The fact-bearing subset must actually exist in the dataset.

    The Day-27 judge slice is exactly the mortgage-domain intents.
    If the dataset builder ever drops one, the headline number's
    sample size silently shrinks — this assertion catches that.
    """
    pairs = load_pairs()
    intents_in_data = {p.intent for p in pairs}
    missing = bench.FACT_BEARING_INTENTS - intents_in_data
    assert not missing, f"FACT_BEARING_INTENTS not in dataset: {missing}"


def test_compute_cost_increases_with_brief_tokens() -> None:
    """USD cost rises monotonically with brief token count.

    Sanity check on the cost model: a longer brief at the same model
    rate must cost more, otherwise the comparison is broken before
    you even look at the numbers.
    """
    pair_index = build_pair_index()
    # Use whatever pair_ids exist — pick the first two by sort order.
    pids = sorted(pair_index)[:2]
    short_briefs = [
        bench.BriefResult(
            pair_id=pids[0],
            strategy="x",
            bucket="short",
            history_message_count=5,
            token_budget=8000,
            brief_tokens_estimate=200,
            brief_text="",
            segment_source_counts={},
            retrieval_latency_ms=1.0,
            notes="",
        )
    ]
    long_briefs = [
        bench.BriefResult(
            pair_id=pids[1],
            strategy="x",
            bucket="long",
            history_message_count=200,
            token_budget=50000,
            brief_tokens_estimate=8000,
            brief_text="",
            segment_source_counts={},
            retrieval_latency_ms=1.0,
            notes="",
        )
    ]
    c_short = bench._compute_cost(
        brief_results=short_briefs,
        pair_index=pair_index,
        model="claude-sonnet-4-6",
    )
    c_long = bench._compute_cost(
        brief_results=long_briefs,
        pair_index=pair_index,
        model="claude-sonnet-4-6",
    )
    assert c_long.usd_per_query > c_short.usd_per_query


def test_resolve_model_label_honors_cli_override() -> None:
    # Override always wins (Day-27 needs this to project Sonnet cost
    # even when the live client is Mock).
    label = bench._resolve_model_label(cost_model_override="claude-haiku-4-5-20251001")
    assert label == "claude-haiku-4-5-20251001"
    # Unknown override falls back to the default Sonnet row, not raises.
    label_default = bench._resolve_model_label(cost_model_override="bogus-model")
    assert label_default == "claude-sonnet-4-6"


def test_strategies_in_scope_are_registered() -> None:
    # If any of the three named strategies isn't registered, the harness
    # would KeyError mid-run with no good error.
    import benchmarks.strategies  # noqa: F401 — registration side-effect
    from benchmarks.registry import _STRATEGIES

    for sname in bench.STRATEGIES_IN_SCOPE:
        assert sname in _STRATEGIES, f"strategy not registered: {sname}"


def test_run_strategy_returns_one_result_per_pair() -> None:
    pairs = load_pairs()[:10]
    res = bench._run_strategy(
        strategy_name="hybrid", pairs=pairs, token_budget=8000
    )
    assert len(res) == 10
    assert all(r.strategy == "hybrid" for r in res)


def test_per_pair_winners_tally_shape() -> None:
    # Build a tiny fake judged-by-pair dict and assert the tally adds up.
    from benchmarks.judge import JudgeResult

    judged_by_pair: dict[str, dict[str, JudgeResult]] = {
        "p0": {
            "hybrid": JudgeResult(
                "p0", "hybrid", "short", 4, -1.0, -1.0, "llm", ""
            ),
            "naive_dump": JudgeResult(
                "p0", "naive_dump", "short", 3, -1.0, -1.0, "llm", ""
            ),
        },
        "p1": {
            "hybrid": JudgeResult(
                "p1", "hybrid", "long", 2, -1.0, -1.0, "llm", ""
            ),
            "naive_dump": JudgeResult(
                "p1", "naive_dump", "long", 4, -1.0, -1.0, "llm", ""
            ),
        },
        "p2": {
            "hybrid": JudgeResult(
                "p2", "hybrid", "medium", 3, -1.0, -1.0, "llm", ""
            ),
            "naive_dump": JudgeResult(
                "p2", "naive_dump", "medium", 3, -1.0, -1.0, "llm", ""
            ),
        },
    }
    tally = bench._per_pair_winners(judged_by_pair=judged_by_pair)
    assert tally == {
        "hybrid_beats_naive": 1,
        "ties": 1,
        "naive_beats_hybrid": 1,
    }


def test_hybrid_cheaper_than_naive_on_very_long_bucket() -> None:
    """The Day-27 headline: hybrid >= 2x cheaper than naive on very_long.

    This is the cost-frontier story. If this regresses, hybrid is no
    longer the champion against the naive baseline and the Phase-5
    wrap-up needs to be revisited.
    """
    pairs = load_pairs()
    pair_index = build_pair_index()
    very_long_pairs = [p for p in pairs if p.history_bucket == "very_long"]
    assert very_long_pairs, "dataset has no very_long pairs — rebuild it"

    naive_res = bench._run_strategy(
        strategy_name="naive_dump", pairs=very_long_pairs, token_budget=8000
    )
    hybrid_res = bench._run_strategy(
        strategy_name="hybrid", pairs=very_long_pairs, token_budget=8000
    )
    naive_cost = bench._compute_cost(
        brief_results=naive_res,
        pair_index=pair_index,
        model="claude-sonnet-4-6",
    )
    hybrid_cost = bench._compute_cost(
        brief_results=hybrid_res,
        pair_index=pair_index,
        model="claude-sonnet-4-6",
    )
    assert hybrid_cost.usd_per_100q * 2 <= naive_cost.usd_per_100q, (
        f"hybrid ${hybrid_cost.usd_per_100q:.4f} should be >=2x cheaper "
        f"than naive ${naive_cost.usd_per_100q:.4f} on very_long bucket"
    )
