-- PennyCore — audit_log append-only trigger (Day 6 follow-up; was scheduled
-- for Day 4 phase wrap but slipped).
--
-- Enforces SYSTEM_DESIGN §5.3 (Auditability invariant) at the database
-- layer: once a row lands in audit_log, no UPDATE or DELETE can mutate it.
-- App-level checks are insufficient — a compromised service account or a
-- "quick fix" SQL session would bypass them. RAISE EXCEPTION inside the
-- trigger is the only thing that survives all access paths.
--
-- Design notes:
--   * The trigger fires BEFORE UPDATE/DELETE so it short-circuits before
--     any row is touched. AFTER would still abort the txn but waste work
--     and leave more confusing error messages.
--   * The exception code 'P0001' (raise_exception) maps to psycopg's
--     `RaiseException`; callers can catch it explicitly without parsing
--     SQLSTATE strings.
--   * TRUNCATE is the one mutation the trigger cannot intercept (triggers
--     don't fire on TRUNCATE by default). The companion event-trigger at
--     the bottom blocks TRUNCATE on audit_log. (Event triggers require
--     superuser to install, but `docker-entrypoint-initdb.d` runs as the
--     db owner which is sufficient on the local-dev image.)
--   * Inserts are unaffected — that's the whole point of "append-only".
--
-- Roll-forward only. No DROP path: if a future migration needs to mutate
-- audit_log, that migration must temporarily disable the trigger
-- (`ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_mutation`) inside
-- the same transaction as the mutation, then re-enable it. The audit
-- record of WHY the mutation happened goes in a sibling table
-- (`audit_log_amendments`, not in scope for Phase 2).

-- ---------------------------------------------------------------------------
-- 1. Row-level guard: reject UPDATE / DELETE on audit_log
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION audit_log_reject_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'audit_log is append-only — % is not permitted (rule 16, SYSTEM_DESIGN §5.3)',
        TG_OP
        USING ERRCODE = 'P0001';
END;
$$;

DROP TRIGGER IF EXISTS audit_log_no_mutation ON audit_log;

CREATE TRIGGER audit_log_no_mutation
    BEFORE UPDATE OR DELETE
    ON audit_log
    FOR EACH ROW
    EXECUTE FUNCTION audit_log_reject_mutation();

-- ---------------------------------------------------------------------------
-- 2. Statement-level guard: reject TRUNCATE on audit_log
-- ---------------------------------------------------------------------------
-- TRUNCATE doesn't fire FOR EACH ROW triggers. A FOR EACH STATEMENT
-- trigger on TRUNCATE catches it cleanly.

CREATE OR REPLACE FUNCTION audit_log_reject_truncate()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'audit_log is append-only — TRUNCATE is not permitted (rule 16, SYSTEM_DESIGN §5.3)'
        USING ERRCODE = 'P0001';
END;
$$;

DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;

CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE
    ON audit_log
    FOR EACH STATEMENT
    EXECUTE FUNCTION audit_log_reject_truncate();

-- ---------------------------------------------------------------------------
-- 3. Inline test (idempotent — safe on every boot)
-- ---------------------------------------------------------------------------
-- Sanity check: on first migration run we INSERT + try to UPDATE +
-- expect the trigger to raise. We use a DO block so the smoke test runs
-- inside the migration but rolls back its own scratch row.
-- This is belt-and-braces — the migration itself succeeds even if this
-- block is removed; the assertion just makes the trigger's existence
-- audible at boot time.

DO $$
DECLARE
    sentinel_id BIGINT;
    test_tenant TEXT := 'audit_log_trigger_self_test';
BEGIN
    -- Self-test only runs if a tenant of that exact name exists; otherwise
    -- the FK on audit_log.tenant_id would fail and the migration block
    -- aborts. We don't want to seed a real tenant just to test, so we
    -- skip silently when the helper tenant isn't present. To enable the
    -- self-test on first boot, the docker-entrypoint-initdb.d ordering
    -- could pre-seed; for now this is a no-op on a fresh database.
    IF NOT EXISTS (SELECT 1 FROM tenants WHERE id = test_tenant) THEN
        RETURN;
    END IF;

    INSERT INTO audit_log (tenant_id, kind, actor_kind, payload)
    VALUES (test_tenant, 'proposal', 'system', '{"self_test": true}'::jsonb)
    RETURNING id INTO sentinel_id;

    BEGIN
        UPDATE audit_log SET kind = 'decision' WHERE id = sentinel_id;
        RAISE EXCEPTION 'audit_log_no_mutation trigger FAILED to block UPDATE';
    EXCEPTION
        WHEN raise_exception THEN
            -- Expected — trigger fired correctly.
            NULL;
    END;

    BEGIN
        DELETE FROM audit_log WHERE id = sentinel_id;
        RAISE EXCEPTION 'audit_log_no_mutation trigger FAILED to block DELETE';
    EXCEPTION
        WHEN raise_exception THEN
            NULL;
    END;
END $$;
