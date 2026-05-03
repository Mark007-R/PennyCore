#!/usr/bin/env python3
"""
Autonomy-Gated Agent Orchestrator — Evaluator

Usage:
    python evaluate.py your_module.YourClass

This script defines the AgentOrchestrator protocol, imports your implementation,
and runs behavioral scenarios against it. It tests structural properties — not
exact LLM outputs.

Do not modify this file.
"""

import asyncio
import importlib
import sys
import time
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Protocol — your class must subclass this
# ---------------------------------------------------------------------------


class AgentOrchestrator(ABC):
    """
    Minimal interface for the autonomy-gated agent orchestrator.

    The evaluator calls these methods to drive scenarios. How you implement
    the internals — event schemas, state management, policy format, LLM
    integration — is entirely up to you.
    """

    @abstractmethod
    async def handle_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Process a domain event and return proposed actions.

        The event dict will always contain at minimum:
            - "event_type": str (e.g. "document_uploaded", "message_received")
            - "tenant_id": str

        It may also contain: "event_id", "loan_id", "party_id", "payload",
        "timestamp", and other fields your system defines.

        Returns a list of action dicts. Each action must contain at minimum:
            - "action_id": str (unique)
            - "action_type": str
            - "status": str

        Actions may contain any additional fields (reasoning, metadata, etc.)
        """
        ...

    @abstractmethod
    async def configure_policies(
        self, tenant_id: str, policies: dict[str, Any]
    ) -> None:
        """
        Set autonomy policies for a tenant.

        The policies dict maps action types to autonomy levels. The exact
        format and expressiveness of this mapping is a design decision.

        The evaluator will pass simple policies like:
            {"send_email": "auto", "update_record": "approval_required"}

        Your engine may support richer formats (conditional rules, thresholds,
        wildcards, defaults, etc.)
        """
        ...

    @abstractmethod
    async def get_pending_actions(
        self, tenant_id: str
    ) -> list[dict[str, Any]]:
        """Return all actions for this tenant awaiting human approval."""
        ...

    @abstractmethod
    async def approve_action(self, action_id: str) -> dict[str, Any]:
        """
        Approve a pending action. Returns the updated action dict.
        Should raise an exception if the action is not in an approvable state.
        """
        ...

    @abstractmethod
    async def reject_action(
        self, action_id: str, reason: str = ""
    ) -> dict[str, Any]:
        """
        Reject a pending action. Returns the updated action dict.
        Should raise an exception if the action is not in a rejectable state.
        """
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_auto_status(status: str) -> bool:
    """Check if a status string indicates auto-approved/auto-executed."""
    s = status.lower()
    return "auto" in s or s in ("approved", "executed", "completed")


def _is_pending_status(status: str) -> bool:
    """Check if a status string indicates pending human approval."""
    s = status.lower()
    return "pending" in s or "approval" in s or "review" in s or "queued" in s


def _is_rejected_status(status: str) -> bool:
    """Check if a status string indicates rejection."""
    s = status.lower()
    return "reject" in s or "denied" in s or "declined" in s


# ---------------------------------------------------------------------------
# Scenario infrastructure
# ---------------------------------------------------------------------------


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ScenarioResult:
    name: str
    checks: list[Check] = field(default_factory=list)
    error: str | None = None
    duration_s: float = 0.0

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        self.checks.append(Check(name, condition, detail))

    def fail(self, error: str) -> None:
        self.error = error


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def scenario_1_basic_event_to_action(
    orch: AgentOrchestrator,
) -> ScenarioResult:
    """Basic Event-to-Action Flow"""
    result = ScenarioResult("1. Basic event-to-action flow")

    # Permissive policies — everything auto
    await orch.configure_policies("tenant-basic", {
        "send_email": "auto",
        "send_message": "auto",
        "send_borrower_message": "auto",
        "notify_loan_officer": "auto",
        "notify": "auto",
        "update_record": "auto",
        "request_document": "auto",
    })

    actions = await orch.handle_event({
        "event_type": "document_uploaded",
        "tenant_id": "tenant-basic",
        "loan_id": "loan-001",
        "payload": {
            "document_type": "pay_stub",
            "borrower_name": "Jane Doe",
        },
    })

    result.check(
        "Returns actions",
        isinstance(actions, list) and len(actions) > 0,
        f"Got {len(actions) if isinstance(actions, list) else 'non-list'} actions",
    )

    if not actions:
        return result

    for i, action in enumerate(actions):
        result.check(
            f"Action {i} has action_id",
            isinstance(action.get("action_id"), str)
            and len(action["action_id"]) > 0,
        )
        result.check(
            f"Action {i} has action_type",
            isinstance(action.get("action_type"), str)
            and len(action["action_type"]) > 0,
        )
        result.check(
            f"Action {i} has status",
            isinstance(action.get("status"), str) and len(action["status"]) > 0,
        )

    # All action_ids should be unique
    ids = [a.get("action_id") for a in actions if a.get("action_id")]
    result.check("Action IDs are unique", len(ids) == len(set(ids)))

    return result


async def scenario_2_policy_gating(orch: AgentOrchestrator) -> ScenarioResult:
    """Policy Gating (Auto vs. Approval Required)"""
    result = ScenarioResult("2. Policy gating")

    # Everything requires approval
    await orch.configure_policies("tenant-strict", {
        "send_email": "approval_required",
        "send_message": "approval_required",
        "send_borrower_message": "approval_required",
        "notify_loan_officer": "approval_required",
        "notify": "approval_required",
        "update_record": "approval_required",
        "request_document": "approval_required",
        "clear_condition": "approval_required",
        "schedule_call": "approval_required",
        "default": "approval_required",
        "*": "approval_required",
    })

    strict_actions = await orch.handle_event({
        "event_type": "message_received",
        "tenant_id": "tenant-strict",
        "loan_id": "loan-002",
        "payload": {
            "sender": "borrower",
            "content": "When will my loan be approved?",
            "channel": "chat",
        },
    })

    result.check(
        "Strict tenant produces actions",
        isinstance(strict_actions, list) and len(strict_actions) > 0,
        f"Got {len(strict_actions) if isinstance(strict_actions, list) else 0}",
    )

    if strict_actions:
        all_pending = all(
            _is_pending_status(a.get("status", "")) for a in strict_actions
        )
        result.check(
            "All actions are pending (not auto-executed)",
            all_pending,
            f"Statuses: {[a.get('status') for a in strict_actions]}",
        )

    # Now configure a permissive tenant
    await orch.configure_policies("tenant-permissive", {
        "send_email": "auto",
        "send_message": "auto",
        "send_borrower_message": "auto",
        "notify_loan_officer": "auto",
        "notify": "auto",
        "update_record": "auto",
        "request_document": "auto",
        "clear_condition": "auto",
        "schedule_call": "auto",
        "default": "auto",
        "*": "auto",
    })

    permissive_actions = await orch.handle_event({
        "event_type": "message_received",
        "tenant_id": "tenant-permissive",
        "loan_id": "loan-003",
        "payload": {
            "sender": "borrower",
            "content": "When will my loan be approved?",
            "channel": "chat",
        },
    })

    result.check(
        "Permissive tenant produces actions",
        isinstance(permissive_actions, list) and len(permissive_actions) > 0,
    )

    if permissive_actions:
        all_auto = all(
            _is_auto_status(a.get("status", "")) for a in permissive_actions
        )
        result.check(
            "All actions are auto-approved",
            all_auto,
            f"Statuses: {[a.get('status') for a in permissive_actions]}",
        )

    return result


async def scenario_3_approval_lifecycle(
    orch: AgentOrchestrator,
) -> ScenarioResult:
    """Approval Queue Lifecycle"""
    result = ScenarioResult("3. Approval queue lifecycle")

    # All actions require approval
    await orch.configure_policies("tenant-lifecycle", {
        "default": "approval_required",
        "*": "approval_required",
        "send_email": "approval_required",
        "send_message": "approval_required",
        "send_borrower_message": "approval_required",
        "notify_loan_officer": "approval_required",
        "notify": "approval_required",
        "update_record": "approval_required",
        "request_document": "approval_required",
    })

    actions = await orch.handle_event({
        "event_type": "document_uploaded",
        "tenant_id": "tenant-lifecycle",
        "loan_id": "loan-004",
        "payload": {
            "document_type": "bank_statement",
            "borrower_name": "John Smith",
            "issues": ["large_deposit_detected"],
        },
    })

    result.check("Event produces actions", len(actions) > 0)
    if not actions:
        return result

    # Check pending actions queue
    pending = await orch.get_pending_actions("tenant-lifecycle")
    result.check(
        "Pending actions queue is populated",
        len(pending) > 0,
        f"Found {len(pending)} pending actions",
    )

    if not pending:
        return result

    # Approve first action
    first_id = pending[0]["action_id"]
    approved = await orch.approve_action(first_id)
    result.check(
        "Approved action has non-pending status",
        not _is_pending_status(approved.get("status", "pending")),
        f"Status after approval: {approved.get('status')}",
    )

    # Reject second action (if available)
    if len(pending) > 1:
        second_id = pending[1]["action_id"]
        rejected = await orch.reject_action(second_id, reason="Not appropriate")
        result.check(
            "Rejected action has rejected status",
            _is_rejected_status(rejected.get("status", "")),
            f"Status after rejection: {rejected.get('status')}",
        )

    # Approved/rejected actions should no longer be pending
    still_pending = await orch.get_pending_actions("tenant-lifecycle")
    resolved_ids = {first_id}
    if len(pending) > 1:
        resolved_ids.add(pending[1]["action_id"])

    still_pending_ids = {a["action_id"] for a in still_pending}
    result.check(
        "Resolved actions removed from pending queue",
        len(resolved_ids & still_pending_ids) == 0,
        f"Resolved: {resolved_ids}, still pending: {still_pending_ids}",
    )

    # Double-approve should raise
    try:
        await orch.approve_action(first_id)
        result.check(
            "Double-approve raises exception",
            False,
            "No exception raised on double-approve",
        )
    except Exception:
        result.check("Double-approve raises exception", True)

    return result


async def scenario_4_idempotency(orch: AgentOrchestrator) -> ScenarioResult:
    """Idempotency / Duplicate Events"""
    result = ScenarioResult("4. Idempotency")

    await orch.configure_policies("tenant-idemp", {
        "default": "approval_required",
        "*": "approval_required",
        "send_email": "approval_required",
        "send_message": "approval_required",
        "send_borrower_message": "approval_required",
        "notify_loan_officer": "approval_required",
        "notify": "approval_required",
    })

    event = {
        "event_id": "evt-duplicate-001",
        "event_type": "condition_flagged",
        "tenant_id": "tenant-idemp",
        "loan_id": "loan-005",
        "payload": {
            "condition": "missing_w2",
            "borrower_name": "Alice Johnson",
        },
    }

    first_actions = await orch.handle_event(event)
    result.check("First event produces actions", len(first_actions) > 0)

    second_actions = await orch.handle_event(event)

    # Second call should either return same actions (idempotent) or empty list
    if len(second_actions) == 0:
        result.check("Duplicate event returns empty list (idempotent)", True)
    elif len(second_actions) == len(first_actions):
        first_ids = {a["action_id"] for a in first_actions}
        second_ids = {a["action_id"] for a in second_actions}
        result.check(
            "Duplicate event returns same actions (idempotent)",
            first_ids == second_ids,
            f"First: {first_ids}, Second: {second_ids}",
        )
    else:
        result.check(
            "Duplicate event handled (no extra actions)",
            False,
            f"First call: {len(first_actions)} actions, "
            f"second call: {len(second_actions)} actions",
        )

    # Verify no duplicates in pending queue
    pending = await orch.get_pending_actions("tenant-idemp")
    pending_ids = [a["action_id"] for a in pending]
    result.check(
        "No duplicate action IDs in pending queue",
        len(pending_ids) == len(set(pending_ids)),
        f"Pending IDs: {pending_ids}",
    )

    return result


async def scenario_5_tenant_isolation(
    orch: AgentOrchestrator,
) -> ScenarioResult:
    """Multi-Tenant Isolation"""
    result = ScenarioResult("5. Multi-tenant isolation")

    await orch.configure_policies("tenant-alpha", {
        "default": "approval_required",
        "*": "approval_required",
        "send_email": "approval_required",
        "send_message": "approval_required",
        "notify": "approval_required",
    })
    await orch.configure_policies("tenant-beta", {
        "default": "approval_required",
        "*": "approval_required",
        "send_email": "approval_required",
        "send_message": "approval_required",
        "notify": "approval_required",
    })

    await orch.handle_event({
        "event_type": "document_uploaded",
        "tenant_id": "tenant-alpha",
        "loan_id": "loan-alpha-1",
        "payload": {"document_type": "tax_return", "borrower_name": "Alpha Borrower"},
    })

    await orch.handle_event({
        "event_type": "message_received",
        "tenant_id": "tenant-beta",
        "loan_id": "loan-beta-1",
        "payload": {"sender": "borrower", "content": "Hello"},
    })

    alpha_pending = await orch.get_pending_actions("tenant-alpha")
    beta_pending = await orch.get_pending_actions("tenant-beta")

    result.check("Tenant alpha has pending actions", len(alpha_pending) > 0)
    result.check("Tenant beta has pending actions", len(beta_pending) > 0)

    # Check no cross-contamination
    alpha_ids = {a["action_id"] for a in alpha_pending}
    beta_ids = {a["action_id"] for a in beta_pending}
    result.check(
        "No overlapping action IDs between tenants",
        len(alpha_ids & beta_ids) == 0,
        f"Alpha: {alpha_ids}, Beta: {beta_ids}",
    )

    # Approve alpha action, verify beta is unaffected
    if alpha_pending:
        await orch.approve_action(alpha_pending[0]["action_id"])
        beta_after = await orch.get_pending_actions("tenant-beta")
        result.check(
            "Approving alpha action doesn't affect beta",
            len(beta_after) == len(beta_pending),
        )

    return result


async def scenario_6_audit_trail(orch: AgentOrchestrator) -> ScenarioResult:
    """Audit Trail & Reasoning"""
    result = ScenarioResult("6. Audit trail & reasoning")

    await orch.configure_policies("tenant-audit", {
        "default": "approval_required",
        "*": "approval_required",
        "send_email": "approval_required",
        "send_message": "approval_required",
        "send_borrower_message": "approval_required",
        "notify_loan_officer": "approval_required",
        "notify": "approval_required",
    })

    actions = await orch.handle_event({
        "event_type": "loan_status_changed",
        "tenant_id": "tenant-audit",
        "loan_id": "loan-006",
        "payload": {
            "old_status": "in_review",
            "new_status": "conditionally_approved",
            "borrower_name": "Bob Williams",
        },
    })

    result.check("Event produces actions", len(actions) > 0)

    if actions:
        action = actions[0]
        # Check for reasoning / explanation from the LLM
        reasoning_fields = ["reasoning", "reason", "explanation", "rationale", "llm_reasoning"]
        has_reasoning = any(
            isinstance(action.get(f), str) and len(action.get(f, "")) > 0
            for f in reasoning_fields
        )
        result.check(
            "Action includes LLM reasoning",
            has_reasoning,
            f"Fields present: {[f for f in reasoning_fields if action.get(f)]}",
        )

    # Approve an action and check for audit metadata
    if actions:
        pending = await orch.get_pending_actions("tenant-audit")
        if pending:
            approved = await orch.approve_action(pending[0]["action_id"])
            audit_fields = [
                "audit_trail", "history", "status_history", "updated_at",
                "approved_at", "transitions", "audit", "timeline",
            ]
            has_audit = any(approved.get(f) is not None for f in audit_fields)
            result.check(
                "Approved action has audit metadata",
                has_audit,
                f"Fields present: {[f for f in audit_fields if approved.get(f) is not None]}",
            )

    return result


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

SCENARIOS = [
    scenario_1_basic_event_to_action,
    scenario_2_policy_gating,
    scenario_3_approval_lifecycle,
    scenario_4_idempotency,
    scenario_5_tenant_isolation,
    scenario_6_audit_trail,
]


def _load_class(dotted_path: str) -> type:
    """Import a class by dotted path (e.g. 'my_module.MyOrchestrator')."""
    try:
        module_path, class_name = dotted_path.rsplit(".", 1)
    except ValueError:
        print(f"ERROR: '{dotted_path}' is not a valid dotted path.")
        print("Expected format: your_module.YourClass")
        sys.exit(1)

    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as e:
        print(f"ERROR: Could not import module '{module_path}': {e}")
        sys.exit(1)

    cls = getattr(module, class_name, None)
    if cls is None:
        print(f"ERROR: Class '{class_name}' not found in module '{module_path}'")
        sys.exit(1)

    if not (isinstance(cls, type) and issubclass(cls, AgentOrchestrator)):
        print(f"ERROR: {dotted_path} does not subclass AgentOrchestrator")
        sys.exit(1)

    return cls


async def _run_all(orch: AgentOrchestrator) -> None:
    results: list[ScenarioResult] = []

    for scenario_fn in SCENARIOS:
        name = scenario_fn.__doc__ or scenario_fn.__name__
        print(f"\n{'=' * 64}")
        print(f"  {name}")
        print(f"{'=' * 64}")

        t0 = time.monotonic()
        try:
            res = await scenario_fn(orch)
        except Exception:
            res = ScenarioResult(name)
            res.fail(f"Unhandled exception:\n{traceback.format_exc()}")
        res.duration_s = time.monotonic() - t0
        results.append(res)

        for c in res.checks:
            symbol = "PASS" if c.passed else "FAIL"
            line = f"  [{symbol}] {c.name}"
            if c.detail:
                line += f" — {c.detail}"
            print(line)
        if res.error:
            print(f"  [ERROR] {res.error}")
        print(f"  ({res.duration_s:.2f}s)")

    # Summary
    print(f"\n{'=' * 64}")
    print("  SUMMARY")
    print(f"{'=' * 64}")
    passed = sum(1 for r in results if r.passed)
    for r in results:
        symbol = "PASS" if r.passed else "FAIL"
        print(f"  [{symbol}] {r.name}")
    print(f"\n  {passed}/{len(results)} scenarios passed")
    print(f"{'=' * 64}")


def main() -> None:
    # Load .env
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if len(sys.argv) != 2:
        print("Usage: python evaluate.py your_module.YourClass")
        print()
        print("Example: python evaluate.py orchestrator.MyOrchestrator")
        sys.exit(1)

    cls = _load_class(sys.argv[1])
    print(f"Loaded: {cls.__name__}")
    orch = cls()
    asyncio.run(_run_all(orch))


if __name__ == "__main__":
    main()
