"""Build-metadata surface (Day 29, Phase 6).

`Dockerfile.prod` bakes the git SHA + build date into image labels AND
into env vars (`PENNYCORE_GIT_SHA`, `PENNYCORE_BUILD_DATE`,
`PENNYCORE_IMAGE_VERSION`). The `/info` endpoint on both services
reads those env vars so a deployed instance is self-describing — the
exact thing fly.io / railway dashboards plus an on-call engineer want
when they hit a misbehaving service and need to know which commit is
actually running.

Lives in `contracts/` rather than per-service so both `context_engine/`
and `orchestrator/` import the same source-of-truth dict, and so the
fly.io deploy adapter (`scripts/deploy_fly.sh`) can ship the same
shape — `/info` on the deployed app matches `/info` from
`docker compose -f docker-compose.prod.yml up` byte-for-byte (modulo
the SHA + date).

Why a single dict and not per-field helpers: the prod surface is a
*self-describing capability*, not a feature with branching behavior.
A flat dict is the simplest thing that matches the JSON shape the
endpoint emits, and it's trivially testable.
"""

from __future__ import annotations

import os
from typing import Any


def build_info() -> dict[str, Any]:
    """Return the running container's build metadata.

    Reads three env vars baked in by `Dockerfile.prod`:

      * `PENNYCORE_GIT_SHA`      — short git revision at build time.
      * `PENNYCORE_BUILD_DATE`   — UTC ISO-8601 timestamp at build time.
      * `PENNYCORE_IMAGE_VERSION`— the image semver from the build arg.

    If a var is unset (dev / non-prod image), the field falls back to
    ``"unknown"`` rather than raising — `/info` is a diagnostic surface,
    not a guarantee-of-build-metadata. The `mode` field carries
    ``"prod"`` only when all three vars are present and non-empty, so
    operators can tell at a glance whether they're looking at a real
    prod image or a dev container that happens to expose `/info`.
    """
    sha = os.getenv("PENNYCORE_GIT_SHA", "").strip() or "unknown"
    built = os.getenv("PENNYCORE_BUILD_DATE", "").strip() or "unknown"
    version = os.getenv("PENNYCORE_IMAGE_VERSION", "").strip() or "unknown"
    is_prod = all(v != "unknown" for v in (sha, built, version))
    return {
        "git_sha": sha,
        "build_date": built,
        "image_version": version,
        "mode": "prod" if is_prod else "dev",
    }
