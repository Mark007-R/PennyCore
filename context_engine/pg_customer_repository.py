"""Postgres adapter for `CustomerRepository` (Day 6 follow-up).

Satisfies `context_engine.customer_repository.CustomerRepository`. The
in-memory adapter ships a thread-safe lock + three dicts; the Postgres
adapter relies on the DDL: `UNIQUE (tenant_id, identity_kind,
identity_value)` on `customer_identities` is the same join oracle, now
enforced by the database itself.

Key design choices:
  * `IdentityCollision` re-raises on UNIQUE-violation. psycopg surfaces
    these as `psycopg.errors.UniqueViolation` (SQLSTATE 23505); we map
    that to the same exception the in-memory adapter raises so the
    linker's collision-handling path is provider-agnostic.
  * `add_identity` is idempotent: if the identity already points at
    this same customer, return the existing row; if it points at a
    DIFFERENT customer, raise. Same shape as the in-memory adapter.
  * `create_customer` runs the customer INSERT + every identity INSERT
    inside a single transaction. A collision on any identity rolls
    back the whole batch — atomicity is the only correctness-preserving
    semantics under concurrent ingestion.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterable, Iterator

from context_engine.customer_repository import IdentityCollision
from context_engine.pg_repository import ConnectionFactory
from contracts import Customer, CustomerIdentity, IdentityKind

if TYPE_CHECKING:
    from psycopg import Connection


def _new_customer_id() -> str:
    return f"cust_{uuid.uuid4().hex[:24]}"


def _new_identity_id() -> str:
    return f"idn_{uuid.uuid4().hex[:24]}"


def _ident_index_value(kind: IdentityKind, value: str) -> str:
    """Mirror the in-memory adapter's `_ident_index_value` so the index
    key inserted matches what `CustomerIdentity`'s validator would
    produce. The in-memory module is the source of truth; this is the
    duplicate that lives at the Postgres adapter site."""
    v = value.strip()
    if kind in (IdentityKind.EMAIL, IdentityKind.CHAT_HANDLE):
        v = v.lower()
    return v


class PgCustomerRepository:
    """Postgres-backed customer + identity store."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._conn_factory = connection_factory

    @contextmanager
    def _connection(self) -> Iterator["Connection"]:
        conn = self._conn_factory()
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_customer(self, *, tenant_id: str, customer_id: str) -> Customer | None:
        sql = """
            SELECT id, tenant_id, display_name, created_at, updated_at, metadata
              FROM customers
             WHERE tenant_id = %s AND id = %s
        """
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (tenant_id, customer_id))
            row = cur.fetchone()
        return self._row_to_customer(row) if row else None

    def find_customer_by_identity(
        self,
        *,
        tenant_id: str,
        identity_kind: IdentityKind,
        identity_value: str,
    ) -> Customer | None:
        sql = """
            SELECT c.id, c.tenant_id, c.display_name, c.created_at,
                   c.updated_at, c.metadata
              FROM customers c
              JOIN customer_identities ci ON ci.customer_id = c.id
                                          AND ci.tenant_id   = c.tenant_id
             WHERE c.tenant_id    = %s
               AND ci.identity_kind  = %s
               AND ci.identity_value = %s
        """
        normalized = _ident_index_value(identity_kind, identity_value)
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (tenant_id, identity_kind.value, normalized))
            row = cur.fetchone()
        return self._row_to_customer(row) if row else None

    def list_identities(
        self, *, tenant_id: str, customer_id: str
    ) -> list[CustomerIdentity]:
        sql = """
            SELECT id, tenant_id, customer_id, identity_kind, identity_value,
                   created_at
              FROM customer_identities
             WHERE tenant_id = %s AND customer_id = %s
             ORDER BY created_at ASC, id ASC
        """
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (tenant_id, customer_id))
            return [self._row_to_identity(r) for r in cur.fetchall()]

    def count(self, *, tenant_id: str | None = None) -> int:
        if tenant_id is None:
            sql = "SELECT COUNT(*) FROM customers"
            params: tuple[Any, ...] = ()
        else:
            sql = "SELECT COUNT(*) FROM customers WHERE tenant_id = %s"
            params = (tenant_id,)
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def create_customer(
        self,
        *,
        tenant_id: str,
        identities: Iterable[tuple[IdentityKind, str]],
        display_name: str | None = None,
    ) -> Customer:
        identity_list = list(identities)
        customer_id = _new_customer_id()

        with self._connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO customers (id, tenant_id, display_name)
                        VALUES (%s, %s, %s)
                        RETURNING id, tenant_id, display_name,
                                  created_at, updated_at, metadata
                        """,
                        (customer_id, tenant_id, display_name),
                    )
                    customer_row = cur.fetchone()

                    for kind, value in identity_list:
                        normalized = _ident_index_value(kind, value)
                        cur.execute(
                            """
                            INSERT INTO customer_identities
                                (id, tenant_id, customer_id, identity_kind,
                                 identity_value)
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (
                                _new_identity_id(),
                                tenant_id,
                                customer_id,
                                kind.value,
                                normalized,
                            ),
                        )
                conn.commit()
            except Exception as exc:
                conn.rollback()
                if _is_unique_violation(exc):
                    raise IdentityCollision(
                        f"identity collision while creating customer in "
                        f"tenant {tenant_id!r}: {exc}"
                    ) from exc
                raise

        if customer_row is None:
            raise RuntimeError("create_customer: INSERT returned no row")
        return self._row_to_customer(customer_row)

    def add_identity(
        self,
        *,
        tenant_id: str,
        customer_id: str,
        identity_kind: IdentityKind,
        identity_value: str,
    ) -> CustomerIdentity:
        normalized = _ident_index_value(identity_kind, identity_value)

        with self._connection() as conn:
            with conn.cursor() as cur:
                # First check for an existing identity row at the index
                # key. This handles the idempotent re-add case + the
                # collision-different-customer case without paying for a
                # raised UniqueViolation on the happy idempotent path.
                cur.execute(
                    """
                    SELECT id, tenant_id, customer_id, identity_kind,
                           identity_value, created_at
                      FROM customer_identities
                     WHERE tenant_id      = %s
                       AND identity_kind  = %s
                       AND identity_value = %s
                    """,
                    (tenant_id, identity_kind.value, normalized),
                )
                existing = cur.fetchone()

                if existing is not None:
                    if existing[2] != customer_id:
                        raise IdentityCollision(
                            f"identity ({identity_kind.value}={identity_value!r}) "
                            f"already linked to customer {existing[2]!r}, "
                            f"cannot reattach to {customer_id!r}"
                        )
                    return self._row_to_identity(existing)

                # Customer existence check — KeyError mirrors in-memory adapter.
                cur.execute(
                    "SELECT 1 FROM customers WHERE tenant_id = %s AND id = %s",
                    (tenant_id, customer_id),
                )
                if cur.fetchone() is None:
                    raise KeyError(
                        f"customer {customer_id!r} not found in tenant "
                        f"{tenant_id!r}"
                    )

                # Insert; UNIQUE constraint is the safety net for races
                # (two concurrent inserts of the same identity → second
                # raises UniqueViolation, we catch + re-raise as
                # IdentityCollision).
                identity_id = _new_identity_id()
                try:
                    cur.execute(
                        """
                        INSERT INTO customer_identities
                            (id, tenant_id, customer_id, identity_kind,
                             identity_value)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING id, tenant_id, customer_id, identity_kind,
                                  identity_value, created_at
                        """,
                        (
                            identity_id,
                            tenant_id,
                            customer_id,
                            identity_kind.value,
                            normalized,
                        ),
                    )
                    inserted = cur.fetchone()
                except Exception as exc:
                    conn.rollback()
                    if _is_unique_violation(exc):
                        raise IdentityCollision(
                            f"concurrent insert collision on identity "
                            f"({identity_kind.value}={identity_value!r}) "
                            f"in tenant {tenant_id!r}"
                        ) from exc
                    raise
            conn.commit()

        if inserted is None:
            raise RuntimeError("add_identity: INSERT returned no row")
        return self._row_to_identity(inserted)

    # ------------------------------------------------------------------
    # Row mappers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_customer(row: Any) -> Customer:
        return Customer(
            id=row[0],
            tenant_id=row[1],
            display_name=row[2],
            created_at=row[3],
            updated_at=row[4],
            metadata=row[5] if row[5] is not None else {},
        )

    @staticmethod
    def _row_to_identity(row: Any) -> CustomerIdentity:
        return CustomerIdentity(
            id=row[0],
            tenant_id=row[1],
            customer_id=row[2],
            identity_kind=IdentityKind(row[3]),
            identity_value=row[4],
            created_at=row[5],
        )


def _is_unique_violation(exc: BaseException) -> bool:
    """True if `exc` is a psycopg UniqueViolation (SQLSTATE 23505).

    Importing `psycopg.errors` lazily so this module is import-clean
    when psycopg isn't installed (e.g. pure unit-test runs).
    """
    try:
        from psycopg.errors import UniqueViolation
    except ImportError:
        return False
    return isinstance(exc, UniqueViolation)


# Convenience: build a connection factory from a DATABASE_URL string.
def make_connection_factory(database_url: str) -> ConnectionFactory:
    """Return a callable that opens a fresh psycopg connection.

    Use this when you don't want to bring in a connection pool. For
    higher-throughput workloads, replace with
    `psycopg_pool.ConnectionPool(database_url).connection`.
    """

    def factory() -> "Connection":
        import psycopg
        return psycopg.connect(database_url)

    return factory
