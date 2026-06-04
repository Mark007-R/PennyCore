"""Mock action executors (Day 10, Phase 2).

When the policy engine returns `AUTO` (or a human approves a pending
action), the DecisionPipeline calls `ActionExecutor.execute(action)`.
The executor dispatches by `action_type` to a mock implementation that
logs the intent instead of actually sending an email / SMS / etc.

## Why mocks, not real integrations

The PennyCore portfolio narrative is the orchestrator pattern itself —
event → plan → policy → audit. Wiring a real Twilio / SendGrid /
loan-management-system integration is a separate engineering effort
that hiring managers can see is "drop in three SDK calls" once the
orchestrator pattern is in place. Mocks keep the demo runnable
offline, keep the test suite hermetic, and let the Phase 3 + Phase 5
benchmarks measure orchestration performance without conflating it
with third-party API latency.

Each mock executor:

  * Logs the intent at INFO level so the docker-compose tail surfaces
    the action in plain English ("Sent borrower message: ...").
  * Records the mock-execution payload (recipient, subject, body) on
    the returned action's payload under `_executed_payload`, which
    the admin UI (Day 31) and the audit log surface to compliance.
  * Returns the (action.id, executed_payload) tuple so the caller can
    construct the next state-machine row without re-reading the action.

## Failure injection

The default executor never fails — every mock returns success. Tests
can inject a custom `executor_table` via the `ActionExecutor`
constructor to simulate `EXECUTION_FAILED` paths (used by Day-23
hardening tests). The DecisionPipeline catches exceptions from the
executor and writes an `EXECUTION_FAILED` audit row + sets the
action's status; the executor itself just raises on failure.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from contracts.actions import Action, ActionStatus, ActionType
from contracts.observability import trace_span

_LOG = logging.getLogger(__name__)


# An executor function takes the Action and returns the
# "execution payload" — whatever the mock decided to "send" / "do".
# The DecisionPipeline merges that into the action's payload under
# `_executed_payload` (see `ActionExecutor.execute`).
ExecutorFn = Callable[[Action], dict[str, Any]]


def _mock_send_borrower_message(action: Action) -> dict[str, Any]:
    message = action.payload.get("message") or (
        f"Acknowledged: we received your message for loan "
        f"{action.payload.get('loan_id', '(unknown)')}."
    )
    _LOG.info(
        "mock-executor send_borrower_message: tenant=%s action=%s message=%r",
        action.tenant_id,
        action.id,
        message,
    )
    return {
        "channel": "chat",
        "recipient": "borrower",
        "message": message,
    }


def _mock_notify_loan_officer(action: Action) -> dict[str, Any]:
    reason = action.payload.get("reason") or action.payload.get(
        "_planner_reasoning",
        "Event flagged for loan-officer review.",
    )
    _LOG.info(
        "mock-executor notify_loan_officer: tenant=%s action=%s reason=%r",
        action.tenant_id,
        action.id,
        reason,
    )
    return {
        "channel": "internal",
        "recipient": "loan_officer",
        "reason": reason,
    }


def _mock_schedule_call(action: Action) -> dict[str, Any]:
    when = action.payload.get("scheduled_for", "next available slot")
    _LOG.info(
        "mock-executor schedule_call: tenant=%s action=%s when=%r",
        action.tenant_id,
        action.id,
        when,
    )
    return {
        "channel": "voice",
        "scheduled_for": when,
    }


def _mock_request_document(action: Action) -> dict[str, Any]:
    document_type = action.payload.get("document_type", "missing document")
    _LOG.info(
        "mock-executor request_document: tenant=%s action=%s document_type=%r",
        action.tenant_id,
        action.id,
        document_type,
    )
    return {
        "channel": "email",
        "recipient": "borrower",
        "document_type": document_type,
    }


def _mock_update_status(action: Action) -> dict[str, Any]:
    new_status = action.payload.get("new_status", "in_review")
    _LOG.info(
        "mock-executor update_status: tenant=%s action=%s new_status=%r",
        action.tenant_id,
        action.id,
        new_status,
    )
    return {
        "channel": "internal",
        "new_status": new_status,
    }


def _mock_no_op(action: Action) -> dict[str, Any]:
    _LOG.debug(
        "mock-executor no_op: tenant=%s action=%s (no side effect)",
        action.tenant_id,
        action.id,
    )
    return {"channel": "noop"}


# Default dispatch table. The DecisionPipeline calls these by
# action_type; tests override individual entries to exercise the
# `EXECUTION_FAILED` path without restructuring the pipeline.
DEFAULT_EXECUTORS: dict[ActionType, ExecutorFn] = {
    ActionType.SEND_BORROWER_MESSAGE: _mock_send_borrower_message,
    ActionType.NOTIFY_LOAN_OFFICER: _mock_notify_loan_officer,
    ActionType.SCHEDULE_CALL: _mock_schedule_call,
    ActionType.REQUEST_DOCUMENT: _mock_request_document,
    ActionType.UPDATE_STATUS: _mock_update_status,
    ActionType.NO_OP: _mock_no_op,
}


class ExecutionError(RuntimeError):
    """Raised by `ActionExecutor.execute` when the mock dispatcher
    cannot resolve a handler for the action_type. Distinct from a
    handler-raised exception (those propagate as-is so the pipeline
    can write an `EXECUTION_FAILED` audit row with the original cause)."""


class ActionExecutor:
    """Dispatches an `Action` to its mock implementation and returns
    the post-execution Action.

    `executor_table` defaults to `DEFAULT_EXECUTORS`. Override per-type
    to inject failure modes or instrument calls in tests.

    The returned `Action` has:
      * `status = ActionStatus.EXECUTED`
      * `executed_at` set to now (UTC)
      * `payload["_executed_payload"]` populated with the mock's
        "what it would have sent" dict (the audit log carries this
        too, but the action itself is the source of truth that the
        UI surfaces).
    """

    def __init__(
        self,
        executor_table: dict[ActionType, ExecutorFn] | None = None,
    ) -> None:
        self._table = dict(executor_table) if executor_table else dict(
            DEFAULT_EXECUTORS
        )

    def execute(self, action: Action) -> Action:
        """Run the mock executor for `action`. Returns the
        post-execution Action.

        On unknown `action_type`, raises `ExecutionError`. On handler
        exception, the original exception propagates — the
        DecisionPipeline catches both and writes the appropriate
        audit row.

        Day-30 observability: every execute call is one span
        (`pennycore.executor.execute`) carrying tenant_id, action_id,
        action_type, and (on success) the executed-at timestamp. The
        span sits inside the decision_pipeline's handle_proposal span
        so a Jaeger trace makes the "event → decision → execution"
        flow visible end-to-end. Failed executions show as red spans
        with `error.type` set per the OTel semantic convention.
        """
        with trace_span(
            "pennycore.executor.execute",
            tenant_id=action.tenant_id,
            attributes={
                "pennycore.action_id": action.id,
                "pennycore.action_type": action.action_type.value,
                "pennycore.event_id": action.event_id,
            },
        ) as span:
            handler = self._table.get(action.action_type)
            if handler is None:
                raise ExecutionError(
                    f"no executor registered for action_type={action.action_type.value!r}"
                )

            result = handler(action)
            if not isinstance(result, dict):
                raise ExecutionError(
                    f"executor for {action.action_type.value!r} returned "
                    f"{type(result).__name__}, expected dict"
                )

            now = datetime.now(timezone.utc)
            merged_payload = {**action.payload, "_executed_payload": result}
            span.set_attribute("pennycore.executed", True)
            return action.model_copy(
                update={
                    "status": ActionStatus.EXECUTED,
                    "executed_at": now,
                    "updated_at": now,
                    "payload": merged_payload,
                }
            )
