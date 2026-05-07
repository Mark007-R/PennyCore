#!/usr/bin/env bash
# PennyCore — boot the full local stack via docker compose.
#
# Equivalent to:  docker compose up --build
# Convenience wrapper that also tails logs and prints the service URLs once
# both APIs are healthy.
#
# Usage:  bash scripts/run_local.sh           # foreground, ctrl-C to stop
#         bash scripts/run_local.sh --detach  # background

set -euo pipefail

cd "$(dirname "$0")/.."

DETACH=""
if [[ "${1:-}" == "--detach" || "${1:-}" == "-d" ]]; then
    DETACH="--detach"
fi

echo "==> Building images and starting Postgres + Redis + context-engine + orchestrator"
docker compose up --build $DETACH

if [[ -n "$DETACH" ]]; then
    echo
    echo "Stack is up:"
    echo "  context-engine -> http://localhost:8001"
    echo "  orchestrator   -> http://localhost:8002"
    echo "  postgres       -> localhost:5432 (db: pennycore, user: pennycore)"
    echo "  redis          -> localhost:6379"
    echo
    echo "Stop with:  docker compose down"
fi
