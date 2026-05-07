# PennyCore — local CI gauntlet (PowerShell). Run before every commit.
#
# Mirrors `scripts/ci.sh` for Windows dev. Phase-1 scope: lint + (scoped)
# type-check + pytest. Strict mypy is on `contracts/` only; the FastAPI
# services land Day 5+ and join strict mode then.
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts/ci.ps1

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$PythonExe = if ($env:PYTHON) { $env:PYTHON } else { "python" }

Write-Host "==> ruff (lint)" -ForegroundColor Cyan
& $PythonExe -m ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> mypy (strict on contracts/, lenient elsewhere)" -ForegroundColor Cyan
& $PythonExe -m mypy contracts
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> pytest (unit + integration + adversarial)" -ForegroundColor Cyan
& $PythonExe -m pytest -q `
    --cov=contracts `
    --cov=context_engine `
    --cov=orchestrator `
    --cov-report=term-missing `
    tests/
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "==> CI passed" -ForegroundColor Green
