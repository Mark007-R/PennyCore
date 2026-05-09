"""Unit-level shape checks for the Postgres adapters.

These tests never open a real connection — they verify the adapters
import cleanly without psycopg installed AND that they structurally
satisfy the runtime-checkable Protocols. The actual SQL behavior is
covered in `tests/integration/test_pg_repositories.py` (skipped without
DATABASE_URL).

Why this matters: a typo in a method name, a missing attribute, or a
changed Protocol signature would NOT be caught by the integration suite
(which is gated by DATABASE_URL and won't run in a CI without
Postgres). This file pins the structural contract so a refactor that
breaks the swap-in symmetry shows up immediately on the unit-test pass.
"""

from __future__ import annotations

from context_engine.customer_repository import CustomerRepository
from context_engine.pg_customer_repository import (
    PgCustomerRepository,
    make_connection_factory,
)
from context_engine.pg_repository import PgEventRepository
from context_engine.repository import EventRepository


def _stub_factory():  # type: ignore[no-untyped-def]
    """A factory that would fail if invoked — but the Protocol checks
    below never invoke it. The point is structural-shape parity with
    the in-memory adapters, not connection liveness."""

    def _never_called():  # type: ignore[no-untyped-def]
        raise RuntimeError("connection factory invoked in shape-only test")

    return _never_called


class TestPgEventRepositoryShape:
    def test_satisfies_event_repository_protocol(self) -> None:
        repo = PgEventRepository(_stub_factory())
        assert isinstance(repo, EventRepository)

    def test_has_expected_methods(self) -> None:
        repo = PgEventRepository(_stub_factory())
        # The Protocol's required methods all exist + are callable.
        for name in (
            "upsert_event",
            "get_event_by_idempotency",
            "get_event_by_id",
            "count",
        ):
            assert callable(getattr(repo, name)), f"missing: {name}"


class TestPgCustomerRepositoryShape:
    def test_satisfies_customer_repository_protocol(self) -> None:
        repo = PgCustomerRepository(_stub_factory())
        assert isinstance(repo, CustomerRepository)

    def test_has_expected_methods(self) -> None:
        repo = PgCustomerRepository(_stub_factory())
        for name in (
            "get_customer",
            "find_customer_by_identity",
            "create_customer",
            "add_identity",
            "list_identities",
            "count",
        ):
            assert callable(getattr(repo, name)), f"missing: {name}"


class TestConnectionFactoryHelper:
    def test_returns_callable(self) -> None:
        factory = make_connection_factory("postgresql://nowhere/never")
        assert callable(factory)

    def test_does_not_open_connection_eagerly(self) -> None:
        # Building the factory must not touch the network. If `make_*`
        # ever starts opening a connection inside, this test fails fast.
        factory = make_connection_factory(
            "postgresql://definitely-not-a-real-host:1/x"
        )
        # No exception — factory is a closure, connection only opens on call.
        assert factory is not None
