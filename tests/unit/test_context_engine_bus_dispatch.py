"""Day-8 context-engine bus dispatcher tests.

Asserts the `REDIS_URL`-based selector picks the right `EventBus`
implementation. Mirrors the orchestrator's listener-mode test in
`test_orchestrator_readyz_probes.py`.
"""

from __future__ import annotations

import importlib
import sys

import pytest


def _reload_api(monkeypatch: pytest.MonkeyPatch) -> object:
    """Reload `context_engine.api` so the module-level bus selector
    re-runs against the patched env."""
    if "context_engine.api" in sys.modules:
        del sys.modules["context_engine.api"]
    return importlib.import_module("context_engine.api")


def test_no_redis_url_uses_in_memory_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    api = _reload_api(monkeypatch)
    assert api._bus_mode == "in-memory"
    from context_engine.event_bus import InMemoryEventBus
    assert isinstance(api._default_bus, InMemoryEventBus)


def test_redis_url_set_uses_redis_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://fake-host:6379/0")

    # Inject a fake redis module so `redis.Redis.from_url` returns a stub
    # rather than attempting a real connection.
    class FakeRedisModule:
        class Redis:
            @staticmethod
            def from_url(_url: str, decode_responses: bool = True):  # noqa: ARG004
                class FakeClient:
                    def publish(self, _c: str, _m: str) -> int:
                        return 0
                    def ping(self) -> bool:
                        return True
                    def close(self) -> None:
                        pass
                return FakeClient()
    monkeypatch.setitem(sys.modules, "redis", FakeRedisModule)

    api = _reload_api(monkeypatch)
    assert api._bus_mode == "redis"
    from context_engine.redis_event_bus import RedisEventBus
    assert isinstance(api._default_bus, RedisEventBus)


def test_root_endpoint_surfaces_bus_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GET /` reports the resolved bus mode for operational visibility."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    api = _reload_api(monkeypatch)
    from fastapi.testclient import TestClient

    client = TestClient(api.app)
    body = client.get("/").json()
    assert body["bus_mode"] in {"in-memory", "redis"}
    assert "datastore_mode" in body
