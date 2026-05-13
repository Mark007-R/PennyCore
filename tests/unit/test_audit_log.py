"""Tests for the Day-10 in-memory audit log.

Coverage:
  1. write returns an entry with an assigned monotonic ID.
  2. entries_for_tenant returns only that tenant's rows.
  3. entries_for_action returns only that action's rows.
  4. entries_for_event returns only that event's rows.
  5. Entries are immutable (frozen Pydantic model).
  6. clear() resets every index AND the ID counter.
  7. all_entries returns insertion order.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from contracts.audit import AuditActorKind, AuditKind, AuditLogEntry
from orchestrator.audit import InMemoryAuditLog


def _entry(
    *,
    tenant_id: str = "tenant-a",
    action_id: str | None = "act_001",
    event_id: str | None = "evt_001",
    kind: AuditKind = AuditKind.PROPOSAL,
    actor_kind: AuditActorKind = AuditActorKind.SYSTEM,
) -> AuditLogEntry:
    return AuditLogEntry(
        tenant_id=tenant_id,
        kind=kind,
        action_id=action_id,
        caused_by_event_id=event_id,
        actor_kind=actor_kind,
    )


def test_write_assigns_monotonic_id() -> None:
    log = InMemoryAuditLog()
    a = log.write(_entry())
    b = log.write(_entry())
    c = log.write(_entry())
    assert a.id == 1
    assert b.id == 2
    assert c.id == 3


def test_entries_for_tenant_filters() -> None:
    log = InMemoryAuditLog()
    log.write(_entry(tenant_id="alpha", action_id="act_a"))
    log.write(_entry(tenant_id="beta", action_id="act_b"))
    log.write(_entry(tenant_id="alpha", action_id="act_a2"))
    alpha = log.entries_for_tenant("alpha")
    beta = log.entries_for_tenant("beta")
    assert {e.action_id for e in alpha} == {"act_a", "act_a2"}
    assert {e.action_id for e in beta} == {"act_b"}


def test_entries_for_action_filters() -> None:
    log = InMemoryAuditLog()
    log.write(_entry(action_id="act_001"))
    log.write(_entry(action_id="act_002"))
    log.write(_entry(action_id="act_001", kind=AuditKind.APPROVAL))
    rows = log.entries_for_action("act_001")
    assert len(rows) == 2
    assert {e.kind for e in rows} == {AuditKind.PROPOSAL, AuditKind.APPROVAL}


def test_entries_for_event_filters() -> None:
    log = InMemoryAuditLog()
    log.write(_entry(event_id="evt_X"))
    log.write(_entry(event_id="evt_Y"))
    rows = log.entries_for_event("evt_X")
    assert len(rows) == 1
    assert rows[0].caused_by_event_id == "evt_X"


def test_entries_with_none_action_or_event_skip_those_indexes() -> None:
    log = InMemoryAuditLog()
    log.write(_entry(action_id=None, event_id=None))
    assert log.entries_for_action("act_001") == []
    assert log.entries_for_event("evt_001") == []
    # Tenant index still gets the row.
    assert len(log.entries_for_tenant("tenant-a")) == 1


def test_entries_are_immutable() -> None:
    """`frozen=True` on AuditLogEntry — attempting to set an attr raises."""
    log = InMemoryAuditLog()
    written = log.write(_entry())
    with pytest.raises(Exception):
        written.tenant_id = "mutated"  # type: ignore[misc]


def test_clear_resets_counter_and_indexes() -> None:
    log = InMemoryAuditLog()
    log.write(_entry())
    log.write(_entry())
    log.clear()
    assert log.all_entries() == []
    assert log.entries_for_tenant("tenant-a") == []
    # Counter reset — first new entry's ID is 1, not 3.
    fresh = log.write(_entry())
    assert fresh.id == 1


def test_all_entries_preserves_insertion_order() -> None:
    log = InMemoryAuditLog()
    log.write(_entry(kind=AuditKind.PROPOSAL))
    log.write(_entry(kind=AuditKind.DECISION))
    log.write(_entry(kind=AuditKind.APPROVAL))
    log.write(_entry(kind=AuditKind.EXECUTION))
    kinds = [e.kind for e in log.all_entries()]
    assert kinds == [
        AuditKind.PROPOSAL,
        AuditKind.DECISION,
        AuditKind.APPROVAL,
        AuditKind.EXECUTION,
    ]


def test_ts_defaults_to_utc_aware() -> None:
    log = InMemoryAuditLog()
    entry = log.write(_entry())
    assert entry.ts.tzinfo is not None
    assert entry.ts.tzinfo.utcoffset(entry.ts) == timezone.utc.utcoffset(
        datetime.now(timezone.utc)
    )
