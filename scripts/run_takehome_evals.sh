#!/usr/bin/env bash
# PennyCore — convenience runner for both takehome evaluators.
#
# Listed in the SKILL folder structure as a Day-1 deliverable; landing
# on Day 7 alongside the first adapter so it has something to run.
#
# Behavior:
#   * Runs `python evaluate.py` inside `takehome/context-engine/` if the
#     adapter module (`memory_system.py`) exists. Otherwise reports
#     "adapter not yet shipped" and continues.
#   * Same for `takehome/orchestrator/` (adapter lands Day 10 — until
#     then this section reports the not-yet-shipped status without
#     exiting non-zero).
#   * Aggregates pass/fail counts into a single end-of-run summary and
#     exits 0 only if every present adapter produced a passing
#     evaluator run (`exit 0` from `evaluate.py`, which honors the
#     evaluator's own "passed >= total - 1" rule for LLM-skipped
#     scenarios).
#
# Usage:  bash scripts/run_takehome_evals.sh
# CI use: scripts/ci.sh does NOT call this — the takehome evaluators
#         live behind their own deliberate run because scenario-6
#         depends on a live OPENAI_API_KEY which CI shouldn't need.

set -uo pipefail

cd "$(dirname "$0")/.."
repo_root="$(pwd)"

PYTHON="${PYTHON:-python}"

passes=0
fails=0
skipped=0

run_evaluator() {
    local name="$1"
    local dir="$2"
    local adapter="$3"

    echo ""
    echo "================================================================"
    echo "  ${name}"
    echo "================================================================"

    if [[ ! -f "${dir}/evaluate.py" ]]; then
        echo "  evaluate.py missing at ${dir}/evaluate.py — skipping"
        skipped=$((skipped + 1))
        return 0
    fi

    if [[ ! -f "${dir}/${adapter}" ]]; then
        echo "  adapter ${adapter} not yet shipped (lands per the SKILL"
        echo "  Day-by-Day plan). Skipping without failing the run."
        skipped=$((skipped + 1))
        return 0
    fi

    # Evaluators expect to be run from inside their own directory so the
    # `importlib.import_module("<adapter>")` call resolves correctly.
    (
        cd "${dir}"
        "${PYTHON}" evaluate.py
    )
    local rc=$?
    if [[ ${rc} -eq 0 ]]; then
        passes=$((passes + 1))
    else
        fails=$((fails + 1))
    fi
    return 0
}

run_evaluator "context-engine evaluator" "takehome/context-engine" "memory_system.py"
run_evaluator "orchestrator evaluator"  "takehome/orchestrator"   "orchestrator_impl.py"

echo ""
echo "----------------------------------------------------------------"
printf "  Summary: %d passed, %d failed, %d skipped (adapter not shipped yet)\n" \
    "${passes}" "${fails}" "${skipped}"
echo "----------------------------------------------------------------"

if [[ ${fails} -gt 0 ]]; then
    exit 1
fi
exit 0
