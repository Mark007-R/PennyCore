"""Python-rules policy engine — Phase 3 (Day 17) competitor.

Same tenant policy table as :class:`DeclarativePolicyEngine`, but the
table is compiled into a per-tenant Python callable rather than a
direct dict lookup. The benchmark question this engine answers is:
"is there any correctness or maintainability benefit from expressing
policies as code instead of as a config dict?"

## What the engine adds over the declarative dict

* Per-tenant **callables** can branch on event payload, channel,
  weekday, or anything else the policy author wants. The Day-17
  benchmark exercises only the basic action_type lookup so the head-
  to-head with declarative is apples-to-apples; the conditional
  surface is what compliance teams pay engineering for in production.
* "default" + "*" fallback semantics match the declarative engine
  exactly — same resolution order, same safe default. Two engines
  with the same lookup contract differ only in *how policies are
  expressed*.

## What the engine costs over the declarative dict

* Every policy update requires touching Python — a compliance team
  can't edit a YAML / dict and ship. The Day-18 wrap-up writes this
  up as the maintainability tradeoff.
* No introspection on the policy table (the dict became opaque
  behind the callable). The benchmark records this as Auditability
  3/5 vs Declarative's 5/5.

## Multi-tenant invariant (rule 15)

Per-tenant handlers live in ``_handlers[tenant_id]``. There is no
shared mutable state between tenants, so a tenant's policy table
cannot leak into another's lookup path.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from contracts.actions import ActionType
from contracts.policies import PolicyDecision

from orchestrator.policy.declarative import (
    _DEFAULT_KEY,
    _WILDCARD_KEY,
    _normalize_action_type,
    _normalize_decision,
)

# A tenant handler returns the decision for (action_type, event_context).
# event_context is optional — the Day-17 benchmark passes the scenario's
# event dict so a real Python rule could branch on event_type / payload.
TenantHandler = Callable[[str, dict[str, Any] | None], PolicyDecision]


class PythonRulesPolicyEngine:
    """Policies expressed as compiled per-tenant Python callables.

    Same set/decide surface as :class:`DeclarativePolicyEngine`.
    Re-setting a tenant's policies recompiles its handler — there is
    no partial-update path on the Day-17 surface.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._handlers: dict[str, TenantHandler] = {}
        self._raw_tables: dict[str, dict[str, PolicyDecision]] = {}

    def set_policies(
        self, tenant_id: str, policies: dict[str, str]
    ) -> None:
        if not tenant_id:
            raise ValueError("tenant_id must be non-empty")
        normalized: dict[str, PolicyDecision] = {}
        for key, value in policies.items():
            if not isinstance(value, str):
                raise ValueError(
                    f"policy decision must be a string, got "
                    f"{type(value).__name__} for key {key!r}"
                )
            normalized[key] = _normalize_decision(value)

        # Capture in a closure so the compiled handler doesn't carry a
        # reference to ``self`` — keeps per-tenant logic isolated and
        # the closure cheap to invoke per decision.
        def handler(
            action_type: str,
            event_context: dict[str, Any] | None,
        ) -> PolicyDecision:
            # Future Python-rule policies might consult ``event_context``
            # (weekday checks, payload thresholds, etc.). Day-17 keeps
            # the path identical to declarative so the head-to-head is
            # clean. The ``del`` keeps Ruff happy about the unused arg
            # while leaving the parameter in the signature.
            del event_context
            if action_type in normalized:
                return normalized[action_type]
            if _DEFAULT_KEY in normalized:
                return normalized[_DEFAULT_KEY]
            if _WILDCARD_KEY in normalized:
                return normalized[_WILDCARD_KEY]
            return PolicyDecision.APPROVAL_REQUIRED

        with self._lock:
            self._handlers[tenant_id] = handler
            self._raw_tables[tenant_id] = normalized

    def decide(
        self,
        tenant_id: str,
        action_type: ActionType | str,
        event_context: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        key = _normalize_action_type(action_type)
        with self._lock:
            handler = self._handlers.get(tenant_id)
        if handler is None:
            return PolicyDecision.APPROVAL_REQUIRED
        return handler(key, event_context)

    def tenants(self) -> list[str]:
        with self._lock:
            return sorted(self._handlers.keys())

    def policies_for(self, tenant_id: str) -> dict[str, str]:
        with self._lock:
            table = self._raw_tables.get(tenant_id, {})
            return {k: v.value for k, v in table.items()}

    def clear(self) -> None:
        with self._lock:
            self._handlers.clear()
            self._raw_tables.clear()
