"""PennyCore migration runner.

Applies every `.sql` file in `migrations/versions/` in alphabetical order
against the database pointed to by `DATABASE_URL`. Idempotent: a
`schema_migrations` ledger records which files have been applied, and
re-running is a no-op once they're all in.

Why a 60-line runner instead of Alembic:
  * Alembic drags SQLAlchemy in as a runtime dep; we don't otherwise need
    SQLAlchemy (the repositories use psycopg3 directly).
  * The migration SQL files in `migrations/versions/` are also consumed
    by `docker-entrypoint-initdb.d` on fresh container boots — that's the
    primary boot path. This script handles the secondary path: applying
    new migrations to an already-running database.
  * Adding Alembic later is a pure superset — the .sql files become
    `op.execute(open(...).read())` Python migrations and `alembic
    stamp head` records the current state. No schema rework needed.

Usage:
    DATABASE_URL=postgresql://pennycore:pennycore_local_dev@localhost:5432/pennycore \\
        python scripts/migrate.py

Or via docker compose (Postgres host = `postgres`):
    docker compose run --rm context_engine python scripts/migrate.py

Exit codes:
    0 — all migrations applied (or already up-to-date)
    1 — DATABASE_URL not set, or a migration failed mid-flight
    2 — file ordering issue (migration file numbering not strictly increasing)
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# psycopg is imported lazily so this module is import-clean in environments
# that don't have the binary wheel installed (CI for unit tests, etc.).


REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations" / "versions"
MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def discover_migrations() -> list[Path]:
    """Return migration files sorted by their leading 4-digit prefix.

    Validates that the prefixes are strictly increasing (no gaps OK,
    duplicates not OK — would produce ambiguous ordering).
    """
    if not MIGRATIONS_DIR.is_dir():
        print(f"migrate: no migrations directory at {MIGRATIONS_DIR}", file=sys.stderr)
        sys.exit(1)

    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    seen_prefixes: set[str] = set()
    for f in files:
        m = MIGRATION_FILENAME_RE.match(f.name)
        if m is None:
            print(
                f"migrate: filename does not match NNNN_name.sql: {f.name}",
                file=sys.stderr,
            )
            sys.exit(2)
        prefix = m.group(1)
        if prefix in seen_prefixes:
            print(f"migrate: duplicate migration prefix {prefix!r}", file=sys.stderr)
            sys.exit(2)
        seen_prefixes.add(prefix)
    return files


def ensure_ledger(conn) -> None:
    """Create the `schema_migrations` ledger if it doesn't exist."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename    TEXT PRIMARY KEY,
                applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                checksum    TEXT NOT NULL
            )
            """
        )
    conn.commit()


def applied_filenames(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def apply_one(conn, path: Path) -> None:
    """Apply one migration in a single transaction.

    Records the file's content checksum in the ledger so a future
    accidental edit shows up as drift on the next run.
    """
    import hashlib

    sql = path.read_text(encoding="utf-8")
    checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()

    print(f"migrate: applying {path.name} (sha256={checksum[:12]}...)")
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
            (path.name, checksum),
        )
    conn.commit()


def main() -> int:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print(
            "migrate: DATABASE_URL not set (e.g. "
            "postgresql://user:pass@host:5432/db)",
            file=sys.stderr,
        )
        return 1

    try:
        import psycopg
    except ImportError:
        print(
            "migrate: psycopg not installed — `pip install psycopg[binary]`",
            file=sys.stderr,
        )
        return 1

    migrations = discover_migrations()
    if not migrations:
        print("migrate: no migration files found")
        return 0

    with psycopg.connect(database_url) as conn:
        ensure_ledger(conn)
        already = applied_filenames(conn)

        pending = [m for m in migrations if m.name not in already]
        if not pending:
            print(f"migrate: up-to-date ({len(already)} applied)")
            return 0

        print(f"migrate: applying {len(pending)} new migration(s)")
        for m in pending:
            try:
                apply_one(conn, m)
            except Exception as exc:
                print(
                    f"migrate: FAILED on {m.name}: {exc}",
                    file=sys.stderr,
                )
                conn.rollback()
                return 1

        print(f"migrate: done ({len(already) + len(pending)} total applied)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
