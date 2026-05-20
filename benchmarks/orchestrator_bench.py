"""Orchestrator policy-engine comparison harness — Phase 3 (Day 17).

Runs all four registered policy strategies (declarative YAML,
python_rules, naive LLM, LLM-as-judge) over the 200-scenario benchmark
dataset built on Day 16. Produces per-strategy aggregates on
correctness, decision latency, LLM-call cost, plus the static
auditability / maintainability rubric scores documented in
``results/EXPERIMENT_LOG.md``.

## What this harness measures

For each (scenario, strategy) pair:

* ``decision_correct`` — does the engine's PolicyDecision match the
  scenario's ``expected_decision``? This is the headline metric.
* ``decision_latency_us`` — wall-clock microseconds for the
  ``decide()`` call alone (no harness overhead). Each strategy runs
  in mock mode so the absolute numbers are tiny; the **ratios**
  between engines are what matter.
* ``llm_call_count`` — populated for naive / LLM-as-judge. Used to
  compute the per-100-decision cost estimate at Claude
  ``sonnet-4-6`` rack rates (input $3 / Mtok, output $15 / Mtok).

Per-strategy aggregates also include:

* Correctness rolled up per-tenant and per-event-type so the report
  can call out *which slice each engine wins on*.
* Confusion matrix on ``expected_decision`` so the failure mode of
  each engine is legible (does it under-approve? over-reject?).
* Rubric scores — Auditability / Maintainability — hardcoded with
  rationale strings; these don't vary per-scenario.

## Why the planner is NOT in the loop today

The Day-17 study is isolating the policy engine. We feed each engine
the scenario's ``expected_action_type`` directly, so noise from
planner misfires doesn't pollute the comparison. The planner's
quality gets its own study (Phase 5 / Day 28) using the same
dataset's ``expected_action_type`` ground-truth column.

## Output shape

Single JSON artifact at
``results/phase3_orchestrator_results.json`` containing:

  {
    "n_scenarios": 200,
    "strategies": ["declarative", "python_rules", "naive_llm",
                   "llm_judge"],
    "per_scenario": [ ... 200 * 4 = 800 rows ... ],
    "summary": { strategy_name: aggregate_dict, ... }
  }

The Day-15-style notebook on Day-18 reads this same JSON to render
charts; the Phase-3 wrap-up report calls
:func:`summarize_strategy` to produce the head-to-head table.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from benchmarks.orchestrator_dataset_loader import (
    OrchestratorScenario,
    load_scenarios,
    load_tenant_policies,
)
from context_engine.llm import get_client
from contracts.policies import PolicyDecision
from orchestrator.policy import (
    DeclarativePolicyEngine,
    LLMJudgePolicyEngine,
    NaivePolicyEngine,
    PythonRulesPolicyEngine,
)

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
DEFAULT_OUTPUT = RESULTS_DIR / "phase3_orchestrator_results.json"

# Claude sonnet-4-6 rack rates (USD per 1M tokens). Used to convert
# the engines' input/output token estimates to "cost per 100
# decisions" so the comparison table reads as money, not tokens. Real
# benchmark in Phase 5 / Day 28 re-runs against the live billing API
# to confirm the estimate.
_PRICE_PER_M_INPUT_TOKENS = 3.0
_PRICE_PER_M_OUTPUT_TOKENS = 15.0


# ---------------------------------------------------------------------------
# Per-strategy auditability / maintainability rubric.
#
# Scores are 1-5 with explicit rationale; locked here so the Day-18
# wrap-up can lift them verbatim into the report without re-justifying.
# ---------------------------------------------------------------------------

_RUBRIC: dict[str, dict[str, Any]] = {
    "declarative": {
        "auditability": 5,
        "auditability_rationale": (
            "Decision is a deterministic dict lookup with the full table "
            "inspectable via policies_for(tenant_id). Compliance can "
            "reconstruct WHY in O(1) — no LLM in path."
        ),
        "maintainability": 5,
        "maintainability_rationale": (
            "Compliance team edits a dict / YAML and ships. No code "
            "review needed; no engineering touch required for routine "
            "policy updates."
        ),
    },
    "python_rules": {
        "auditability": 3,
        "auditability_rationale": (
            "Per-tenant Python callable is opaque to introspection — "
            "reasoning depends on which conditional branch fired. "
            "Decision trace requires reading code, not configuration."
        ),
        "maintainability": 2,
        "maintainability_rationale": (
            "Policy updates require an engineering touch and code "
            "review. Compliance team cannot ship a policy change on "
            "their own. Higher expressiveness ceiling, higher floor."
        ),
    },
    "naive_llm": {
        "auditability": 2,
        "auditability_rationale": (
            "Free-form LLM text reasoning is hard to align to a "
            "structured audit schema. Compliance must read prose "
            "per-decision; aggregate audits across decisions are "
            "impractical."
        ),
        "maintainability": 4,
        "maintainability_rationale": (
            "Compliance edits an English narrative — accessible to "
            "non-engineers, but the LLM may misinterpret edits. "
            "Validation requires running the benchmark, not just "
            "reading the policy."
        ),
    },
    "llm_judge": {
        "auditability": 4,
        "auditability_rationale": (
            "Structured JSON output gives a parseable decision + "
            "reasoning field. Compliance can audit at scale (queryable "
            "schema) but the underlying LLM remains a black box."
        ),
        "maintainability": 4,
        "maintainability_rationale": (
            "Compliance updates the JSON policy table directly (same "
            "shape as declarative). The judge prompt is engineering-"
            "owned and rarely changes; policy table edits are "
            "low-touch."
        ),
    },
}


@dataclass
class ScenarioResult:
    """One (scenario, strategy) result row."""

    scenario_id: str
    strategy: str
    tenant_id: str
    event_type: str
    expected_action_type: str
    expected_decision: str
    observed_decision: str
    decision_correct: bool
    decision_latency_us: float
    llm_call_count_delta: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class StrategyAggregate:
    """Per-strategy rolled-up metrics."""

    strategy: str
    n_scenarios: int
    correctness: float
    decision_latency_us_p50: float
    decision_latency_us_p95: float
    decision_latency_us_mean: float
    total_llm_calls: int
    estimated_cost_per_100_decisions_usd: float
    per_tenant_correctness: dict[str, float]
    per_event_type_correctness: dict[str, float]
    per_expected_decision_correctness: dict[str, float]
    confusion_matrix: dict[str, dict[str, int]]
    auditability: int
    auditability_rationale: str
    maintainability: int
    maintainability_rationale: str
    failed_scenarios: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        out = asdict(self)
        # Keep failure list bounded — the full per-scenario table is
        # in ``per_scenario`` already; this is just a top-25 preview.
        out["failed_scenarios"] = list(self.failed_scenarios[:25])
        out["failed_scenarios_total"] = len(self.failed_scenarios)
        return out


# ---------------------------------------------------------------------------
# Strategy factory — keeps the harness independent of engine constructors.
# ---------------------------------------------------------------------------


def _build_engines(client_factory=get_client) -> dict[str, Any]:
    """Instantiate every engine fresh per run.

    Each LLM-backed engine shares a single client instance so the
    mock client's token counters are coherent across calls. The
    factory pattern keeps the harness testable — tests inject a
    deterministic stub client without monkeypatching.
    """
    client = client_factory()
    return {
        "declarative": DeclarativePolicyEngine(),
        "python_rules": PythonRulesPolicyEngine(),
        "naive_llm": NaivePolicyEngine(client=client),
        "llm_judge": LLMJudgePolicyEngine(client=client),
    }


def _configure_engines(
    engines: dict[str, Any], policies: dict[str, dict[str, str]]
) -> None:
    for tenant_id, table in policies.items():
        for engine in engines.values():
            engine.set_policies(tenant_id, table)


# ---------------------------------------------------------------------------
# Per-scenario execution
# ---------------------------------------------------------------------------


def _run_one(
    *,
    scenario: OrchestratorScenario,
    strategy_name: str,
    engine: Any,
) -> ScenarioResult:
    """Run one engine on one scenario; record decision + latency.

    LLM-call delta is computed before/after for engines that track
    ``llm_call_count``; the declarative + python_rules engines never
    touch the LLM and report a delta of 0.
    """
    prior_llm_calls = getattr(engine, "llm_call_count", 0)
    t0 = time.perf_counter_ns()
    decision: PolicyDecision = engine.decide(
        tenant_id=scenario.tenant_id,
        action_type=scenario.expected_action_type,
        event_context=scenario.event,
    )
    elapsed_us = (time.perf_counter_ns() - t0) / 1000.0
    new_llm_calls = getattr(engine, "llm_call_count", 0)

    return ScenarioResult(
        scenario_id=scenario.scenario_id,
        strategy=strategy_name,
        tenant_id=scenario.tenant_id,
        event_type=scenario.event["event_type"],
        expected_action_type=scenario.expected_action_type,
        expected_decision=scenario.expected_decision,
        observed_decision=decision.value,
        decision_correct=(decision.value == scenario.expected_decision),
        decision_latency_us=round(elapsed_us, 3),
        llm_call_count_delta=new_llm_calls - prior_llm_calls,
    )


def run(
    *,
    scenarios: list[OrchestratorScenario] | None = None,
    strategies: list[str] | None = None,
    client_factory=get_client,
) -> tuple[list[ScenarioResult], dict[str, StrategyAggregate]]:
    """Execute every selected strategy over every scenario.

    Returns (per_scenario_rows, per_strategy_aggregates). The harness
    serialises both; tests assert against the in-memory shapes.
    """
    scenarios = scenarios or load_scenarios()
    policies = load_tenant_policies()
    engines = _build_engines(client_factory=client_factory)

    # Reset metrics on LLM-backed engines so successive ``run()`` calls
    # in a single process (notebooks, tests) start with clean
    # counters.
    for engine in engines.values():
        if hasattr(engine, "reset_metrics"):
            engine.reset_metrics()

    _configure_engines(engines, policies)

    if strategies is None:
        strategy_names = list(engines.keys())
    else:
        unknown = set(strategies) - set(engines.keys())
        if unknown:
            raise KeyError(f"Unknown strategies: {sorted(unknown)}")
        strategy_names = list(strategies)

    per_scenario: list[ScenarioResult] = []
    for sname in strategy_names:
        engine = engines[sname]
        for scenario in scenarios:
            per_scenario.append(
                _run_one(
                    scenario=scenario, strategy_name=sname, engine=engine
                )
            )

    summary: dict[str, StrategyAggregate] = {}
    for sname in strategy_names:
        engine = engines[sname]
        rows = [r for r in per_scenario if r.strategy == sname]
        summary[sname] = summarize_strategy(sname, engine, rows)

    return per_scenario, summary


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def summarize_strategy(
    strategy_name: str,
    engine: Any,
    rows: list[ScenarioResult],
) -> StrategyAggregate:
    """Roll up per-strategy correctness + latency + cost + rubric."""
    n = len(rows)
    if n == 0:
        raise ValueError(f"no rows for strategy {strategy_name!r}")

    correct = sum(1 for r in rows if r.decision_correct)
    latencies = sorted(r.decision_latency_us for r in rows)
    p50 = latencies[n // 2]
    p95 = latencies[max(0, int(n * 0.95) - 1)]
    mean = statistics.fmean(latencies)

    total_llm_calls = getattr(engine, "llm_call_count", 0)
    in_tokens = getattr(engine, "llm_input_tokens_estimate", 0)
    out_tokens = getattr(engine, "llm_output_tokens_estimate", 0)
    cost_usd = (
        in_tokens * _PRICE_PER_M_INPUT_TOKENS / 1_000_000.0
        + out_tokens * _PRICE_PER_M_OUTPUT_TOKENS / 1_000_000.0
    )
    cost_per_100 = (cost_usd / n) * 100 if n else 0.0

    per_tenant = _rollup_correctness(rows, lambda r: r.tenant_id)
    per_event_type = _rollup_correctness(rows, lambda r: r.event_type)
    per_expected_decision = _rollup_correctness(
        rows, lambda r: r.expected_decision
    )

    confusion: dict[str, dict[str, int]] = {}
    for r in rows:
        confusion.setdefault(r.expected_decision, Counter())[
            r.observed_decision
        ] += 1
    confusion_serializable = {
        k: dict(v) for k, v in confusion.items()
    }

    failed = [r.scenario_id for r in rows if not r.decision_correct]

    rubric = _RUBRIC[strategy_name]

    return StrategyAggregate(
        strategy=strategy_name,
        n_scenarios=n,
        correctness=round(correct / n, 4),
        decision_latency_us_p50=round(p50, 3),
        decision_latency_us_p95=round(p95, 3),
        decision_latency_us_mean=round(mean, 3),
        total_llm_calls=total_llm_calls,
        estimated_cost_per_100_decisions_usd=round(cost_per_100, 6),
        per_tenant_correctness=per_tenant,
        per_event_type_correctness=per_event_type,
        per_expected_decision_correctness=per_expected_decision,
        confusion_matrix=confusion_serializable,
        auditability=rubric["auditability"],
        auditability_rationale=rubric["auditability_rationale"],
        maintainability=rubric["maintainability"],
        maintainability_rationale=rubric["maintainability_rationale"],
        failed_scenarios=failed,
    )


def _rollup_correctness(
    rows: list[ScenarioResult], key_fn
) -> dict[str, float]:
    """Group rows by key and return ``{group: correctness_fraction}``."""
    groups: dict[str, list[ScenarioResult]] = {}
    for r in rows:
        groups.setdefault(key_fn(r), []).append(r)
    out: dict[str, float] = {}
    for k, grp in groups.items():
        out[k] = round(
            sum(1 for r in grp if r.decision_correct) / len(grp), 4
        )
    return out


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Output JSON path.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Run only the first 10 scenarios across all strategies.",
    )
    parser.add_argument(
        "--strategies",
        type=str,
        default="",
        help=(
            "Comma-separated subset, e.g. 'declarative,naive_llm'. "
            "Empty = all four."
        ),
    )
    args = parser.parse_args()

    scenarios = load_scenarios()
    if args.smoke:
        scenarios = scenarios[:10]

    strategies = (
        [s for s in args.strategies.split(",") if s]
        if args.strategies
        else None
    )

    rows, summary = run(scenarios=scenarios, strategies=strategies)

    payload: dict[str, object] = {
        "n_scenarios": len(scenarios),
        "strategies": list(summary.keys()),
        "per_scenario": [r.to_dict() for r in rows],
        "summary": {k: v.to_dict() for k, v in summary.items()},
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )

    # Pretty-print the head-to-head table on stdout — same shape the
    # report will lift.
    headers = [
        "strategy", "correct", "p50_us", "p95_us", "llm_calls",
        "$/100", "audit", "maint",
    ]
    print("  ".join(f"{h:>14}" for h in headers))
    for sname, agg in summary.items():
        print(
            "  ".join(
                f"{x:>14}"
                for x in [
                    sname,
                    f"{agg.correctness:.3f}",
                    f"{agg.decision_latency_us_p50:.2f}",
                    f"{agg.decision_latency_us_p95:.2f}",
                    str(agg.total_llm_calls),
                    f"${agg.estimated_cost_per_100_decisions_usd:.5f}",
                    str(agg.auditability),
                    str(agg.maintainability),
                ]
            )
        )
    print(f"\nWrote {len(rows)} rows to {args.out}")


if __name__ == "__main__":
    main()
