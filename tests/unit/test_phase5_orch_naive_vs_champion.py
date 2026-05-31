"""Shape + headline tests for the Day-28 orchestrator naive-vs-champion bench.

The Day-28 harness (`benchmarks.phase5_orch_naive_vs_champion`) is the
Phase-5 wrap-up re-run that locks in the orchestrator's declarative-YAML
champion against the naive everything-is-LLM baseline with realistic
production-payload USD cost and per-tenant failure shape.

These tests assert:

1. The two-sided cost projection (mock-payload + prod-payload) is
   monotone and sane.
2. The per-tenant failure-shape function correctly surfaces the
   over-approve / over-reject asymmetry naive_llm exhibits in mock mode.
3. The frontier helper sorts strategies by correctness descending and
   includes every strategy.
4. The headline directional finding holds: declarative is strictly more
   correct than naive_llm AND strictly cheaper at prod-payload rates.
"""

from __future__ import annotations

import os

import pytest

from benchmarks import phase5_orch_naive_vs_champion as bench
from benchmarks.orchestrator_bench import run
from benchmarks.orchestrator_dataset_loader import load_scenarios


@pytest.fixture(autouse=True)
def _force_mock_llm():
    prior = os.environ.get("MOCK_LLM")
    os.environ["MOCK_LLM"] = "true"
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("MOCK_LLM", None)
        else:
            os.environ["MOCK_LLM"] = prior


def test_pricing_table_documents_sonnet() -> None:
    assert "claude-sonnet-4-6" in bench.PRICING_USD_PER_M
    s = bench.PRICING_USD_PER_M["claude-sonnet-4-6"]
    assert s["input"] > 0 and s["output"] > s["input"]


def test_prod_payload_assumptions_are_realistic() -> None:
    """The projected production payload must dwarf the mock-mode one.

    If the prod-payload assumption ever drops to match the mock-mode
    payload, the comparison loses its point (mock-mode cost
    understates production by ~10x; that gap is the whole reason for
    the projection).
    """
    assert bench.PROD_INPUT_TOKENS_PER_CALL >= 1000
    assert bench.PROD_OUTPUT_TOKENS_PER_CALL >= 50


def test_project_cost_is_zero_for_zero_call_engine() -> None:
    scenarios = load_scenarios()[:30]
    _, summary = run(scenarios=scenarios)
    decl_cp = bench._project_cost(
        n_scenarios=len(scenarios),
        aggregate=summary["declarative"],
        cost_model="claude-sonnet-4-6",
    )
    assert decl_cp.n_calls == 0
    assert decl_cp.mock_payload_usd_per_100 == 0.0
    assert decl_cp.prod_payload_usd_per_100 == 0.0


def test_project_cost_grows_with_call_count() -> None:
    scenarios = load_scenarios()
    _, summary = run(scenarios=scenarios)
    cp_naive = bench._project_cost(
        n_scenarios=len(scenarios),
        aggregate=summary["naive_llm"],
        cost_model="claude-sonnet-4-6",
    )
    assert cp_naive.n_calls == len(scenarios)
    assert cp_naive.prod_payload_usd_per_100 > 0
    # Prod payload should be materially > mock payload (the bench's
    # raison d'etre).
    assert cp_naive.prod_payload_usd_per_100 > cp_naive.mock_payload_usd_per_100


def test_per_tenant_failure_shape_surfaces_naive_asymmetry() -> None:
    """Naive must fail on multiple tenants with non-uniform error mode.

    This is the load-bearing directional finding for the Day-28 wrap:
    naive_llm's tenant-agnostic heuristic CANNOT get all three tenants
    right because the tenants have *opposite* policies on the same
    event types. If this assertion ever passes vacuously (all tenants
    100%), the comparison story is dead.
    """
    scenarios = load_scenarios()
    per_scenario, _ = run(scenarios=scenarios)
    per_tenant = bench._per_tenant_failure_shape(
        rows=per_scenario, scenarios=scenarios, strategy="naive_llm"
    )
    # Multiple tenants present.
    assert len(per_tenant) >= 2
    # No tenant should be 100% (naive is bad somewhere on every tenant
    # in our policy table — the dataset is designed that way).
    for tenant, info in per_tenant.items():
        assert info["correctness"] < 1.0, (
            f"naive_llm 100% correct on {tenant}? — dataset has been "
            f"weakened or the heuristic accidentally learned the table"
        )
    # The top_confusion field is populated when there's at least one
    # failure (which we just asserted is the case for every tenant).
    for info in per_tenant.values():
        assert info["top_confusion"] is not None


def test_per_tenant_failure_shape_declarative_is_perfect() -> None:
    """The declarative champion gets every scenario right on every tenant."""
    scenarios = load_scenarios()
    per_scenario, _ = run(scenarios=scenarios)
    per_tenant = bench._per_tenant_failure_shape(
        rows=per_scenario, scenarios=scenarios, strategy="declarative"
    )
    for tenant, info in per_tenant.items():
        assert info["correctness"] == 1.0, (
            f"declarative champion missed on {tenant}: "
            f"{info['correct']}/{info['n_scenarios']}"
        )


def test_frontier_sorted_by_correctness_desc() -> None:
    scenarios = load_scenarios()
    _, summary = run(scenarios=scenarios)
    cps = {
        sname: bench._project_cost(
            n_scenarios=len(scenarios),
            aggregate=agg,
            cost_model="claude-sonnet-4-6",
        )
        for sname, agg in summary.items()
    }
    f = bench._frontier_table(summary=summary, cost_projections=cps)
    correctnesses = [r["correctness"] for r in f]
    assert correctnesses == sorted(correctnesses, reverse=True)
    strategies_in_frontier = {r["strategy"] for r in f}
    assert strategies_in_frontier == set(summary.keys())


def test_champion_strictly_dominates_naive_on_correctness_and_cost() -> None:
    """Headline Day-28 finding: declarative wins on BOTH axes vs naive."""
    scenarios = load_scenarios()
    _, summary = run(scenarios=scenarios)
    cps = {
        sname: bench._project_cost(
            n_scenarios=len(scenarios),
            aggregate=agg,
            cost_model="claude-sonnet-4-6",
        )
        for sname, agg in summary.items()
    }
    decl = summary["declarative"]
    naive = summary["naive_llm"]
    assert decl.correctness > naive.correctness
    assert decl.correctness == 1.0
    assert naive.correctness < 0.7
    assert (
        cps["declarative"].prod_payload_usd_per_100
        < cps["naive_llm"].prod_payload_usd_per_100
    )
    assert cps["declarative"].prod_payload_usd_per_100 == 0.0
