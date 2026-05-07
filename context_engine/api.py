"""context-engine FastAPI scaffold (Phase 1, Day 4).

Surface area today is intentionally minimal — three endpoints that prove
the container boots, the package imports, and the service answers HTTP.

| Endpoint   | Purpose                                                       |
|------------|---------------------------------------------------------------|
| `GET /`    | Service identity + LLM mode (mock / anthropic / azure / openai) |
| `/healthz` | Liveness probe — process is up. No external deps probed.       |
| `/readyz`  | Readiness probe — Day 5 adds Postgres + Redis connectivity.    |

The real surface lands Day 5 (`POST /events` ingestion + cross-channel
linking) and Day 7 (`POST /briefs/assemble`). All future endpoints scope
by `tenant_id` per the multi-tenant invariant (rule 15).
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI

# `.env` lives at the project root and is loaded once at import time.
# Loading is idempotent — calling it again from the orchestrator app is a no-op.
load_dotenv()


app = FastAPI(
    title="PennyCore — context-engine",
    description=(
        "Memory librarian. Stores every customer interaction across channels, "
        "links customers across channels, and assembles a token-budgeted brief "
        "for the LLM before each reply."
    ),
    version="0.1.0",
)


def _llm_mode() -> str:
    """Resolve the active LLM mode from env. Mirrors `context_engine/llm/__init__.py` (Day 5)."""
    if os.getenv("MOCK_LLM", "").lower() in ("true", "1", "yes"):
        return "mock"
    return os.getenv("LLM_PROVIDER", "mock").lower()


@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    """Service identity + active LLM mode. Useful for the demo UI."""
    return {
        "service": "context-engine",
        "version": app.version,
        "llm_mode": _llm_mode(),
        "status": "scaffold",
        "phase": "1-foundation",
    }


@app.get("/healthz", tags=["health"])
def healthz() -> dict[str, str]:
    """Liveness probe — the process is up. No external dependencies probed."""
    return {"status": "ok"}


@app.get("/readyz", tags=["health"])
def readyz() -> dict[str, Any]:
    """Readiness probe.

    Today (Day 4): scaffold-only — always returns ready=true. Day 5 wires
    real Postgres + Redis connectivity probes; until then the container can
    start before its dependencies are healthy without flapping the readiness
    signal.
    """
    return {
        "status": "ready",
        "scaffold": True,
        "datastores_probed": False,
    }
