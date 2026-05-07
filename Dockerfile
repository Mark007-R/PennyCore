# PennyCore — multi-stage Dockerfile shared by context-engine and orchestrator.
#
# Both services run the same image; docker-compose.yml overrides the CMD
# to point uvicorn at the right ASGI app. Sharing the image is deliberate:
# both services share `contracts/` and (Day 5+) the LLM dispatch layer.
#
#   stage `base`     — pinned Python + OS deps + non-root user
#   stage `deps`     — pip install (cached as long as pyproject.toml unchanged)
#   stage `runtime`  — final layer with the source tree + default CMD
#
# Build:        docker build -t pennycore:dev .
# Run (CE):     docker run -p 8001:8000 pennycore:dev
# Run (orch):   docker run -p 8002:8000 pennycore:dev \
#                 uvicorn orchestrator.api:app --host 0.0.0.0 --port 8000

# syntax=docker/dockerfile:1.7

# -----------------------------------------------------------------------------
# Stage 1: base — Python + minimal OS deps + unprivileged user
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# `curl` is used by the docker-compose healthcheck below.
# `libpq` ships preinstalled in the slim image's psycopg wheel route, but we
# still need build tooling for any future source-only deps.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Run as non-root. UID 1000 keeps bind-mounts on Linux dev hosts writable.
RUN useradd --create-home --shell /bin/bash --uid 1000 pennycore
WORKDIR /app

# -----------------------------------------------------------------------------
# Stage 2: deps — install Python deps. Cached as long as pyproject.toml unchanged.
# -----------------------------------------------------------------------------
FROM base AS deps

# Copy ONLY the manifest files so this layer caches across most edits.
COPY pyproject.toml requirements.txt README.md ./

RUN pip install --upgrade pip \
 && pip install -r requirements.txt

# -----------------------------------------------------------------------------
# Stage 3: runtime — copy source, install in editable mode, drop privileges
# -----------------------------------------------------------------------------
FROM deps AS runtime

# Source — order chosen so contracts (rarely-changing) lands before services.
COPY contracts ./contracts
COPY context_engine ./context_engine
COPY orchestrator ./orchestrator

# Install PennyCore itself in editable mode so `from contracts import ...`
# works without the conftest sys.path shim that tests still use locally.
RUN pip install --no-deps -e .

# Make the working tree owned by the non-root user before dropping.
RUN chown -R pennycore:pennycore /app
USER pennycore

# Default command — context-engine on port 8000. docker-compose overrides for
# the orchestrator container.
EXPOSE 8000
CMD ["uvicorn", "context_engine.api:app", "--host", "0.0.0.0", "--port", "8000"]

# Healthcheck: the FastAPI scaffold's /healthz endpoint. Compose probes
# at the orchestration layer too; this is the per-container fallback.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
    CMD curl --fail --silent http://localhost:8000/healthz || exit 1
