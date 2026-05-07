"""orchestrator FastAPI scaffold (Phase 1, Day 4).

Surface area today is intentionally minimal — three endpoints that prove
the container boots, the package imports, and the service answers HTTP.

| Endpoint   | Purpose                                                          |
|------------|------------------------------------------------------------------|
| `GET /`    | Service identity + LLM mode (mock / anthropic / azure / openai)  |
| `/healthz` | Liveness probe — process is up. No external deps probed.          |
| `/readyz`  | Readiness probe — Day 8 adds Postgres + Redis connectivity.       |

The real surface lands Day 8 (Redis event listener), Day 9 (LLM planner),
Day 10 (policy check + approval queue + executor + audit log). All future
endpoints scope by `tenant_id` per the multi-tenant invariant (rule 15)
and write to the audit log per the audit invariant (rule 16).
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()


app = FastAPI(
    title="PennyCore — orchestrator",
    description=(
        "Decision-maker. Listens to events, asks the LLM what to do, applies "
        "tenant-specific policy rules, manages a human-approval queue, "
        "prevents double-action on duplicate events, writes the audit trail."
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
        "service": "orchestrator",
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

    Today (Day 4): scaffold-only — always returns ready=true. Day 8+ wires
    real Postgres + Redis connectivity probes (the orchestrator subscribes
    to a Redis channel for events from context-engine).
    """
    return {
        "status": "ready",
        "scaffold": True,
        "datastores_probed": False,
    }
