"""PennyCore — shared contracts.

Pydantic v2 models that mirror the SQL DDL in
migrations/versions/0001_initial_schema.sql. Both context_engine and
orchestrator import from this package; the takehome adapters translate
between the loose-typed external dicts and these strict types at the boundary.

Single source of truth for field shapes, enum values, and validation. If the
DDL gains a column, the matching Pydantic model gains a field in the same
commit — no exceptions.
"""

from contracts.actions import (
    Action,
    ActionProposal,
    ActionStatus,
    ActionType,
    ProposedBy,
)
from contracts.audit import AuditActorKind, AuditKind, AuditLogEntry
from contracts.briefs import Brief, BriefSegment, RetrievalStrategy, SegmentSource
from contracts.customers import Customer, CustomerIdentity, IdentityKind
from contracts.events import Channel, ChannelType, Event, EventType, Message, MessageDirection
from contracts.policies import ApprovalRule, Policy, PolicyDecision, Tenant

__all__ = [
    "Action",
    "ActionProposal",
    "ActionStatus",
    "ActionType",
    "ApprovalRule",
    "AuditActorKind",
    "AuditKind",
    "AuditLogEntry",
    "Brief",
    "BriefSegment",
    "Channel",
    "ChannelType",
    "Customer",
    "CustomerIdentity",
    "Event",
    "EventType",
    "IdentityKind",
    "Message",
    "MessageDirection",
    "Policy",
    "PolicyDecision",
    "ProposedBy",
    "RetrievalStrategy",
    "SegmentSource",
    "Tenant",
]
