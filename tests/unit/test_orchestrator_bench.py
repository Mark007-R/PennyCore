"""Smoke + shape tests for the Day-17 orchestrator benchmark harness.

The harness in :mod:`benchmarks.orchestrator_bench` runs every policy
strategy across the 200-scenario dataset and produces a results
artifact. These tests:

  1. Run the harness on a small slice and assert the per-scenario +
     summary shapes.
  2. Assert the Day-17 headline expectations from
     ``results/EXPERIMENT_LOG.md`` hold:
       * declarative + python_rules tie at 100% correctness in
         mock mode (same policy table, same lookup);
       * naive_llm scores strictly below 1.0 in mock mode (the
         documented "ignores tenant policy" failure mode);
       * llm_judge mock mirrors declarative correctness.
  3. Assert auditability + maintainability rubric scores are
     attached and well-formed.
"""

from __future__ import annotations

from benchmarks.orchestrator_bench import run
from benchmarks.orchestrator_dataset_loader import load_scenarios


def test_harness_runs_on_small_slice() -> None:
    scenarios = load_scenarios()[:20]
    rows, summary = run(scenarios=scenarios)
    expected_strategies = {"declarative", "python_rules", "naive_llm", "llm_judge"}
    assert set(summary.keys()) == expected_strategies
    assert len(rows) == len(scenarios) * len(expected_strategies)
    # Every row carries the required correctness key.
    for r in rows:
        assert isinstance(r.decision_correct, bool)
        assert r.observed_decision in {"auto", "approval_required", "reject"}


def test_declarative_and_python_rules_tie_on_correctness() -> None:
    scenarios = load_scenarios()
    _, summary = run(scenarios=scenarios)
    # Same policy table, same lookup semantics → same correctness.
    assert summary["declarative"].correctness == summary["python_rules"].correctness


def test_naive_llm_underperforms_in_mock_mode() -> None:
    scenarios = load_scenarios()
    _, summary = run(scenarios=scenarios)
    naive = summary["naive_llm"].correctness
    declarative = summary["declarative"].correctness
    # The documented mock-mode failure mode: naive ignores tenant
    # policy variation. Tighten the assertion if the heuristic gets
    # smarter — the headline finding for Day 17 depends on it.
    assert naive < declarative
    assert naive < 0.8, f"naive correctness {naive} too high — heuristic too kind"


def test_llm_judge_mock_mirrors_declarative_correctness() -> None:
    scenarios = load_scenarios()
    _, summary = run(scenarios=scenarios)
    # In mock mode the LLM-as-judge engine reads the policy table
    # directly (emulating a perfectly-prompt-following LLM) so
    # correctness must equal the declarative engine's.
    assert summary["llm_judge"].correctness == summary["declarative"].correctness


def test_llm_calls_are_paid_only_by_llm_strategies() -> None:
    scenarios = load_scenarios()[:30]
    _, summary = run(scenarios=scenarios)
    assert summary["declarative"].total_llm_calls == 0
    assert summary["python_rules"].total_llm_calls == 0
    assert summary["naive_llm"].total_llm_calls == len(scenarios)
    assert summary["llm_judge"].total_llm_calls == len(scenarios)


def test_rubric_scores_attached() -> None:
    scenarios = load_scenarios()[:5]
    _, summary = run(scenarios=scenarios)
    for sname, agg in summary.items():
        assert 1 <= agg.auditability <= 5, sname
        assert 1 <= agg.maintainability <= 5, sname
        assert agg.auditability_rationale, sname
        assert agg.maintainability_rationale, sname


def test_confusion_matrix_shape() -> None:
    scenarios = load_scenarios()
    _, summary = run(scenarios=scenarios)
    # naive_llm should show off-diagonal entries (it gets things wrong).
    naive_confusion = summary["naive_llm"].confusion_matrix
    has_off_diagonal = any(
        observed != expected and count > 0
        for expected, row in naive_confusion.items()
        for observed, count in row.items()
    )
    assert has_off_diagonal


def test_cost_per_100_decisions_present_for_llm_strategies() -> None:
    scenarios = load_scenarios()[:20]
    _, summary = run(scenarios=scenarios)
    assert summary["declarative"].estimated_cost_per_100_decisions_usd == 0.0
    assert summary["python_rules"].estimated_cost_per_100_decisions_usd == 0.0
    assert summary["naive_llm"].estimated_cost_per_100_decisions_usd > 0.0
    assert summary["llm_judge"].estimated_cost_per_100_decisions_usd > 0.0
