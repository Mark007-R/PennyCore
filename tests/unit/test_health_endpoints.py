"""Smoke tests for the Day-4 FastAPI scaffolds.

Verifies that:
  * Both apps import cleanly (no missing deps in `pyproject.toml`).
  * `/healthz` returns 200 with the expected body shape.
  * `/readyz` returns 200 (scaffold mode — no datastore probes yet).
  * `GET /` reports the right service identity.
  * The OpenAPI schema is generated, so client codegen is unblocked.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from context_engine.api import app as ce_app
from orchestrator.api import app as orch_app


@pytest.fixture(scope="module")
def ce_client() -> TestClient:
    return TestClient(ce_app)


@pytest.fixture(scope="module")
def orch_client() -> TestClient:
    return TestClient(orch_app)


# --- context-engine ---------------------------------------------------------

def test_context_engine_root(ce_client: TestClient) -> None:
    r = ce_client.get("/")
    assert r.status_code == 200
    body: dict[str, Any] = r.json()
    assert body["service"] == "context-engine"
    assert body["version"] == "0.1.0"
    assert body["status"] == "scaffold"
    assert body["phase"] == "1-foundation"
    assert body["llm_mode"] in {"mock", "anthropic", "azure", "openai"}


def test_context_engine_healthz(ce_client: TestClient) -> None:
    r = ce_client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_context_engine_readyz(ce_client: TestClient) -> None:
    r = ce_client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["scaffold"] is True
    assert body["datastores_probed"] is False


# --- orchestrator -----------------------------------------------------------

def test_orchestrator_root(orch_client: TestClient) -> None:
    r = orch_client.get("/")
    assert r.status_code == 200
    body: dict[str, Any] = r.json()
    assert body["service"] == "orchestrator"
    assert body["version"] == "0.1.0"
    assert body["status"] == "scaffold"
    assert body["phase"] == "1-foundation"


def test_orchestrator_healthz(orch_client: TestClient) -> None:
    r = orch_client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_orchestrator_readyz(orch_client: TestClient) -> None:
    r = orch_client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


# --- both -------------------------------------------------------------------

def test_openapi_schema_exposed(ce_client: TestClient, orch_client: TestClient) -> None:
    """OpenAPI schema is the contract for the future admin UI / client SDK."""
    for client, expected_title in (
        (ce_client, "PennyCore — context-engine"),
        (orch_client, "PennyCore — orchestrator"),
    ):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        assert spec["info"]["title"] == expected_title
        # The three Phase-1 endpoints must be present.
        assert "/healthz" in spec["paths"]
        assert "/readyz" in spec["paths"]
        assert "/" in spec["paths"]


def test_unknown_route_returns_404(ce_client: TestClient) -> None:
    r = ce_client.get("/this-endpoint-does-not-exist")
    assert r.status_code == 404
