"""Tests for the Day-29 `/info` endpoint + the `contracts.build_info`
helper that backs it.

Three things matter:

  1. `build_info()` returns the env-var values when set.
  2. `build_info()` falls back to ``"unknown"`` + ``mode="dev"`` when
     unset (dev container path).
  3. The endpoint round-trips both modes on each service.

We force module-level singletons to be observed via the FastAPI
TestClient — no live containers needed.
"""

from __future__ import annotations

import importlib
import os

from fastapi.testclient import TestClient

import contracts.build_info as build_info_mod


def test_build_info_dev_mode_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("PENNYCORE_GIT_SHA", raising=False)
    monkeypatch.delenv("PENNYCORE_BUILD_DATE", raising=False)
    monkeypatch.delenv("PENNYCORE_IMAGE_VERSION", raising=False)
    info = build_info_mod.build_info()
    # Day 30 added `tracing_mode` — assert on the load-bearing fields
    # only so the test doesn't break next time we add a self-describe
    # field.
    assert info["git_sha"] == "unknown"
    assert info["build_date"] == "unknown"
    assert info["image_version"] == "unknown"
    assert info["mode"] == "dev"
    assert "tracing_mode" in info


def test_build_info_prod_mode_when_all_three_env_vars_set(monkeypatch) -> None:
    monkeypatch.setenv("PENNYCORE_GIT_SHA", "abc1234")
    monkeypatch.setenv("PENNYCORE_BUILD_DATE", "2026-06-04T10:00:00Z")
    monkeypatch.setenv("PENNYCORE_IMAGE_VERSION", "0.2.0")
    info = build_info_mod.build_info()
    assert info["git_sha"] == "abc1234"
    assert info["build_date"] == "2026-06-04T10:00:00Z"
    assert info["image_version"] == "0.2.0"
    assert info["mode"] == "prod"


def test_build_info_partial_env_stays_dev_mode(monkeypatch) -> None:
    # Two of three set → still dev. Avoids labelling half-configured
    # local containers as prod.
    monkeypatch.setenv("PENNYCORE_GIT_SHA", "abc1234")
    monkeypatch.setenv("PENNYCORE_BUILD_DATE", "2026-06-04T10:00:00Z")
    monkeypatch.delenv("PENNYCORE_IMAGE_VERSION", raising=False)
    info = build_info_mod.build_info()
    assert info["mode"] == "dev"
    assert info["image_version"] == "unknown"


def test_build_info_empty_string_env_treated_as_unset(monkeypatch) -> None:
    # Compose / fly.io sometimes pass empty strings for missing build args.
    monkeypatch.setenv("PENNYCORE_GIT_SHA", "")
    monkeypatch.setenv("PENNYCORE_BUILD_DATE", "  ")
    monkeypatch.setenv("PENNYCORE_IMAGE_VERSION", "")
    info = build_info_mod.build_info()
    assert info["git_sha"] == "unknown"
    assert info["build_date"] == "unknown"
    assert info["image_version"] == "unknown"
    assert info["mode"] == "dev"


def test_context_engine_info_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("PENNYCORE_GIT_SHA", "deadbeef")
    monkeypatch.setenv("PENNYCORE_BUILD_DATE", "2026-06-04T12:00:00Z")
    monkeypatch.setenv("PENNYCORE_IMAGE_VERSION", "0.2.0")
    # Re-import the module so the test sees a fresh resolution path —
    # the endpoint itself is dynamic (reads env on every call) but
    # importing under monkeypatched env keeps the parity story clean.
    import context_engine.api as ce_api

    importlib.reload(ce_api)
    client = TestClient(ce_api.app)

    response = client.get("/info")
    assert response.status_code == 200
    payload = response.json()
    assert payload["service"] == "context-engine"
    assert payload["git_sha"] == "deadbeef"
    assert payload["build_date"] == "2026-06-04T12:00:00Z"
    assert payload["image_version"] == "0.2.0"
    assert payload["mode"] == "prod"


def test_orchestrator_info_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("PENNYCORE_GIT_SHA", "cafef00d")
    monkeypatch.setenv("PENNYCORE_BUILD_DATE", "2026-06-04T13:00:00Z")
    monkeypatch.setenv("PENNYCORE_IMAGE_VERSION", "0.2.0")

    import orchestrator.api as orch_api

    importlib.reload(orch_api)
    client = TestClient(orch_api.app)

    response = client.get("/info")
    assert response.status_code == 200
    payload = response.json()
    assert payload["service"] == "orchestrator"
    assert payload["git_sha"] == "cafef00d"
    assert payload["mode"] == "prod"


def test_orchestrator_info_endpoint_dev_mode_when_env_unset(monkeypatch) -> None:
    for var in (
        "PENNYCORE_GIT_SHA",
        "PENNYCORE_BUILD_DATE",
        "PENNYCORE_IMAGE_VERSION",
    ):
        monkeypatch.delenv(var, raising=False)

    import orchestrator.api as orch_api

    importlib.reload(orch_api)
    client = TestClient(orch_api.app)

    payload = client.get("/info").json()
    assert payload["mode"] == "dev"
    assert payload["git_sha"] == "unknown"
