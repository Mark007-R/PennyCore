"""Declarative dict-backed policy engine (Day 10, Phase 2).

This is the Phase-2 champion. A tenant configures policies as
`{action_type_str: decision_str}` and the engine resolves
`decide(tenant_id, action_type)` in three steps:

  1. Exact match on the action_type string.
  2. Fall back to the `"default"` key (explicit per-tenant default).
  3. Fall back to the `"*"` wildcard key (compat shape used by the
     external take-home assessment's strict/permissive tenants).
  4. Final safety net: `PolicyDecision.APPROVAL_REQUIRED` (D9 in the
     external rubric — unknown action types escalate to a human rather
     than silently auto-executing).

## Why a dict rather than YAML on disk

Phase 3 will add three more engines and benchmark all four. The
champion-loser comparison is the deliverable. Keeping the Day-10 engine
as an in-memory dict (no YAML parsing, no disk I/O) means the
declarative variant's correctness is decoupled from any file-format
bugs. Phase 3 adds `from_yaml` / `from_postgres` constructors on top of
this same engine — the lookup logic doesn't change.

## Why store decisions as a flat dict (not as `Policy` rows)

The `Policy` Pydantic model in `contracts/policies.py` is the
persistence shape for the Postgres `policies` table. The engine's
runtime shape is denormalized — one `dict[action_type, decision]` per
tenant — because the lookup is read-mostly and benefits from O(1)
access. Phase 3 adds a `sync_from_repository(tenant_id)` method that
loads `Policy` rows into the in-memory dict.

## Multi-tenant invariant

`_by_tenant: dict[tenant_id, dict[action_type, PolicyDecision]]`. A
tenant's policy table is keyed by its own ID — there is no path from
tenant A's lookup into tenant B's table. The test suite asserts this
explicitly (see `test_declarative_policy.py`).
"""

from __future__ import annotations

import threading
from typing import Any, Protocol, runtime_checkable

from contracts.actions import ActionType
from contracts.policies import PolicyDecision

# Keys reserved for "any action_type" routing inside a single tenant's
# policy table. Both are honored — the external take-home assessment's
# strict/permissive tenants use `"*"`, the declarative spec we wrote in
# SYSTEM_DESIGN §6 uses `"default"`. Supporting both costs nothing.
_DEFAULT_KEY = "default"
_WILDCARD_KEY = "*"


class UnknownPolicyDecisionError(ValueError):
    """Raised by `set_policies` when a decision string isn't recognized.

    Keeps configuration errors loud — a typo like
    `{"send_email": "auto-approve"}` would otherwise be silently
    rewritten to "approval_required" by the safe default, which is
    exactly the bug class D7 in the external rubric calls out
    ("policies have no observable effect").
    """


@runtime_checkable
class PolicyEngine(Protocol):
    """The shape every engine variant satisfies.

    `set_policies` replaces a tenant's full policy table — partial
    updates aren't supported on the Day-10 surface. Phase 3 may add a
    `patch_policies` method when comparing engines that support
    incremental updates.
    """

    def set_policies(
        self, tenant_id: str, policies: dict[str, str]
    ) -> None: ...

    def decide(
        self,
        tenant_id: str,
        action_type: ActionType | str,
        event_context: dict[str, Any] | None = None,
    ) -> PolicyDecision: ...


# ---------------------------------------------------------------------------
# Decision string normalization. The external take-home assessment uses
# `"auto"` / `"approval_required"` / `"reject"`. `PolicyDecision`'s enum
# values match exactly, so the normalization is a single `PolicyDecision(...)`
# call — but we centralize it so the error surface is consistent.
# ---------------------------------------------------------------------------


def _normalize_decision(value: str) -> PolicyDecision:
    try:
        return PolicyDecision(value)
    except ValueError as exc:
        valid = sorted(d.value for d in PolicyDecision)
        raise UnknownPolicyDecisionError(
            f"unknown policy decision {value!r}; must be one of {valid}"
        ) from exc


def _normalize_action_type(action_type: ActionType | str) -> str:
    """Coerce both `ActionType.SEND_BORROWER_MESSAGE` and the raw string
    `"send_borrower_message"` to the same lookup key. The dict's keys
    are always plain strings so the external assessment's
    `configure_policies` call (which passes strings like `"send_email"`)
    doesn't need a translation layer."""
    if isinstance(action_type, ActionType):
        return action_type.value
    return action_type


class DeclarativePolicyEngine:
    """In-memory two-level dict implementing `PolicyEngine`.

    Thread-safe: an RLock guards both reads and writes. The lock is
    cheap (no I/O under it) and the Phase-2 use case is a single
    FastAPI worker, but the test suite includes a concurrent-mutation
    scenario to keep the contract honest.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_tenant: dict[str, dict[str, PolicyDecision]] = {}

    # ------------------------------------------------------------------
    # PolicyEngine surface
    # ------------------------------------------------------------------

    def set_policies(
        self, tenant_id: str, policies: dict[str, str]
    ) -> None:
        """Replace tenant_id's policy table with the supplied mapping.

        `policies` is `{action_type_or_special_key: decision_str}`.
        The special keys `"default"` and `"*"` are honored at lookup
        time. Unknown decision strings raise
        `UnknownPolicyDecisionError`; an empty dict is valid (every
        lookup will fall through to the safe default).
        """
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        normalized: dict[str, PolicyDecision] = {}
        for key, value in policies.items():
            if not isinstance(value, str):
                raise UnknownPolicyDecisionError(
                    f"policy decision must be a string, got {type(value).__name__} "
                    f"for key {key!r}"
                )
            normalized[key] = _normalize_decision(value)
        with self._lock:
            self._by_tenant[tenant_id] = normalized

    def decide(
        self,
        tenant_id: str,
        action_type: ActionType | str,
        event_context: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """Resolve the decision for (tenant_id, action_type).

        Resolution order: exact match → `"default"` → `"*"` → safe
        default (`APPROVAL_REQUIRED`). Unknown tenants take the same
        path as a known tenant with an empty table — they fall through
        to the safe default. This matters because the external
        evaluator's scenario 1 never configures a `"default"` for
        every action_type the planner could emit; the safe default
        keeps the structural checks passing.

        ``event_context`` is accepted for parity with the Phase-3
        Python-rules / LLM-judge / naive engines (Day 17). The
        declarative engine never consults it — the dict lookup is by
        action_type alone.
        """
        del event_context  # declarative engine ignores event context
        key = _normalize_action_type(action_type)
        with self._lock:
            table = self._by_tenant.get(tenant_id)
            if table is None:
                return PolicyDecision.APPROVAL_REQUIRED
            if key in table:
                return table[key]
            if _DEFAULT_KEY in table:
                return table[_DEFAULT_KEY]
            if _WILDCARD_KEY in table:
                return table[_WILDCARD_KEY]
            return PolicyDecision.APPROVAL_REQUIRED

    # ------------------------------------------------------------------
    # Introspection helpers — used by the admin UI (Day 31) and the
    # Phase 3 benchmark harness.
    # ------------------------------------------------------------------

    def tenants(self) -> list[str]:
        """Return all tenant IDs that have configured policies."""
        with self._lock:
            return sorted(self._by_tenant.keys())

    def policies_for(self, tenant_id: str) -> dict[str, str]:
        """Return tenant_id's policy table as `{key: decision_value}`.

        Empty dict for unknown tenants — caller can distinguish via
        `tenant_id in engine.tenants()` if needed.
        """
        with self._lock:
            table = self._by_tenant.get(tenant_id, {})
            return {k: v.value for k, v in table.items()}

    def clear(self) -> None:
        """Drop all per-tenant policies. Used by tests."""
        with self._lock:
            self._by_tenant.clear()
