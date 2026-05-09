"""Customer + identity repository (Day 6, Phase 2).

The cross-channel linker (`context_engine.linking.resolve_customer`) walks
this port to translate an inbound event's identity hints (email / phone /
external_id / chat_handle) into a stable `customer_id`. The DDL oracle is
`UNIQUE (tenant_id, identity_kind, identity_value)` on `customer_identities`
(migrations/versions/0001_initial_schema.sql §4) — same uniqueness key the
in-memory implementation enforces here so swapping in the Postgres adapter
later is a one-line change in `context_engine/api.py` (the same Protocol
pattern Day 5 used for `EventRepository`).

Multi-tenant invariant (rule 15): every method on the port is tenant-scoped.
There is no API path that returns a customer or identity without a
`tenant_id`. Cross-tenant lookups return `None` even when the id is known —
the in-memory implementation enforces this with tenant-keyed dicts; the
Postgres adapter will enforce it with `WHERE tenant_id = %s` on every query.
"""

from __future__ import annotations

import uuid
from threading import RLock
from typing import Iterable, Protocol, runtime_checkable

from contracts import Customer, CustomerIdentity, IdentityKind


def new_customer_id() -> str:
    """UUID4-derived customer id with a `cust_` prefix.

    Same convention as `new_event_id()` in `ingestion.py`: random uuid4 (not
    sortable; ordering relies on `created_at`), short hex, human-debuggable
    prefix.
    """
    return f"cust_{uuid.uuid4().hex[:24]}"


def new_identity_id() -> str:
    return f"idn_{uuid.uuid4().hex[:24]}"


@runtime_checkable
class CustomerRepository(Protocol):
    """Narrow port for customer + identity persistence.

    Methods are tenant-scoped by parameter, NOT by a constructor-bound
    tenant — the same repository instance serves every tenant in process,
    and the multi-tenant invariant is enforced by the `tenant_id` argument
    being mandatory and unforgeable. The Postgres adapter (forthcoming) will
    satisfy this exact shape.
    """

    def get_customer(self, *, tenant_id: str, customer_id: str) -> Customer | None: ...

    def find_customer_by_identity(
        self,
        *,
        tenant_id: str,
        identity_kind: IdentityKind,
        identity_value: str,
    ) -> Customer | None:
        """Look up the customer that owns `(tenant_id, identity_kind,
        identity_value)`. Returns `None` if the identity is unseen for this
        tenant (even if it exists for a different tenant — isolation)."""

    def create_customer(
        self,
        *,
        tenant_id: str,
        identities: Iterable[tuple[IdentityKind, str]],
        display_name: str | None = None,
    ) -> Customer:
        """Create a new customer + the supplied identities atomically.

        Caller has already checked that NONE of the identities resolve to an
        existing customer in this tenant — passing a duplicate raises
        `IdentityCollision`. The implementation MUST enforce this even if the
        caller forgot to check (the DDL UNIQUE constraint will too)."""

    def add_identity(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        identity_kind: IdentityKind,
        identity_value: str,
    ) -> CustomerIdentity:
        """Attach an identity to an existing customer.

        Idempotent: if `(tenant_id, identity_kind, identity_value)` already
        points at this same customer, returns the existing row unchanged.
        Raises `IdentityCollision` if the identity points at a DIFFERENT
        customer in the same tenant — that's the same UNIQUE-constraint
        signal Postgres would raise."""

    def list_identities(
        self,
        *,
        tenant_id: str,
        customer_id: str,
    ) -> list[CustomerIdentity]: ...

    def count(self, *, tenant_id: str | None = None) -> int: ...


class IdentityCollision(Exception):
    """Raised when an identity insert would violate
    `UNIQUE (tenant_id, identity_kind, identity_value)` against a different
    customer than the one being written. Propagated up to the linker, which
    treats it as a hard error: a healthy linker resolves the collision by
    looking up the existing customer FIRST and only inserts when free.
    """


class InMemoryCustomerRepository:
    """Process-local customer store used by tests and `MOCK_LLM` mode.

    Storage layout mirrors what Postgres will hold:

      * `_customers[(tenant_id, customer_id)] = Customer`
      * `_identities[(tenant_id, identity_id)] = CustomerIdentity`
      * `_identity_index[(tenant_id, kind, value)] = customer_id`
        ← this is the UNIQUE oracle. The Postgres adapter relies on the DDL
        constraint of the same shape.

    Thread-safe (`RLock`) because FastAPI's TestClient and the Phase 4
    load-test harness both hammer the same instance concurrently.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._customers: dict[tuple[str, str], Customer] = {}
        self._identities: dict[tuple[str, str], CustomerIdentity] = {}
        self._identity_index: dict[tuple[str, IdentityKind, str], str] = {}

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    def get_customer(self, *, tenant_id: str, customer_id: str) -> Customer | None:
        with self._lock:
            return self._customers.get((tenant_id, customer_id))

    def find_customer_by_identity(
        self,
        *,
        tenant_id: str,
        identity_kind: IdentityKind,
        identity_value: str,
    ) -> Customer | None:
        with self._lock:
            cid = self._identity_index.get((tenant_id, identity_kind, identity_value))
            if cid is None:
                return None
            return self._customers.get((tenant_id, cid))

    def list_identities(
        self,
        *,
        tenant_id: str,
        customer_id: str,
    ) -> list[CustomerIdentity]:
        with self._lock:
            return [
                ident
                for (t, _iid), ident in self._identities.items()
                if t == tenant_id and ident.customer_id == customer_id
            ]

    def count(self, *, tenant_id: str | None = None) -> int:
        with self._lock:
            if tenant_id is None:
                return len(self._customers)
            return sum(1 for (t, _cid) in self._customers if t == tenant_id)

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    def create_customer(
        self,
        *,
        tenant_id: str,
        identities: Iterable[tuple[IdentityKind, str]],
        display_name: str | None = None,
    ) -> Customer:
        identity_list = list(identities)
        with self._lock:
            # Pre-flight: any of these already linked to a different customer?
            for kind, value in identity_list:
                normalized = _ident_index_value(kind, value)
                existing_cid = self._identity_index.get((tenant_id, kind, normalized))
                if existing_cid is not None:
                    raise IdentityCollision(
                        f"identity ({kind.value}={value!r}) already linked to "
                        f"customer {existing_cid!r} in tenant {tenant_id!r}"
                    )

            customer = Customer(
                id=new_customer_id(),
                tenant_id=tenant_id,
                display_name=display_name,
            )
            self._customers[(tenant_id, customer.id)] = customer

            for kind, value in identity_list:
                self._insert_identity_locked(
                    tenant_id=tenant_id,
                    customer_id=customer.id,
                    identity_kind=kind,
                    identity_value=value,
                )

            return customer

    def add_identity(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        identity_kind: IdentityKind,
        identity_value: str,
    ) -> CustomerIdentity:
        normalized = _ident_index_value(identity_kind, identity_value)
        with self._lock:
            existing_cid = self._identity_index.get(
                (tenant_id, identity_kind, normalized)
            )
            if existing_cid is not None:
                if existing_cid != customer_id:
                    raise IdentityCollision(
                        f"identity ({identity_kind.value}={identity_value!r}) "
                        f"already linked to customer {existing_cid!r}, "
                        f"cannot reattach to {customer_id!r}"
                    )
                # Idempotent: same identity, same customer — return the row.
                for (t, _iid), ident in self._identities.items():
                    if (
                        t == tenant_id
                        and ident.customer_id == customer_id
                        and ident.identity_kind is identity_kind
                        and ident.identity_value == normalized
                    ):
                        return ident
                # Index says it exists but the row doesn't — should never
                # happen. Fall through to fresh insert as a defensive recovery.

            if (tenant_id, customer_id) not in self._customers:
                raise KeyError(
                    f"customer {customer_id!r} not found in tenant {tenant_id!r}"
                )
            return self._insert_identity_locked(
                tenant_id=tenant_id,
                customer_id=customer_id,
                identity_kind=identity_kind,
                identity_value=identity_value,
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _insert_identity_locked(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        identity_kind: IdentityKind,
        identity_value: str,
    ) -> CustomerIdentity:
        """Insert an identity. Caller must hold `self._lock`."""
        identity = CustomerIdentity(
            id=new_identity_id(),
            tenant_id=tenant_id,
            customer_id=customer_id,
            identity_kind=identity_kind,
            identity_value=identity_value,  # validator normalizes
        )
        self._identities[(tenant_id, identity.id)] = identity
        self._identity_index[
            (tenant_id, identity.identity_kind, identity.identity_value)
        ] = customer_id
        return identity

    def clear(self) -> None:
        """Test-only helper. Not on the Protocol — production callers cannot
        wipe the store."""
        with self._lock:
            self._customers.clear()
            self._identities.clear()
            self._identity_index.clear()


def _ident_index_value(kind: IdentityKind, value: str) -> str:
    """Compute the canonical index key for an identity value.

    Mirrors the normalization the `CustomerIdentity` Pydantic validator
    performs (lowercase + strip for email/chat_handle, strip for the rest)
    so the in-memory `_identity_index` lookup matches what would land in the
    Postgres column. Phone-number normalization is the LINKER's job (it runs
    BEFORE this function); the repository just trusts the value it receives.
    """
    v = value.strip()
    if kind in (IdentityKind.EMAIL, IdentityKind.CHAT_HANDLE):
        v = v.lower()
    return v
