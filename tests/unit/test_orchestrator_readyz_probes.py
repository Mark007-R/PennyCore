"""Day-8 `/readyz` probe wiring tests.

The orchestrator app reads `DATABASE_URL` and `REDIS_URL` at request
time inside `readyz`, so toggling them via `monkeypatch.setenv` is
enough to flip the probe mode per test. We patch the underlying probe
implementations so no live broker / database is required.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import orchestrator.api as orch_api


@pytest.fixture
def client() -> TestClient:
    orch_api._reset_listener_for_tests()
    yield TestClient(orch_api.app)
    orch_api._reset_listener_for_tests()


def test_readyz_no_probes_when_no_env(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["datastores_probed"] is False
    assert body["probed"] == {}


def test_readyz_redis_probe_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    class FakeRedisModule:
        class Redis:
            @staticmethod
            def from_url(_url: str, decode_responses: bool = True) -> "FakeClient":  # noqa: ARG004
                return FakeClient()

    class FakeClient:
        def ping(self) -> bool:
            return True

    monkeypatch.setitem(__import__("sys").modules, "redis", FakeRedisModule)

    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["probed"] == {"redis": "ok"}


def test_readyz_redis_probe_failure_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://broken:6379/0")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    class FakeRedisModule:
        class Redis:
            @staticmethod
            def from_url(_url: str, decode_responses: bool = True) -> "FakeClient":  # noqa: ARG004
                return FakeClient()

    class FakeClient:
        def ping(self) -> bool:
            raise ConnectionError("redis is on fire")

    monkeypatch.setitem(__import__("sys").modules, "redis", FakeRedisModule)

    r = client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert "redis" in body["failures"]
    assert "ConnectionError" in body["failures"]["redis"]
