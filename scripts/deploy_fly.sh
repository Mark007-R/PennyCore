#!/usr/bin/env bash
# PennyCore — fly.io deploy script (Day 29, Phase 6).
#
# This script is WRITTEN, not RUN in the local-only project mode the
# SKILL specifies. It exists so a future deploy is one command instead
# of an afternoon of "what did I configure last time?" archaeology.
#
# The script is split into discrete, idempotent steps:
#
#   1. Verify prerequisites (flyctl + .env.prod + git clean).
#   2. Create the fly apps if they don't already exist (one app per
#      service — context-engine + orchestrator).
#   3. Provision managed Postgres + Redis on fly (if not already
#      provisioned) and attach them to both apps.
#   4. Set every secret from .env.prod via `fly secrets set` (atomic
#      batch — fly redeploys only after the whole batch lands).
#   5. Build + push + deploy each service from Dockerfile.prod with
#      GIT_SHA / BUILD_DATE / IMAGE_VERSION build args.
#   6. Verify the deploy with `fly status` and a /readyz curl.
#
# Each step is gated on `should_run "step-name"` so you can re-run the
# script with `--skip-to <step>` to resume after a failure.
#
# Usage:
#   ./scripts/deploy_fly.sh                # full deploy
#   ./scripts/deploy_fly.sh --skip-to 5    # re-deploy without re-secrets
#   ./scripts/deploy_fly.sh --dry-run      # print fly commands, run nothing
#
# IMPORTANT: this script is local-only-mode in the current project. The
# `--execute` flag is required to actually run any fly command — the
# default is dry-run-with-real-deployment-commands-printed. The Day 29
# integration test asserts the dry-run path produces a non-empty plan.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration. Override via env vars before invoking the script.
# ---------------------------------------------------------------------------
APP_CONTEXT_ENGINE="${APP_CONTEXT_ENGINE:-pennycore-context-engine}"
APP_ORCHESTRATOR="${APP_ORCHESTRATOR:-pennycore-orchestrator}"
REGION="${FLY_REGION:-iad}"
PG_NAME="${PG_NAME:-pennycore-pg}"
REDIS_NAME="${REDIS_NAME:-pennycore-redis}"
ENV_FILE="${ENV_FILE:-.env.prod}"

DRY_RUN=1                       # default: dry-run only
SKIP_TO=""

# ---------------------------------------------------------------------------
# CLI parsing — keep it minimal; this isn't a CLI framework.
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --execute) DRY_RUN=0; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --skip-to) SKIP_TO="$2"; shift 2 ;;
        -h|--help)
            sed -n '1,40p' "$0"
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
log() {
    printf '\033[1;34m[deploy_fly]\033[0m %s\n' "$*"
}

warn() {
    printf '\033[1;33m[deploy_fly] WARN:\033[0m %s\n' "$*" >&2
}

fail() {
    printf '\033[1;31m[deploy_fly] ERROR:\033[0m %s\n' "$*" >&2
    exit 1
}

run() {
    # Print every fly command for visibility. Execute only when --execute
    # is passed. This is what makes the script safe to commit and
    # safe to share — running it by accident does nothing.
    printf '  $ %s\n' "$*"
    if [[ "$DRY_RUN" -eq 0 ]]; then
        eval "$@"
    fi
}

should_run() {
    local step="$1"
    if [[ -z "$SKIP_TO" ]]; then
        return 0
    fi
    if [[ "$step" == "$SKIP_TO" ]]; then
        # Once we hit the skip-to step, clear SKIP_TO so every subsequent
        # step runs.
        SKIP_TO=""
        return 0
    fi
    log "  (skipping step '$step')"
    return 1
}

# ---------------------------------------------------------------------------
# Step 1 — prerequisites. flyctl must be installed; .env.prod must
# exist; git tree must be clean (so GIT_SHA is meaningful).
# ---------------------------------------------------------------------------
step_prereqs() {
    should_run "1-prereqs" || return 0
    log "Step 1 — verify prerequisites"
    command -v fly >/dev/null 2>&1 || fail "flyctl is not installed (https://fly.io/docs/flyctl)"
    [[ -f "$ENV_FILE" ]] || fail ".env file not found: $ENV_FILE (copy .env.example.prod)"

    if ! git diff --quiet || ! git diff --cached --quiet; then
        warn "git tree is dirty — GIT_SHA tag will not match a reproducible commit"
    fi
}

# ---------------------------------------------------------------------------
# Step 2 — fly apps. Create both if absent. `apps list` is the
# safest probe because `apps show` exits non-zero on missing.
# ---------------------------------------------------------------------------
step_apps() {
    should_run "2-apps" || return 0
    log "Step 2 — ensure fly apps exist"
    local existing
    existing=$(fly apps list --json 2>/dev/null || echo "[]")

    if ! grep -q "$APP_CONTEXT_ENGINE" <<<"$existing"; then
        run "fly apps create '$APP_CONTEXT_ENGINE' --org personal"
    fi
    if ! grep -q "$APP_ORCHESTRATOR" <<<"$existing"; then
        run "fly apps create '$APP_ORCHESTRATOR' --org personal"
    fi
}

# ---------------------------------------------------------------------------
# Step 3 — managed Postgres + Redis. Both attached to BOTH apps so
# DATABASE_URL / REDIS_URL secrets get auto-populated.
# ---------------------------------------------------------------------------
step_datastores() {
    should_run "3-datastores" || return 0
    log "Step 3 — provision and attach managed datastores"
    run "fly postgres create --name '$PG_NAME' --region '$REGION' --vm-size shared-cpu-1x"
    run "fly postgres attach '$PG_NAME' --app '$APP_CONTEXT_ENGINE'"
    run "fly postgres attach '$PG_NAME' --app '$APP_ORCHESTRATOR'"
    run "fly redis create --name '$REDIS_NAME' --region '$REGION' --plan free --no-replicas"
    # fly's redis-attach is a recent flag; older flyctl wants `secrets set`
    # with REDIS_URL manually. We use the new path here and document the
    # fallback in DEPLOY.md.
    run "fly redis status '$REDIS_NAME' --json | jq -r '.private_url' \
         | xargs -I{} fly secrets set REDIS_URL={} --app '$APP_CONTEXT_ENGINE'"
    run "fly redis status '$REDIS_NAME' --json | jq -r '.private_url' \
         | xargs -I{} fly secrets set REDIS_URL={} --app '$APP_ORCHESTRATOR'"
}

# ---------------------------------------------------------------------------
# Step 4 — secrets. Load .env.prod, filter empty values + the build-time
# vars (those go via build args, not secrets), then push as one atomic
# `fly secrets set` invocation per app.
# ---------------------------------------------------------------------------
step_secrets() {
    should_run "4-secrets" || return 0
    log "Step 4 — sync secrets from $ENV_FILE"

    local pairs=()
    while IFS='=' read -r key value; do
        # Skip blanks, comments, and vars that shouldn't be secrets.
        [[ -z "$key" || "$key" =~ ^# || -z "$value" ]] && continue
        case "$key" in
            GIT_SHA|BUILD_DATE|IMAGE_VERSION) continue ;;
            CONTEXT_ENGINE_WORKERS|ORCHESTRATOR_WORKERS|CONTEXT_ENGINE_PORT|ORCHESTRATOR_PORT) continue ;;
            *) pairs+=("$key=$value") ;;
        esac
    done <"$ENV_FILE"

    if [[ ${#pairs[@]} -eq 0 ]]; then
        warn "no secrets discovered in $ENV_FILE — skipping fly secrets set"
        return 0
    fi

    # Atomic batch update — fly only redeploys after the whole set lands.
    run "fly secrets set ${pairs[*]} --app '$APP_CONTEXT_ENGINE' --stage"
    run "fly secrets set ${pairs[*]} --app '$APP_ORCHESTRATOR' --stage"
    run "fly secrets deploy --app '$APP_CONTEXT_ENGINE'"
    run "fly secrets deploy --app '$APP_ORCHESTRATOR'"
}

# ---------------------------------------------------------------------------
# Step 5 — build + deploy. Build args carry the git SHA + build date
# so the deployed `/info` endpoint surfaces them.
# ---------------------------------------------------------------------------
step_deploy() {
    should_run "5-deploy" || return 0
    log "Step 5 — build + deploy"

    local git_sha build_date
    git_sha="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
    build_date="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    run "fly deploy --app '$APP_CONTEXT_ENGINE' \
        --dockerfile Dockerfile.prod \
        --build-arg GIT_SHA='$git_sha' \
        --build-arg BUILD_DATE='$build_date' \
        --build-arg IMAGE_VERSION=0.2.0 \
        --strategy rolling"

    run "fly deploy --app '$APP_ORCHESTRATOR' \
        --dockerfile Dockerfile.prod \
        --build-arg GIT_SHA='$git_sha' \
        --build-arg BUILD_DATE='$build_date' \
        --build-arg IMAGE_VERSION=0.2.0 \
        --strategy rolling"
}

# ---------------------------------------------------------------------------
# Step 6 — verify. `fly status` + a /readyz curl from each app's
# public URL. A non-200 on /readyz fails the script.
# ---------------------------------------------------------------------------
step_verify() {
    should_run "6-verify" || return 0
    log "Step 6 — verify"
    run "fly status --app '$APP_CONTEXT_ENGINE'"
    run "fly status --app '$APP_ORCHESTRATOR'"
    run "curl --fail --silent 'https://${APP_CONTEXT_ENGINE}.fly.dev/readyz' | jq ."
    run "curl --fail --silent 'https://${APP_ORCHESTRATOR}.fly.dev/readyz' | jq ."
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
log "PennyCore fly.io deploy — dry-run=$DRY_RUN, skip-to=${SKIP_TO:-<none>}"
log "  context-engine app: $APP_CONTEXT_ENGINE"
log "  orchestrator app:   $APP_ORCHESTRATOR"
log "  region:             $REGION"
log "  env file:           $ENV_FILE"

step_prereqs
step_apps
step_datastores
step_secrets
step_deploy
step_verify

log "done."
if [[ "$DRY_RUN" -eq 1 ]]; then
    log "this was a dry-run. To actually deploy, re-run with --execute."
fi
