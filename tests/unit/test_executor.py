"""Tests for the Day-10 mock action executor.

Coverage:
  1. Each registered ActionType dispatches and produces an executed
     action (status=executed, executed_at set, _executed_payload merged).
  2. Default executor covers every ActionType in the enum.
  3. Custom executor_table overrides individual handlers.
  4. Unknown action_type (post-registration removal) raises
     ExecutionError.
  5. Executor that raises propagates to the caller (pipeline-side
     responsibility to catch + audit).
  6. Executor that returns a non-dict raises ExecutionError.
  7. Existing action payload survives merge.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from contracts.actions import Action, ActionStatus, ActionType
from orchestrator.executor import (
    DEFAULT_EXECUTORS,
    ActionExecutor,
    ExecutionError,
)


def _make_action(
    action_type: ActionType,
    *,
    payload: dict[str, Any] | None = None,
) -> Action:
    return Action(
        id=f"act_test_{action_type.value}",
        tenant_id="tenant-x",
        proposal_id="prop_test",
        event_id="evt_test",
        customer_id=None,
        action_type=action_type,
        status=ActionStatus.PENDING_EXEC,
        payload=payload or {},
    )


@pytest.mark.parametrize("action_type", list(ActionType))
def test_default_executor_handles_every_action_type(
    action_type: ActionType,
) -> None:
    """If a new ActionType is added without registering an executor,
    this test fails — exactly the regression we want to catch."""
    assert action_type in DEFAULT_EXECUTORS
    executor = ActionExecutor()
    result = executor.execute(_make_action(action_type))
    assert result.status is ActionStatus.EXECUTED
    assert result.executed_at is not None
    assert result.executed_at.tzinfo is not None
    assert "_executed_payload" in result.payload


def test_executor_merges_existing_payload() -> None:
    executor = ActionExecutor()
    action = _make_action(
        ActionType.SEND_BORROWER_MESSAGE,
        payload={"message": "Hello Jane", "loan_id": "L-001"},
    )
    result = executor.execute(action)
    # Original keys preserved.
    assert result.payload["message"] == "Hello Jane"
    assert result.payload["loan_id"] == "L-001"
    # New key added.
    assert result.payload["_executed_payload"]["message"] == "Hello Jane"


def test_custom_executor_table_overrides_handler() -> None:
    captured: list[Action] = []

    def custom(action: Action) -> dict[str, Any]:
        captured.append(action)
        return {"channel": "custom", "marker": "ZZZ"}

    table = dict(DEFAULT_EXECUTORS)
    table[ActionType.SEND_BORROWER_MESSAGE] = custom
    executor = ActionExecutor(executor_table=table)
    result = executor.execute(_make_action(ActionType.SEND_BORROWER_MESSAGE))
    assert captured  # custom handler ran
    assert result.payload["_executed_payload"]["marker"] == "ZZZ"


def test_unknown_action_type_raises_execution_error() -> None:
    """Drop an executor from a custom table and verify the dispatcher
    surfaces a clear error rather than KeyErroring."""
    table = dict(DEFAULT_EXECUTORS)
    del table[ActionType.NO_OP]
    executor = ActionExecutor(executor_table=table)
    with pytest.raises(ExecutionError, match="no executor registered"):
        executor.execute(_make_action(ActionType.NO_OP))


def test_executor_exception_propagates() -> None:
    def bad(_action: Action) -> dict[str, Any]:
        raise RuntimeError("downstream unavailable")

    table = dict(DEFAULT_EXECUTORS)
    table[ActionType.SEND_BORROWER_MESSAGE] = bad
    executor = ActionExecutor(executor_table=table)
    with pytest.raises(RuntimeError, match="downstream unavailable"):
        executor.execute(_make_action(ActionType.SEND_BORROWER_MESSAGE))


def test_executor_returning_non_dict_raises_execution_error() -> None:
    def bad(_action: Action) -> Any:
        return "not a dict"  # type: ignore[return-value]

    table = dict(DEFAULT_EXECUTORS)
    table[ActionType.NO_OP] = bad
    executor = ActionExecutor(executor_table=table)
    with pytest.raises(ExecutionError, match="returned str"):
        executor.execute(_make_action(ActionType.NO_OP))


def test_executed_at_is_after_creation() -> None:
    executor = ActionExecutor()
    before = datetime.now(timezone.utc)
    result = executor.execute(_make_action(ActionType.SEND_BORROWER_MESSAGE))
    assert result.executed_at is not None
    assert result.executed_at >= before
