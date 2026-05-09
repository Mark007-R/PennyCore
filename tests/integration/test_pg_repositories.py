"""Integration tests for the Postgres repositories.

Skipped unless `DATABASE_URL` is set — psycopg + a real Postgres are
required. Locally:

    docker compose up -d postgres
    DATABASE_URL=postgresql://pennycore:pennycore_local_dev@localhost:5432/pennycore \\
        python -m pytest tests/integration/

The schema is loaded by `docker-entrypoint-initdb.d` on the postgres
service's first boot. To re-apply migrations to an already-running
database, `python scripts/migrate.py` does it idempotently.

Each test creates its own tenant (random uuid prefix) so parallel runs
and re-runs don't collide. Cleanup is `DELETE FROM tenants WHERE id =
%s` — the schema's ON DELETE CASCADE rules wipe customers, identities,
events, audit rows in one shot.

These tests verify that the Postgres adapters satisfy the SAME contract
the in-memory adapters were tested against — by design, the assertion
shape mirrors `test_linking.py` and `test_ingestion.py` so a regression
in either side stands out.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from contracts import (
    ChannelType,
    Event,
    EventType,
    IdentityKind,
)

# Skip the whole module when DATABASE_URL is missing — every test below
# needs a live Postgres.
DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="set DATABASE_URL to run Postgres integration tests",
)


# --- imports guarded so module-level import works without psycopg ----------
# (pytest's collection still runs the module top-to-bottom even when every
# test is skipped, so anything that requires psycopg goes inside fixtures.)


@pytest.fixture(scope="module")
def conn_factory():  # type: ignore[no-untyped-def]
    from context_engine.pg_customer_repository import make_connection_factory

    return make_connection_factory(DATABASE_URL or "")


@pytest.fixture
def tenant_id(conn_factory) -> Iterator[str]:  # type: ignore[no-untyped-def]
    """Create a throw-away tenant; cascade-delete it after the test."""
    tid = f"test-{uuid.uuid4().hex[:12]}"
    conn = conn_factory()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tenants (id, name, slug) VALUES (%s, %s, %s)",
                (tid, f"Test {tid}", tid),
            )
        conn.commit()
    finally:
        conn.close()

    yield tid

    # Cleanup. ON DELETE CASCADE on every scoped table sweeps everything.
    conn = conn_factory()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tenants WHERE id = %s", (tid,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def event_repo(conn_factory):  # type: ignore[no-untyped-def]
    from context_engine.pg_repository import PgEventRepository

    return PgEventRepository(conn_factory)


@pytest.fixture
def customer_repo(conn_factory):  # type: ignore[no-untyped-def]
    from context_engine.pg_customer_repository import PgCustomerRepository

    return PgCustomerRepository(conn_factory)


# ---------------------------------------------------------------------------
# Event repository
# ---------------------------------------------------------------------------


def _make_event(tenant_id: str, **overrides) -> Event:  # type: ignore[no-untyped-def]
    base: dict = {
        "id": f"evt_{uuid.uuid4().hex[:24]}",
        "tenant_id": tenant_id,
        "customer_id": None,
        "channel_code": ChannelType.SMS,
        "event_type": EventType.MESSAGE_RECEIVED,
        "idempotency_key": f"k-{uuid.uuid4().hex[:8]}",
        "payload": {"text": "hi"},
    }
    base.update(overrides)
    return Event(**base)


class TestPgEventRepository:
    def test_upsert_creates_then_replays(
        self, event_repo, tenant_id: str  # type: ignore[no-untyped-def]
    ) -> None:
        evt = _make_event(tenant_id)
        stored, created = event_repo.upsert_event(evt)
        assert created is True
        assert stored.id == evt.id

        # Replay — different in-memory object, same idempotency key.
        replay = _make_event(
            tenant_id,
            id="evt_should_be_ignored",
            idempotency_key=evt.idempotency_key,
            payload={"text": "tampered"},
        )
        stored2, created2 = event_repo.upsert_event(replay)
        assert created2 is False
        assert stored2.id == evt.id  # original wins
        # Stored payload is the original — replay does not mutate.
        assert stored2.payload == {"text": "hi"}

    def test_get_by_idempotency_returns_none_when_absent(
        self, event_repo, tenant_id: str  # type: ignore[no-untyped-def]
    ) -> None:
        result = event_repo.get_event_by_idempotency(
            tenant_id=tenant_id, idempotency_key="never-seen"
        )
        assert result is None

    def test_tenant_isolation_on_lookup(
        self, event_repo, tenant_id: str, conn_factory  # type: ignore[no-untyped-def]
    ) -> None:
        # Make a second sibling tenant; insert an event under tenant_id.
        other_tid = f"test-{uuid.uuid4().hex[:12]}"
        conn = conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO tenants (id, name, slug) VALUES (%s,%s,%s)",
                    (other_tid, f"Test {other_tid}", other_tid),
                )
            conn.commit()

            evt = _make_event(tenant_id, idempotency_key="shared-key")
            event_repo.upsert_event(evt)

            # Lookup under the OTHER tenant must not find it.
            result = event_repo.get_event_by_idempotency(
                tenant_id=other_tid, idempotency_key="shared-key"
            )
            assert result is None
            # Same key under other_tid is a fresh row, not a conflict.
            evt_other = _make_event(other_tid, idempotency_key="shared-key")
            _, created = event_repo.upsert_event(evt_other)
            assert created is True
        finally:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM tenants WHERE id = %s", (other_tid,))
            conn.commit()
            conn.close()

    def test_count_scoped_by_tenant(
        self, event_repo, tenant_id: str  # type: ignore[no-untyped-def]
    ) -> None:
        for _ in range(3):
            event_repo.upsert_event(_make_event(tenant_id))
        assert event_repo.count(tenant_id=tenant_id) == 3
        # Sentinel — a definitely-absent tenant counts zero.
        assert event_repo.count(tenant_id="definitely-not-a-tenant") == 0


# ---------------------------------------------------------------------------
# Customer repository
# ---------------------------------------------------------------------------


class TestPgCustomerRepository:
    def test_create_and_lookup_by_identity(
        self, customer_repo, tenant_id: str  # type: ignore[no-untyped-def]
    ) -> None:
        c = customer_repo.create_customer(
            tenant_id=tenant_id,
            identities=[(IdentityKind.EMAIL, "Jane@Acme.com")],
            display_name="Jane",
        )
        # Email got lowercased on insert (matches in-memory normalization).
        found = customer_repo.find_customer_by_identity(
            tenant_id=tenant_id,
            identity_kind=IdentityKind.EMAIL,
            identity_value="JANE@acme.com",
        )
        assert found is not None
        assert found.id == c.id
        assert found.display_name == "Jane"

    def test_add_identity_idempotent(
        self, customer_repo, tenant_id: str  # type: ignore[no-untyped-def]
    ) -> None:
        from context_engine.customer_repository import IdentityCollision

        c = customer_repo.create_customer(
            tenant_id=tenant_id,
            identities=[(IdentityKind.EMAIL, "a@x.com")],
        )
        first = customer_repo.add_identity(
            tenant_id=tenant_id,
            customer_id=c.id,
            identity_kind=IdentityKind.PHONE,
            identity_value="+15550100100",
        )
        second = customer_repo.add_identity(
            tenant_id=tenant_id,
            customer_id=c.id,
            identity_kind=IdentityKind.PHONE,
            identity_value="+15550100100",
        )
        assert first.id == second.id

        # Different customer trying to claim the same phone → collision.
        c2 = customer_repo.create_customer(
            tenant_id=tenant_id,
            identities=[(IdentityKind.EMAIL, "b@x.com")],
        )
        with pytest.raises(IdentityCollision):
            customer_repo.add_identity(
                tenant_id=tenant_id,
                customer_id=c2.id,
                identity_kind=IdentityKind.PHONE,
                identity_value="+15550100100",
            )

    def test_create_collision_rolls_back(
        self, customer_repo, tenant_id: str  # type: ignore[no-untyped-def]
    ) -> None:
        from context_engine.customer_repository import IdentityCollision

        # Pre-existing email under this tenant.
        customer_repo.create_customer(
            tenant_id=tenant_id,
            identities=[(IdentityKind.EMAIL, "owned@x.com")],
        )
        before = customer_repo.count(tenant_id=tenant_id)
        with pytest.raises(IdentityCollision):
            customer_repo.create_customer(
                tenant_id=tenant_id,
                identities=[(IdentityKind.EMAIL, "owned@x.com")],
            )
        # No new customer row was created (transaction rolled back).
        assert customer_repo.count(tenant_id=tenant_id) == before

    def test_unknown_customer_raises_keyerror_on_add_identity(
        self, customer_repo, tenant_id: str  # type: ignore[no-untyped-def]
    ) -> None:
        with pytest.raises(KeyError):
            customer_repo.add_identity(
                tenant_id=tenant_id,
                customer_id="cust_does_not_exist",
                identity_kind=IdentityKind.EMAIL,
                identity_value="x@y.com",
            )

    def test_list_identities_returns_all(
        self, customer_repo, tenant_id: str  # type: ignore[no-untyped-def]
    ) -> None:
        c = customer_repo.create_customer(
            tenant_id=tenant_id,
            identities=[
                (IdentityKind.EMAIL, "jane@acme.com"),
                (IdentityKind.PHONE, "+15550100100"),
            ],
        )
        idents = customer_repo.list_identities(
            tenant_id=tenant_id, customer_id=c.id
        )
        assert {i.identity_kind for i in idents} == {
            IdentityKind.EMAIL,
            IdentityKind.PHONE,
        }
        assert {i.identity_value for i in idents} == {
            "jane@acme.com",
            "+15550100100",
        }


# ---------------------------------------------------------------------------
# audit_log append-only trigger (migration 0002)
# ---------------------------------------------------------------------------


class TestAuditLogAppendOnly:
    """Verifies migration 0002 — UPDATE / DELETE / TRUNCATE on audit_log
    are blocked by triggers. Insert is fine."""

    def test_insert_then_update_rejected(
        self, conn_factory, tenant_id: str  # type: ignore[no-untyped-def]
    ) -> None:
        conn = conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO audit_log (tenant_id, kind, actor_kind, payload)
                    VALUES (%s, 'proposal', 'system', '{}'::jsonb)
                    RETURNING id
                    """,
                    (tenant_id,),
                )
                row = cur.fetchone()
                assert row is not None
                audit_id = row[0]
            conn.commit()

            # UPDATE must raise.
            from psycopg.errors import RaiseException  # type: ignore[import-not-found]

            with pytest.raises(RaiseException):
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE audit_log SET kind='decision' WHERE id=%s",
                        (audit_id,),
                    )
            conn.rollback()

            # DELETE must raise.
            with pytest.raises(RaiseException):
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM audit_log WHERE id=%s", (audit_id,)
                    )
            conn.rollback()
        finally:
            conn.close()
