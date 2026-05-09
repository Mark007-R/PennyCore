"""Postgres adapter for `EventRepository` (Day 6 follow-up; was deferred
from Day 5 to "Day 6+" until the linker actually needed cross-channel
joins to make persistence pay off).

Design:
  * `psycopg3` direct, no SQLAlchemy. The repository shape is narrow
    (~5 methods); ORM machinery would obscure the SQL without buying
    anything. Hand-written SQL keeps the multi-tenant `WHERE
    tenant_id = %s` predicates visible at every call site, which is
    the exact discipline rule 15 demands.
  * Connections are obtained from a caller-supplied factory
    (`Callable[[], Connection]`) so tests can inject a context-managed
    connection without psycopg's pool. The factory pattern survives a
    later swap to `psycopg_pool.ConnectionPool` — `pool.connection`
    satisfies the same signature.
  * Idempotency oracle: `UNIQUE (tenant_id, idempotency_key)` from
    `migrations/versions/0001_initial_schema.sql §5`. The `INSERT ...
    ON CONFLICT DO NOTHING RETURNING` pattern + a fallback SELECT
    handles both the new-row and replay cases in at most two
    statements per call.
  * Tenant isolation is enforced at the SQL level — every read
    predicates on `tenant_id`. There is no method that returns rows
    for "all tenants" in production use; `count(tenant_id=None)` is
    only there for the test parity contract with the in-memory
    adapter.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator

from contracts import ChannelType, Event, EventType

if TYPE_CHECKING:
    from psycopg import Connection


# Type alias — caller supplies a zero-arg factory that yields a
# psycopg connection. The repository takes ownership for the duration
# of one method call (open → use → close), which is the right granularity
# for HTTP request-response: short-lived, no long-held connections.
ConnectionFactory = Callable[[], "Connection"]


class PgEventRepository:
    """Postgres-backed event store.

    Satisfies `context_engine.repository.EventRepository` (the
    `runtime_checkable` Protocol). Swap-in is a one-liner in
    `context_engine/api.py:get_repo`.
    """

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._conn_factory = connection_factory

    @contextmanager
    def _connection(self) -> Iterator["Connection"]:
        conn = self._conn_factory()
        try:
            yield conn
        finally:
            conn.close()

    def upsert_event(self, event: Event) -> tuple[Event, bool]:
        """Insert if new; return existing on idempotency-key conflict.

        Two-phase: (1) INSERT ... ON CONFLICT DO NOTHING RETURNING id
        — if it returns a row, we created. (2) If it returned no row,
        SELECT the existing row by `(tenant_id, idempotency_key)` and
        return that as the canonical event.

        Whole thing runs in one transaction so a concurrent inserter
        can't slip in between the two statements (the SELECT-after-
        conflict sees the OTHER inserter's row, which is exactly what
        the replay semantics require).
        """
        insert_sql = """
            INSERT INTO events
                (id, tenant_id, customer_id, channel_code, event_type,
                 idempotency_key, payload, received_at)
            VALUES
                (%(id)s, %(tenant_id)s, %(customer_id)s, %(channel_code)s,
                 %(event_type)s, %(idempotency_key)s, %(payload)s,
                 %(received_at)s)
            ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
            RETURNING id
        """
        select_sql = """
            SELECT id, tenant_id, customer_id, channel_code, event_type,
                   idempotency_key, payload, received_at
              FROM events
             WHERE tenant_id = %s AND idempotency_key = %s
        """
        params = self._event_to_row(event)

        with self._connection() as conn:
            with conn.cursor() as cur:
                cur.execute(insert_sql, params)
                inserted = cur.fetchone()

                if inserted is not None:
                    conn.commit()
                    return event, True

                # Conflict path — fetch the canonical existing row.
                cur.execute(
                    select_sql,
                    (event.tenant_id, event.idempotency_key),
                )
                existing_row = cur.fetchone()
            conn.commit()

        if existing_row is None:
            # Should never happen — conflict means a row exists. Defensive.
            raise RuntimeError(
                "upsert_event: ON CONFLICT path returned no existing row"
            )
        return self._row_to_event(existing_row), False

    def get_event_by_idempotency(
        self, *, tenant_id: str, idempotency_key: str
    ) -> Event | None:
        sql = """
            SELECT id, tenant_id, customer_id, channel_code, event_type,
                   idempotency_key, payload, received_at
              FROM events
             WHERE tenant_id = %s AND idempotency_key = %s
        """
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (tenant_id, idempotency_key))
            row = cur.fetchone()
        return self._row_to_event(row) if row else None

    def get_event_by_id(
        self, *, tenant_id: str, event_id: str
    ) -> Event | None:
        sql = """
            SELECT id, tenant_id, customer_id, channel_code, event_type,
                   idempotency_key, payload, received_at
              FROM events
             WHERE tenant_id = %s AND id = %s
        """
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(sql, (tenant_id, event_id))
            row = cur.fetchone()
        return self._row_to_event(row) if row else None

    def count(self, *, tenant_id: str | None = None) -> int:
        if tenant_id is None:
            sql = "SELECT COUNT(*) FROM events"
            params: tuple[Any, ...] = ()
        else:
            sql = "SELECT COUNT(*) FROM events WHERE tenant_id = %s"
            params = (tenant_id,)
        with self._connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Row <-> Event mapping
    # ------------------------------------------------------------------

    @staticmethod
    def _event_to_row(event: Event) -> dict[str, Any]:
        # psycopg3 adapts dict[str, Any] → JSONB automatically when the
        # column type is JSONB; we just hand it the dict.
        return {
            "id": event.id,
            "tenant_id": event.tenant_id,
            "customer_id": event.customer_id,
            "channel_code": event.channel_code.value,
            "event_type": event.event_type.value,
            "idempotency_key": event.idempotency_key,
            "payload": _Jsonb(event.payload),
            "received_at": event.received_at,
        }

    @staticmethod
    def _row_to_event(row: Any) -> Event:
        # row order matches the SELECT lists above.
        return Event(
            id=row[0],
            tenant_id=row[1],
            customer_id=row[2],
            channel_code=ChannelType(row[3]),
            event_type=EventType(row[4]),
            idempotency_key=row[5],
            payload=row[6] if row[6] is not None else {},
            received_at=row[7],
        )


def _Jsonb(value: Any) -> Any:  # noqa: N802 — wrapper name mirrors psycopg's class
    """Wrap a Python dict as a psycopg `Jsonb` adapter when psycopg is
    importable; otherwise return as-is (typed code path tests can stub
    the connection without psycopg installed).

    Importing inside the function keeps the module import-clean in
    environments missing psycopg.
    """
    try:
        from psycopg.types.json import Jsonb
    except ImportError:
        return value
    return Jsonb(value) if value is not None else None
