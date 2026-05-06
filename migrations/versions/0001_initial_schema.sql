-- PennyCore — Initial schema (Day 3, Phase 1).
--
-- Twelve tables. One Postgres database. One logical schema.
-- Multi-tenant discriminator (`tenant_id`) on every tenant-scoped row.
--
-- This DDL is the source-of-truth for both the production code paths in
-- context_engine/ and orchestrator/ AND the takehome adapters. It is
-- consumed by Alembic on Day 4 (the first migration), and the Pydantic
-- contracts in contracts/ are kept in lock-step with the column shapes
-- here.
--
-- Invariants this DDL enforces (cross-references docs/SYSTEM_DESIGN.md §5):
--   §5.1 Idempotency        — UNIQUE (tenant_id, idempotency_key) on events.
--   §5.2 Tenant isolation   — every scoped table carries tenant_id NOT NULL,
--                              with FK to tenants(id) and indexes that lead
--                              with tenant_id so query plans cannot bypass it.
--   §5.3 Auditability       — audit_log carries caused_by_event_id +
--                              action_id as joinable columns, not JSON.
--                              Append-only trigger lands Day 4 (Phase 1 wrap)
--                              once Alembic + Postgres are wired in Docker.
--   §5.4 Token budget       — briefs.token_count is recorded per cached brief
--                              so the assembler can be audited post-hoc.
--   §5.5 Graceful degradation — action_proposals.proposed_by records
--                              {llm, fallback} so degraded paths are visible.
--
-- Conventions:
--   * IDs: TEXT (ULID-shaped strings like "evt_01HZX..."), so they sort
--     by creation time and stay human-debuggable in logs. Postgres UUID
--     would also be fine; ULID-as-text is chosen for log readability.
--   * Timestamps: TIMESTAMPTZ DEFAULT NOW(). Always UTC.
--   * Soft enums: TEXT + CHECK constraint, not Postgres ENUM. Easier to
--     evolve without a migration that rewrites a system catalog.
--   * JSON payloads: JSONB (not JSON), so we can index / query them later.
--   * Cascade rules: tenant deletes cascade EVERYTHING (test-data cleanup);
--     customer deletes do NOT cascade events / actions (audit preservation).

-- ---------------------------------------------------------------------------
-- 0. Extensions (pgvector for Phase 3 semantic retrieval; safe to enable now)
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- 1. tenants — one row per customer-of-PennyCore (e.g. Acme Bank)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tenants (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------------------
-- 2. channels — global lookup (chat / email / sms / voice / api)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS channels (
    code            TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    CHECK (code IN ('chat', 'email', 'sms', 'voice', 'api'))
);

INSERT INTO channels (code, display_name) VALUES
    ('chat',  'Chat'),
    ('email', 'Email'),
    ('sms',   'SMS'),
    ('voice', 'Voice'),
    ('api',   'API')
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3. customers — one row per end-user (e.g. Jane Doe at Acme Bank)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS customers (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    display_name    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_customers_tenant ON customers (tenant_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- 4. customer_identities — channel handles → customer
--    "phone +15550100 belongs to cust-7 at tenant acme-bank"
--    UNIQUE (tenant_id, identity_kind, identity_value) is the linker oracle.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS customer_identities (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id     TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    identity_kind   TEXT NOT NULL,
    identity_value  TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (identity_kind IN ('email', 'phone', 'external_id', 'chat_handle')),
    UNIQUE (tenant_id, identity_kind, identity_value)
);

CREATE INDEX IF NOT EXISTS idx_identities_customer ON customer_identities (tenant_id, customer_id);

-- ---------------------------------------------------------------------------
-- 5. events — every inbound event from any channel
--    Idempotency key: UNIQUE (tenant_id, idempotency_key). Re-ingest of same
--    key returns the existing event_id (§5.1).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS events (
    id                  TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id         TEXT REFERENCES customers(id) ON DELETE SET NULL,
    channel_code        TEXT NOT NULL REFERENCES channels(code),
    event_type          TEXT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    payload             JSONB NOT NULL DEFAULT '{}'::jsonb,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, idempotency_key),
    CHECK (event_type IN (
        'message_received',
        'document_uploaded',
        'status_changed',
        'anomaly_detected',
        'system_event'
    ))
);

CREATE INDEX IF NOT EXISTS idx_events_tenant_received ON events (tenant_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_customer       ON events (tenant_id, customer_id, received_at DESC);

-- ---------------------------------------------------------------------------
-- 6. messages — conversational subset of events, materialized on ingest
--    `event_id` FK lets us reconstruct provenance; `body` is the searchable
--    text. embedding column is pgvector; populated lazily in Phase 3.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id     TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    event_id        TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    channel_code    TEXT NOT NULL REFERENCES channels(code),
    direction       TEXT NOT NULL,
    body            TEXT NOT NULL,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    embedding       vector(384),
    CHECK (direction IN ('inbound', 'outbound'))
);

CREATE INDEX IF NOT EXISTS idx_messages_customer_recent
    ON messages (tenant_id, customer_id, sent_at DESC);

-- ---------------------------------------------------------------------------
-- 7. briefs — cached assembled briefs (Phase 5 work; schema lands now)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS briefs (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    customer_id     TEXT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    query_hash      TEXT NOT NULL,
    token_budget    INTEGER NOT NULL,
    token_count     INTEGER NOT NULL,
    strategy        TEXT NOT NULL,
    body            TEXT NOT NULL,
    segments        JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ,
    CHECK (token_count >= 0),
    CHECK (token_budget > 0),
    CHECK (token_count <= token_budget),
    CHECK (strategy IN ('recency', 'semantic', 'summarized', 'hybrid'))
);

CREATE INDEX IF NOT EXISTS idx_briefs_lookup
    ON briefs (tenant_id, customer_id, query_hash, strategy);

-- ---------------------------------------------------------------------------
-- 8. policies — tenant autonomy configuration (per action_type)
--    The four policy strategies (declarative / python / llm-judge / naive)
--    all read the same `body` JSONB; the engine variant interprets it.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS policies (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    action_type     TEXT NOT NULL,
    decision        TEXT NOT NULL,
    body            JSONB NOT NULL DEFAULT '{}'::jsonb,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (decision IN ('auto', 'approval_required', 'reject')),
    UNIQUE (tenant_id, action_type, version)
);

CREATE INDEX IF NOT EXISTS idx_policies_tenant_lookup
    ON policies (tenant_id, action_type, version DESC);

-- ---------------------------------------------------------------------------
-- 9. action_proposals — LLM proposals BEFORE policy application
--    proposed_by ∈ {llm, fallback} so degraded paths are visible (§5.5).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS action_proposals (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    event_id        TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    customer_id     TEXT REFERENCES customers(id) ON DELETE SET NULL,
    action_type     TEXT NOT NULL,
    proposed_by     TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (proposed_by IN ('llm', 'fallback'))
);

CREATE INDEX IF NOT EXISTS idx_proposals_event ON action_proposals (tenant_id, event_id);

-- ---------------------------------------------------------------------------
-- 10. actions — concrete actions (one per accepted proposal, with status)
--     status transitions: pending_policy → (auto: pending_exec | approval_required: pending_approval | rejected)
--     pending_approval → (approved → pending_exec | rejected)
--     pending_exec    → (executed | execution_failed)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS actions (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    proposal_id     TEXT NOT NULL REFERENCES action_proposals(id) ON DELETE CASCADE,
    event_id        TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    customer_id     TEXT REFERENCES customers(id) ON DELETE SET NULL,
    action_type     TEXT NOT NULL,
    status          TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    executed_at     TIMESTAMPTZ,
    CHECK (status IN (
        'pending_policy',
        'pending_approval',
        'pending_exec',
        'executed',
        'execution_failed',
        'rejected'
    ))
);

CREATE INDEX IF NOT EXISTS idx_actions_tenant_status ON actions (tenant_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_actions_event        ON actions (tenant_id, event_id);

-- ---------------------------------------------------------------------------
-- 11. approval_queue — pending-human-approval rows for actions
--     One queue, scoped per tenant. Race-condition handling lands Day 21.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS approval_queue (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    action_id       TEXT NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    state           TEXT NOT NULL DEFAULT 'pending',
    assigned_to     TEXT,
    enqueued_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_at      TIMESTAMPTZ,
    decided_by      TEXT,
    decision_note   TEXT,
    row_version     INTEGER NOT NULL DEFAULT 1,
    CHECK (state IN ('pending', 'approved', 'rejected', 'expired')),
    UNIQUE (tenant_id, action_id)
);

CREATE INDEX IF NOT EXISTS idx_approval_queue_pending
    ON approval_queue (tenant_id, state, enqueued_at)
    WHERE state = 'pending';

-- ---------------------------------------------------------------------------
-- 12. audit_log — append-only record of every decision
--     Append-only enforced by trigger in a follow-up migration on Day 4.
--     `caused_by_event_id` is a column (joinable), NOT a JSON field.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_log (
    id                      BIGSERIAL PRIMARY KEY,
    tenant_id               TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    ts                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind                    TEXT NOT NULL,
    action_id               TEXT REFERENCES actions(id) ON DELETE SET NULL,
    caused_by_event_id      TEXT REFERENCES events(id) ON DELETE SET NULL,
    actor_kind              TEXT NOT NULL,
    actor_id                TEXT,
    payload                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK (kind IN (
        'proposal',
        'decision',
        'approval',
        'rejection',
        'execution',
        'execution_failed',
        'policy_change'
    )),
    CHECK (actor_kind IN ('system', 'llm', 'human', 'fallback'))
);

CREATE INDEX IF NOT EXISTS idx_audit_tenant_ts    ON audit_log (tenant_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action       ON audit_log (tenant_id, action_id);
CREATE INDEX IF NOT EXISTS idx_audit_event        ON audit_log (tenant_id, caused_by_event_id);
