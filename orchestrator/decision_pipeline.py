"""Decision pipeline — wires planner → policy → executor/queue → audit
(Day 10, Phase 2).

This is the orchestrator's "spine". Given an event envelope, it:

  1. Calls the planner (Day 9) to produce an `ActionProposal`.
  2. Consults the policy engine (this Day 10) for a `PolicyDecision`.
  3. Creates the canonical `Action` row with the right initial
     status — `pending_exec` for `auto`, `pending_approval` for
     `approval_required`, `rejected` for `reject`.
  4. If `auto`: invokes the executor and transitions the action to
     `executed` (or `execution_failed`).
  5. If `approval_required`: enqueues to the approval queue.
  6. If `reject`: leaves the action at `rejected`.
  7. Writes audit rows at every transition.

## Idempotency (rule 16 — every action proposal writes audit; also
external take-home assessment scenario 4 — duplicate events)

The pipeline is keyed by `(tenant_id, event_id)`. If an event arrives
a second time with the same composite key, the pipeline returns the
already-created actions instead of re-running planning + policy. This
satisfies the external scorecard's idempotency check AND keeps the
audit log honest — one event, one set of proposals, one set of
actions, regardless of redelivery.

The dedup index is in-memory for Day 10 (same justification as the
approval queue — the takehome adapter is `__init__()`-only and can't
reach a DB). Day 19 (idempotency hardening) backs this with a
Postgres `(tenant_id, idempotency_key) UNIQUE` constraint per
SYSTEM_DESIGN §5.1.

## Tenant isolation (rule 15)

Every method takes `tenant_id` or derives it from the event envelope.
Action IDs are globally unique UUIDs (no tenant prefix needed — the
takehome scenario 5 specifically asserts no overlap between tenants'
action IDs). The action store + approval queue are tenant-aware
themselves; the pipeline composes them, it doesn't shortcut their
tenant guards.

## Audit invariant (rule 16)

Every state transition writes an `AuditLogEntry`. Audit kinds used
here:

  * `PROPOSAL`         — planner emitted a proposal.
  * `DECISION`         — policy engine returned a decision.
  * `APPROVAL`         — pending action transitioned to approved.
  * `REJECTION`        — pending action transitioned to rejected.
  * `EXECUTION`        — action successfully executed (mock).
  * `EXECUTION_FAILED` — mock executor raised (failure-injection path).

The audit log is the source of truth for the action's
status-transition history; the `audit_trail` field surfaced on the
adapter's action dict is just a denormalized view of these rows.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from contracts.actions import Action, ActionProposal, ActionStatus, ActionType
from contracts.audit import AuditActorKind, AuditKind, AuditLogEntry
from contracts.policies import ApprovalRule, PolicyDecision
from orchestrator.approval_queue import (
    ApprovalNotFoundError,
    ApprovalQueue,
    InMemoryApprovalQueue,
)
from orchestrator.audit import AuditLog, InMemoryAuditLog
from orchestrator.executor import ActionExecutor
from orchestrator.policy import DeclarativePolicyEngine, PolicyEngine
from orchestrator.quorum import QuorumPolicy

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-memory action store. Mirrors the `actions` Postgres table; replaced by
# `PostgresActionStore` in Phase 4. Kept here (not in its own file) because
# nothing else uses it on the Day-10 surface — the production wiring imports
# the store + pipeline together.
# ---------------------------------------------------------------------------


class ActionNotFoundError(LookupError):
    """Raised when the pipeline tries to look up an action by an ID
    that was never produced. Distinct from `ApprovalNotFoundError`
    (which is for queue rows) so callers can disambiguate."""


class InMemoryActionStore:
    """Thread-safe dict of action_id → Action with a tenant index."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_id: dict[str, Action] = {}
        self._by_tenant: dict[str, list[str]] = {}

    def put(self, action: Action) -> None:
        with self._lock:
            existed = action.id in self._by_id
            self._by_id[action.id] = action
            if not existed:
                self._by_tenant.setdefault(action.tenant_id, []).append(
                    action.id
                )

    def get(self, action_id: str) -> Action:
        with self._lock:
            try:
                return self._by_id[action_id]
            except KeyError as exc:
                raise ActionNotFoundError(
                    f"no action with id={action_id!r}"
                ) from exc

    def list_for_tenant(self, tenant_id: str) -> list[Action]:
        with self._lock:
            return [
                self._by_id[a]
                for a in self._by_tenant.get(tenant_id, [])
                if a in self._by_id
            ]

    def clear(self) -> None:
        with self._lock:
            self._by_id.clear()
            self._by_tenant.clear()


def _default_action_id() -> str:
    return f"act_{uuid.uuid4().hex}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# DecisionPipeline
# ---------------------------------------------------------------------------


@dataclass
class _DedupKey:
    tenant_id: str
    event_id: str

    def __hash__(self) -> int:  # dataclass generates __eq__; we need hash
        return hash((self.tenant_id, self.event_id))


class DecisionPipeline:
    """Stateful composition of planner output + policy + queue + executor + audit.

    Construct with explicit dependencies for testing; the default
    factory `make_default_pipeline()` wires the in-memory variants of
    each. Phase 4 hardening introduces a `make_postgres_pipeline()`
    factory that swaps in DB-backed implementations behind the same
    Protocols.
    """

    def __init__(
        self,
        policy: PolicyEngine,
        queue: ApprovalQueue,
        audit_log: AuditLog,
        executor: ActionExecutor | None = None,
        actions: InMemoryActionStore | None = None,
        quorum: QuorumPolicy | None = None,
    ) -> None:
        self.policy = policy
        self.queue = queue
        self.audit = audit_log
        self.executor = executor or ActionExecutor()
        self.actions = actions or InMemoryActionStore()
        # Quorum config for parallel approvals (Day 26). An empty policy
        # means every approval_required action needs a single approver —
        # the Phase-2 behavior — so this is invisible until a tenant
        # opts in via `quorum.set_rule(...)`.
        self.quorum = quorum or QuorumPolicy()
        self._lock = threading.RLock()
        # Idempotency index: (tenant_id, event_id) -> list[action_id]
        self._dedup: dict[_DedupKey, list[str]] = {}

    # ------------------------------------------------------------------
    # Primary entry point — invoked from `make_decision_handler`
    # ------------------------------------------------------------------

    def handle_proposal(self, proposal: ActionProposal) -> list[Action]:
        """Drive a single `ActionProposal` through the full pipeline.

        Returns the resulting list of `Action` rows. Today the
        planner emits exactly ONE proposal per event (see
        `orchestrator/planner.py` design notes), so the list always
        has one element on the first call. On a duplicate event, the
        list mirrors the cached previous result.
        """
        key = _DedupKey(tenant_id=proposal.tenant_id, event_id=proposal.event_id)
        with self._lock:
            cached_ids = self._dedup.get(key)
            if cached_ids is not None:
                return [self.actions.get(a) for a in cached_ids]

            action = self._materialize_action(proposal)
            self.actions.put(action)
            self._write_audit(
                kind=AuditKind.PROPOSAL,
                tenant_id=proposal.tenant_id,
                action_id=action.id,
                event_id=proposal.event_id,
                actor_kind=(
                    AuditActorKind.LLM
                    if proposal.proposed_by.value == "llm"
                    else AuditActorKind.FALLBACK
                ),
                payload={
                    "proposed_by": proposal.proposed_by.value,
                    "action_type": proposal.action_type.value,
                    "reasoning": proposal.payload.get("_planner_reasoning", ""),
                },
            )

            decision = self.policy.decide(
                proposal.tenant_id, proposal.action_type
            )
            self._write_audit(
                kind=AuditKind.DECISION,
                tenant_id=proposal.tenant_id,
                action_id=action.id,
                event_id=proposal.event_id,
                actor_kind=AuditActorKind.SYSTEM,
                payload={
                    "decision": decision.value,
                    "action_type": proposal.action_type.value,
                },
            )

            resolved = self._apply_decision(action, decision)
            self._dedup[key] = [resolved.id]
            return [resolved]

    # ------------------------------------------------------------------
    # Human approval / rejection — invoked from the /approvals API
    # and from the takehome adapter's approve_action / reject_action.
    # ------------------------------------------------------------------

    def approve_action(
        self,
        action_id: str,
        *,
        decided_by: str = "human",
        expected_tenant_id: str | None = None,
    ) -> Action:
        """Record one approval vote; execute the action once quorum is
        reached.

        For a single-approver action (the default) the first vote reaches
        quorum and the action executes immediately — identical to the
        Phase-2 behavior. For an N-of-M action, an early vote is recorded
        and the action stays `pending_approval`; the action only executes
        on the vote that completes the quorum.

        Every vote — partial or quorum-completing — writes an `APPROVAL`
        audit row carrying the running tally (`approvals_recorded`,
        `required_approvals`, `quorum_reached`), so a compliance officer
        can reconstruct exactly who signed off and in what order.

        Raises:
          * `ActionNotFoundError` if action_id is unknown OR (when
             `expected_tenant_id` is supplied) the action belongs to
             a different tenant. Returning the same exception on a
             cross-tenant attempt is deliberate: a 404 from the HTTP
             layer reveals nothing about whether the action exists
             elsewhere in the system. The Day 20 multi-tenant
             hardening tests assert this property.
          * `ApprovalNotFoundError` if no queue row exists (the action
             was auto-executed or rejected at policy time).
          * `ApprovalStateError` if the queue row is already resolved
             (double-approve / vote after quorum).
          * `ApproverNotEligibleError` if `decided_by` is outside the
             action's eligible-approver pool.
          * `DuplicateApproverError` if `decided_by` already voted on
             this action.
        """
        with self._lock:
            action = self.actions.get(action_id)
            if (
                expected_tenant_id is not None
                and action.tenant_id != expected_tenant_id
            ):
                raise ActionNotFoundError(
                    f"no action with id={action_id!r}"
                )
            # The queue raises if not pending / ineligible / duplicate.
            # Single-approver rows resolve here; quorum rows may return
            # still-pending after recording the vote.
            rule = self.queue.approve(action_id, decided_by=decided_by)
            quorum_reached = rule.state == "approved"

            self._write_audit(
                kind=AuditKind.APPROVAL,
                tenant_id=action.tenant_id,
                action_id=action.id,
                event_id=action.event_id,
                actor_kind=AuditActorKind.HUMAN,
                actor_id=decided_by,
                payload={
                    "prior_status": action.status.value,
                    "approvals_recorded": len(rule.approvals),
                    "required_approvals": rule.required_approvals,
                    "quorum_reached": quorum_reached,
                },
            )

            if not quorum_reached:
                # Partial quorum — the action stays pending_approval; no
                # execution until the final approver votes. Return the
                # action unchanged so the caller sees it still pending.
                return action

            # Quorum reached: pending_approval -> pending_exec -> executed.
            promoted = action.model_copy(
                update={
                    "status": ActionStatus.PENDING_EXEC,
                    "updated_at": _now(),
                }
            )
            self.actions.put(promoted)
            return self._execute(promoted)

    def approval_rule(self, action_id: str) -> ApprovalRule | None:
        """Return the queue row (with its quorum tally) for an action, or
        ``None`` if the action was never queued for approval. Read-only —
        the admin UI and the HTTP `_action_view` use it to render approval
        progress (`approvals_remaining`, who has voted)."""
        return self.queue.get(action_id)

    def reject_action(
        self,
        action_id: str,
        *,
        reason: str = "",
        decided_by: str = "human",
        expected_tenant_id: str | None = None,
    ) -> Action:
        """Reject a pending action.

        Same error contract as `approve_action`. The rejected action
        stays in the action store with `status=rejected`; the queue
        row stays with `state=rejected` for the audit trail. When
        `expected_tenant_id` is supplied, a cross-tenant attempt
        raises `ActionNotFoundError` rather than mutating state — the
        Day 20 multi-tenant isolation invariant.
        """
        with self._lock:
            action = self.actions.get(action_id)
            if (
                expected_tenant_id is not None
                and action.tenant_id != expected_tenant_id
            ):
                raise ActionNotFoundError(
                    f"no action with id={action_id!r}"
                )
            self.queue.reject(
                action_id, reason=reason, decided_by=decided_by
            )

            self._write_audit(
                kind=AuditKind.REJECTION,
                tenant_id=action.tenant_id,
                action_id=action.id,
                event_id=action.event_id,
                actor_kind=AuditActorKind.HUMAN,
                actor_id=decided_by,
                payload={
                    "prior_status": action.status.value,
                    "reason": reason,
                },
            )

            rejected = action.model_copy(
                update={
                    "status": ActionStatus.REJECTED,
                    "updated_at": _now(),
                }
            )
            self.actions.put(rejected)
            return rejected

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    def get_action(
        self, action_id: str, *, expected_tenant_id: str | None = None
    ) -> Action:
        """Fetch an action by id. When `expected_tenant_id` is supplied
        and the action belongs to a different tenant, raises
        `ActionNotFoundError` (same shape as a true miss) so the HTTP
        layer's 404 reveals nothing about cross-tenant existence."""
        action = self.actions.get(action_id)
        if (
            expected_tenant_id is not None
            and action.tenant_id != expected_tenant_id
        ):
            raise ActionNotFoundError(
                f"no action with id={action_id!r}"
            )
        return action

    def list_actions_for_tenant(self, tenant_id: str) -> list[Action]:
        return self.actions.list_for_tenant(tenant_id)

    def list_pending_actions(self, tenant_id: str) -> list[Action]:
        """Pending = action store filtered to `status=pending_approval`,
        ordered by the queue's FIFO order. The queue is the source of
        truth for which actions are pending; the store provides the
        full action snapshot."""
        rules = self.queue.list_pending(tenant_id)
        out: list[Action] = []
        for rule in rules:
            try:
                out.append(self.actions.get(rule.action_id))
            except ActionNotFoundError:
                # Queue row references an action we don't have. This
                # should be impossible in single-process mode (we
                # always create the action before enqueueing), but if
                # it ever happens we want the missing row to surface
                # in logs rather than crash the API.
                _LOG.warning(
                    "approval queue references unknown action_id=%r — skipping",
                    rule.action_id,
                )
        return out

    def audit_for_action(self, action_id: str) -> list[AuditLogEntry]:
        return self.audit.entries_for_action(action_id)

    def clear(self) -> None:
        """Drop all in-memory state. Tests use this between scenarios."""
        with self._lock:
            self._dedup.clear()
            if hasattr(self.actions, "clear"):
                self.actions.clear()
            if hasattr(self.queue, "clear"):
                self.queue.clear()
            if hasattr(self.audit, "clear"):
                self.audit.clear()
            self.quorum.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _materialize_action(self, proposal: ActionProposal) -> Action:
        """Turn an `ActionProposal` into a fresh `Action` row at
        `status=pending_policy`. The pipeline immediately transitions
        out of pending_policy via `_apply_decision`."""
        return Action(
            id=_default_action_id(),
            tenant_id=proposal.tenant_id,
            proposal_id=proposal.id,
            event_id=proposal.event_id,
            customer_id=proposal.customer_id,
            action_type=proposal.action_type,
            status=ActionStatus.PENDING_POLICY,
            payload=dict(proposal.payload),
        )

    def _apply_decision(
        self, action: Action, decision: PolicyDecision
    ) -> Action:
        if decision is PolicyDecision.REJECT:
            rejected = action.model_copy(
                update={
                    "status": ActionStatus.REJECTED,
                    "updated_at": _now(),
                }
            )
            self.actions.put(rejected)
            self._write_audit(
                kind=AuditKind.REJECTION,
                tenant_id=rejected.tenant_id,
                action_id=rejected.id,
                event_id=rejected.event_id,
                actor_kind=AuditActorKind.SYSTEM,
                payload={"reason": "policy=reject", "by": "policy_engine"},
            )
            return rejected

        if decision is PolicyDecision.APPROVAL_REQUIRED:
            queued = action.model_copy(
                update={
                    "status": ActionStatus.PENDING_APPROVAL,
                    "updated_at": _now(),
                }
            )
            self.actions.put(queued)
            quorum_rule = self.quorum.resolve(queued.tenant_id, queued.action_type)
            self.queue.enqueue(
                action_id=queued.id,
                tenant_id=queued.tenant_id,
                required_approvals=quorum_rule.required_approvals,
                eligible_approvers=(
                    list(quorum_rule.eligible_approvers)
                    if quorum_rule.eligible_approvers is not None
                    else None
                ),
            )
            return queued

        # PolicyDecision.AUTO — straight to execution.
        pending_exec = action.model_copy(
            update={
                "status": ActionStatus.PENDING_EXEC,
                "updated_at": _now(),
            }
        )
        self.actions.put(pending_exec)
        return self._execute(pending_exec)

    def _execute(self, action: Action) -> Action:
        """Run the action through the mock executor. On exception,
        transition to `EXECUTION_FAILED` and write the failure audit
        row. The original exception is NOT re-raised — callers see
        the `EXECUTION_FAILED` action and can inspect the audit log
        for the cause. Re-raising would force every caller to wrap
        the pipeline in try/except, which would be a footgun."""
        try:
            executed = self.executor.execute(action)
        except Exception as exc:
            now = _now()
            failed = action.model_copy(
                update={
                    "status": ActionStatus.EXECUTION_FAILED,
                    "updated_at": now,
                }
            )
            self.actions.put(failed)
            self._write_audit(
                kind=AuditKind.EXECUTION_FAILED,
                tenant_id=action.tenant_id,
                action_id=action.id,
                event_id=action.event_id,
                actor_kind=AuditActorKind.SYSTEM,
                payload={
                    "error_kind": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            _LOG.warning(
                "executor raised for action=%s (%s: %s)",
                action.id,
                type(exc).__name__,
                exc,
            )
            return failed

        self.actions.put(executed)
        self._write_audit(
            kind=AuditKind.EXECUTION,
            tenant_id=executed.tenant_id,
            action_id=executed.id,
            event_id=executed.event_id,
            actor_kind=AuditActorKind.SYSTEM,
            payload={
                "executed_payload": executed.payload.get("_executed_payload", {}),
            },
        )
        return executed

    def _write_audit(
        self,
        *,
        kind: AuditKind,
        tenant_id: str,
        action_id: str | None,
        event_id: str | None,
        actor_kind: AuditActorKind,
        actor_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        entry = AuditLogEntry(
            tenant_id=tenant_id,
            kind=kind,
            action_id=action_id,
            caused_by_event_id=event_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            payload=payload or {},
        )
        self.audit.write(entry)


# ---------------------------------------------------------------------------
# Default factory + handler integration for the listener chain
# ---------------------------------------------------------------------------


def make_default_pipeline() -> DecisionPipeline:
    """In-memory pipeline. Used by the FastAPI app, by the takehome
    adapter, and by unit tests that need a real pipeline rather than a
    mock collaborator."""
    return DecisionPipeline(
        policy=DeclarativePolicyEngine(),
        queue=InMemoryApprovalQueue(),
        audit_log=InMemoryAuditLog(),
    )
