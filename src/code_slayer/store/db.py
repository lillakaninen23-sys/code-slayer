"""SQLite connection management, pragmas, transaction boundaries, and the
migration runner.

Foundation Plan §05/§06/§15, Revision 2.1.

Be explicit about what is, and is not, atomic: everything in this module
gives ACID transactions over Code Slayer's *own* rows. It says nothing
about, and is never used to claim, atomicity for a filesystem write, a Git
plumbing call, or a subprocess exec — that gap is what the tool-operation
journal (`code_slayer.store.tool_operations_repo`) exists to cover.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

BUSY_TIMEOUT_MS = 5000
MIGRATIONS_PACKAGE = "code_slayer.store.migrations"


class SchemaError(RuntimeError):
    """The database's schema is missing, unreadable, or from a newer,
    incompatible version of Code Slayer than this code understands.

    Foundation Plan requirement: unknown/newer incompatible schema fails
    closed rather than guessing.
    """


def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a connection with Code Slayer's required pragmas set.

    `autocommit=True` (true autocommit; Python 3.12+) means this module
    never issues an implicit BEGIN of its own — every transaction in this
    codebase is opened and closed explicitly via `transaction()` below, so
    there is never ambiguity about what is, or is not, currently atomic.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), autocommit=True)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS};")
    conn.row_factory = sqlite3.Row
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection):
    """The one place a transaction is opened and closed in this codebase.

    Usage: `with transaction(conn): ...`. Commits on normal exit, rolls
    back on any exception, and re-raises. Not re-entrant — nothing in
    Phase 1 nests calls to this, and nesting is deliberately left
    unsupported rather than silently doing the wrong thing (see
    known-limitations in adr/0001-sqlite-state-store.md).
    """
    if conn.in_transaction:
        raise RuntimeError(
            "transaction() called while a transaction is already open — "
            "nested transactions are not supported"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _discover_migrations() -> list[tuple[int, str, str]]:
    """Return `[(version, name, sql), ...]`, sorted by version, loaded from
    the packaged `migrations/` directory."""
    migrations: list[tuple[int, str, str]] = []
    root = resources.files(MIGRATIONS_PACKAGE)
    for entry in root.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        stem = entry.name[: -len(".sql")]
        version_str, _, name = stem.partition("_")
        migrations.append((int(version_str), name, entry.read_text(encoding="utf-8")))
    migrations.sort(key=lambda m: m[0])
    return migrations


def known_schema_version() -> int:
    """The highest migration version this installed code understands."""
    migrations = _discover_migrations()
    return max((v for v, _, _ in migrations), default=0)


def schema_version(conn: sqlite3.Connection) -> int:
    """The schema version currently applied to `conn`'s database, 0 if none."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if row is None:
        return 0
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return row["v"] or 0


def _sql_without_leading_comments(sql: str) -> str:
    """Strip leading blank lines and `--` line comments so the atomicity
    check below looks at the first real statement, not a docstring header."""
    lines = sql.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped == "" or stripped.startswith("--"):
            index += 1
            continue
        break
    return "\n".join(lines[index:])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply every pending migration, in order, each atomically with its own
    `schema_migrations` row.

    Idempotent: a migration already recorded in `schema_migrations` is
    never re-applied. Fails closed (`SchemaError`) if the database's
    recorded version is newer than any migration this code knows about,
    rather than guessing that it is still compatible.
    """
    migrations = _discover_migrations()
    known_max = max((v for v, _, _ in migrations), default=0)
    current = schema_version(conn)
    if current > known_max:
        raise SchemaError(
            f"database schema version {current} is newer than the highest "
            f"version this installed code understands ({known_max}); "
            "refusing to open it rather than risk misinterpreting it"
        )
    for version, _name, sql in migrations:
        if version <= current:
            continue
        if not _sql_without_leading_comments(sql).lstrip().startswith("BEGIN;"):
            raise SchemaError(
                f"migration {version} does not start with 'BEGIN;' — refusing to "
                "apply a migration whose atomicity this runner cannot guarantee"
            )
        if conn.in_transaction:
            raise SchemaError(
                f"cannot apply migration {version}: a transaction is already open"
            )
        try:
            conn.executescript(sql)  # runs the migration's own BEGIN; + DDL
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, utcnow_iso()),
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return schema_version(conn)
