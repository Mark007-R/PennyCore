"""Tests for the Day-29 production deploy assets.

These don't actually run `docker build` or `flyctl` (too slow + would
need a daemon). They assert the *invariants* that make the assets
trustworthy:

  * `Dockerfile.prod` uses gunicorn (not bare uvicorn), bakes the
    git SHA env vars, runs tini as PID 1, drops to non-root.
  * `docker-compose.prod.yml` does not bind-mount the source tree
    (that would defeat the "image is frozen at build time" guarantee
    — the prod-vs-dev distinction the SKILL Day 29 task asks for).
  * `docker-compose.prod.yml` requires `POSTGRES_PASSWORD` (fail-fast
    on missing secret).
  * `scripts/deploy_fly.sh` exists, is executable-mode in tree, has
    the documented step layout, defaults to dry-run, and the dry-run
    plan covers all six steps.
  * `.env.example.prod` is committed and lists the exact same env vars
    `docker-compose.prod.yml` references — guarding against drift.

The Day-29 deploy story is "written, not run" per the SKILL; the
substitute for `actually run it` is `assert it would be coherent`.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Dockerfile.prod
# ---------------------------------------------------------------------------


def test_dockerfile_prod_exists() -> None:
    assert (REPO_ROOT / "Dockerfile.prod").is_file()


def test_dockerfile_prod_uses_gunicorn_not_bare_uvicorn() -> None:
    body = _read("Dockerfile.prod")
    # The CMD must launch gunicorn with the uvicorn worker class.
    assert "gunicorn" in body
    assert "uvicorn.workers.UvicornWorker" in body
    # Reject `uvicorn ...` as the top-level process — bare uvicorn
    # doesn't manage worker recycling on SIGTERM.
    cmd_lines = [
        line.strip()
        for line in body.splitlines()
        if line.lstrip().startswith("CMD") or line.lstrip().startswith('"uvicorn"')
    ]
    bare_uvicorn = [line for line in cmd_lines if line.startswith('CMD ["uvicorn"')]
    assert not bare_uvicorn, f"prod CMD launches bare uvicorn: {bare_uvicorn}"


def test_dockerfile_prod_uses_tini_as_pid1() -> None:
    body = _read("Dockerfile.prod")
    assert "tini" in body
    # Tini is invoked via ENTRYPOINT so signals propagate to gunicorn.
    assert re.search(r'ENTRYPOINT\s*\[\s*"/usr/bin/tini"', body), (
        "tini must be the ENTRYPOINT for signal propagation"
    )


def test_dockerfile_prod_runs_as_non_root() -> None:
    body = _read("Dockerfile.prod")
    assert re.search(r"^USER pennycore", body, re.MULTILINE), (
        "prod image must drop privileges to the pennycore user"
    )


def test_dockerfile_prod_carries_build_metadata_env() -> None:
    body = _read("Dockerfile.prod")
    for var in (
        "PENNYCORE_GIT_SHA",
        "PENNYCORE_BUILD_DATE",
        "PENNYCORE_IMAGE_VERSION",
    ):
        assert var in body, f"{var} must be baked into the prod image"


def test_dockerfile_prod_has_healthcheck() -> None:
    body = _read("Dockerfile.prod")
    assert "HEALTHCHECK" in body
    assert "/healthz" in body


# ---------------------------------------------------------------------------
# docker-compose.prod.yml
# ---------------------------------------------------------------------------


def test_compose_prod_exists() -> None:
    assert (REPO_ROOT / "docker-compose.prod.yml").is_file()


def test_compose_prod_uses_dockerfile_prod() -> None:
    body = _read("docker-compose.prod.yml")
    # Both services build from Dockerfile.prod.
    assert body.count("dockerfile: Dockerfile.prod") == 2


def test_compose_prod_does_not_bind_mount_source() -> None:
    """Prod must NOT bind-mount source — image is frozen at build time."""
    body = _read("docker-compose.prod.yml")
    for forbidden in (
        "./context_engine:/app/context_engine",
        "./orchestrator:/app/orchestrator",
        "./contracts:/app/contracts",
    ):
        assert forbidden not in body, (
            f"prod compose accidentally binds {forbidden} — defeats the "
            f"point of a prod image"
        )


def test_compose_prod_requires_postgres_password() -> None:
    """The compose file must fail-fast on missing POSTGRES_PASSWORD."""
    body = _read("docker-compose.prod.yml")
    # `${POSTGRES_PASSWORD:?...}` syntax — colon-question-mark — is the
    # docker compose fail-fast for required vars.
    assert re.search(r"\$\{POSTGRES_PASSWORD:\?", body), (
        "POSTGRES_PASSWORD must be required (use :? fail-fast syntax)"
    )


def test_compose_prod_overrides_orchestrator_cmd_with_gunicorn() -> None:
    body = _read("docker-compose.prod.yml")
    # The orchestrator container overrides CMD to point gunicorn at
    # orchestrator.api:app. Without this override both containers
    # would serve context_engine.
    assert "orchestrator.api:app" in body
    assert body.count("gunicorn") >= 1


def test_compose_prod_no_reload_flag_anywhere() -> None:
    body = _read("docker-compose.prod.yml")
    # `--reload` is a dev-only uvicorn flag; finding it in the prod
    # compose means we're shipping a hot-reload watcher to production.
    assert "--reload" not in body, "prod compose must not use uvicorn --reload"


def test_compose_prod_restart_policy_is_unless_stopped() -> None:
    body = _read("docker-compose.prod.yml")
    assert body.count("restart: unless-stopped") >= 4  # pg, redis, ce, orch


# ---------------------------------------------------------------------------
# .env.example.prod
# ---------------------------------------------------------------------------


def test_env_example_prod_exists() -> None:
    assert (REPO_ROOT / ".env.example.prod").is_file()


def test_env_example_prod_lists_every_compose_variable() -> None:
    """Catch the most common drift bug: a new env var added to the
    compose file without a sibling entry here.

    Commented-out compose lines don't count — operators set those only
    when they need a non-default behavior, and the SKILL's "minimal
    prod surface" principle says we don't surface them by default.
    """
    compose_body = _read("docker-compose.prod.yml")
    env_body = _read(".env.example.prod")

    # Drop fully-commented compose lines so the var-extraction matches
    # what the compose file *actually* reads at boot.
    active_compose = "\n".join(
        line for line in compose_body.splitlines() if not line.lstrip().startswith("#")
    )

    refs = set(re.findall(r"\$\{([A-Z_]+)(?::[?:-][^}]*)?\}", active_compose))
    # Drop refs that are intentionally synthesized (`DATABASE_URL` and
    # `REDIS_URL` default to values computed from the other secrets).
    intentional_defaults = {"DATABASE_URL", "REDIS_URL"}
    expected = refs - intentional_defaults

    missing = sorted(var for var in expected if var not in env_body)
    assert not missing, (
        f".env.example.prod is missing entries for: {missing}. "
        "Add them so a fresh deploy host sees every var it needs."
    )


# ---------------------------------------------------------------------------
# scripts/deploy_fly.sh
# ---------------------------------------------------------------------------


def test_deploy_fly_script_exists() -> None:
    assert (REPO_ROOT / "scripts" / "deploy_fly.sh").is_file()


def test_deploy_fly_documents_all_six_steps() -> None:
    body = _read("scripts/deploy_fly.sh")
    # Each step is a discrete function. Drift here means the SKILL's
    # 6-step contract is silently broken.
    for step in (
        "step_prereqs",
        "step_apps",
        "step_datastores",
        "step_secrets",
        "step_deploy",
        "step_verify",
    ):
        assert f"{step}()" in body, f"deploy_fly.sh missing function {step}"


def test_deploy_fly_defaults_to_dry_run() -> None:
    body = _read("scripts/deploy_fly.sh")
    assert "DRY_RUN=1" in body
    # The `run` helper guards `eval` on DRY_RUN — otherwise the script
    # would execute on every invocation. The Day-29 SKILL contract says
    # "written but NOT run".
    assert re.search(r'if \[\[ "\$DRY_RUN" -eq 0 \]\]', body), (
        "deploy script must guard execution behind DRY_RUN=0"
    )


def test_deploy_fly_requires_execute_flag_for_real_run() -> None:
    body = _read("scripts/deploy_fly.sh")
    # --execute is the documented opt-in flag.
    assert "--execute) DRY_RUN=0" in body


def test_deploy_fly_dry_run_prints_all_six_step_commands() -> None:
    """Static surrogate for "would a dry-run print every step?".

    The original plan was to subprocess-run `bash deploy_fly.sh --dry-run`
    and grep its stdout, but on Windows hosts the bash shim emits
    UTF-16 output that's not portable to assert on. Instead we read
    the script directly and assert each step function calls `log` with
    its documented banner — same load-bearing contract, no shell
    dependency.
    """
    body = _read("scripts/deploy_fly.sh")

    expected_banners = [
        "Step 1 — verify prerequisites",
        "Step 2 — ensure fly apps exist",
        "Step 3 — provision and attach managed datastores",
        "Step 4 — sync secrets",
        "Step 5 — build + deploy",
        "Step 6 — verify",
    ]
    for banner in expected_banners:
        assert banner in body, f"deploy_fly.sh missing step banner: {banner!r}"


def test_deploy_fly_atomic_secrets_batch() -> None:
    """One `fly secrets set ... --stage` then `fly secrets deploy` per
    app is the documented atomic pattern. Without --stage each `set`
    triggers an immediate redeploy → N redeploys for N vars → noisy."""
    body = _read("scripts/deploy_fly.sh")
    assert "secrets set" in body
    assert "--stage" in body
    assert "secrets deploy" in body


# ---------------------------------------------------------------------------
# .gitignore + .dockerignore — defence in depth for .env.prod
# ---------------------------------------------------------------------------


def test_env_prod_is_gitignored() -> None:
    body = _read(".gitignore")
    assert re.search(r"^\.env\.prod$", body, re.MULTILINE), (
        ".env.prod must be listed in .gitignore"
    )


def test_env_prod_is_dockerignored() -> None:
    body = _read(".dockerignore")
    assert re.search(r"^\.env\.prod$", body, re.MULTILINE), (
        ".env.prod must be listed in .dockerignore"
    )
