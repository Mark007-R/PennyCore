#!/usr/bin/env bash
# PennyCore — local CI gauntlet. Run before every commit.
#
# Mirrors what a GitHub Actions runner would do if we were pushing public CI.
# Phase-1 scope: lint + (scoped) type-check + pytest. Strict mypy is on
# `contracts/` only; the FastAPI services land Day 5+ and join strict mode then.
#
# Usage:  bash scripts/ci.sh
# Exits non-zero on any failure. Set CI=1 to force non-interactive output.

set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python}"

echo "==> ruff (lint)"
$PYTHON -m ruff check .

echo "==> mypy (strict on contracts/, lenient elsewhere)"
$PYTHON -m mypy contracts

echo "==> pytest (unit + integration + adversarial)"
$PYTHON -m pytest -q \
    --cov=contracts \
    --cov=context_engine \
    --cov=orchestrator \
    --cov-report=term-missing \
    tests/

echo "==> CI passed"
