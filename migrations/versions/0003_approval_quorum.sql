-- PennyCore — N-of-M parallel-approval quorum columns (Day 26, Phase 5).
--
-- Extends approval_queue (migration 0001, table 11) so a row can require
-- more than one distinct approver before it resolves to 'approved'. Mirrors
-- the new fields on `contracts.policies.ApprovalRule`.
--
-- Semantics (enforced by orchestrator/approval_queue.py — this migration is
-- just the storage shape):
--   * required_approvals (N) — how many distinct approvers must approve.
--     Defaults to 1, which reproduces the Phase-2 single-approver behavior,
--     so the column is back-compatible with every existing row.
--   * eligible_approvers (the M pool) — JSONB array of approver IDs allowed
--     to vote. NULL means "any approver" (open pool).
--   * approvals — JSONB array of the distinct approver IDs that have voted
--     so far. The row stays 'pending' until
--     jsonb_array_length(approvals) >= required_approvals.
--
-- Rejection remains a veto: a single eligible rejection resolves the row,
-- so no rejection-tally column is needed.
--
-- Roll-forward only, and idempotent (ADD COLUMN IF NOT EXISTS) so a partially
-- applied migration can be re-run safely.

ALTER TABLE approval_queue
    ADD COLUMN IF NOT EXISTS required_approvals INTEGER NOT NULL DEFAULT 1;

ALTER TABLE approval_queue
    ADD COLUMN IF NOT EXISTS eligible_approvers JSONB;

ALTER TABLE approval_queue
    ADD COLUMN IF NOT EXISTS approvals JSONB NOT NULL DEFAULT '[]'::jsonb;

-- A quorum that exceeds its own eligible pool can never be reached. Reject
-- that misconfiguration at the storage layer too (the app rejects it at
-- enqueue time; this is defense-in-depth for direct SQL writes).
ALTER TABLE approval_queue
    DROP CONSTRAINT IF EXISTS chk_approval_quorum_sane;
ALTER TABLE approval_queue
    ADD CONSTRAINT chk_approval_quorum_sane CHECK (
        required_approvals >= 1
        AND (
            eligible_approvers IS NULL
            OR required_approvals <= jsonb_array_length(eligible_approvers)
        )
    );
