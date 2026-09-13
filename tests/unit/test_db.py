"""State store: DB creation, schema versioning, migration semantics."""

from __future__ import annotations

import sqlite3

import pytest

from code_slayer.store import db


def test_db_creation_creates_parent_dirs_and_file(tmp_path):
    path = tmp_path / "nested" / "dir" / "state.db"
    conn = db.connect(path)
    try:
        assert path.exists()
    finally:
        conn.close()


def test_schema_version_zero_before_migration(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    try:
        assert db.schema_version(conn) == 0
    finally:
        conn.close()


def test_migrate_applies_known_schema_version(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    try:
        version = db.migrate(conn)
        assert version == db.known_schema_version()
        assert version >= 1
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    try:
        first = db.migrate(conn)
        second = db.migrate(conn)
        assert first == second
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        versions = [r["version"] for r in rows]
        assert len(versions) == len(set(versions)), "a migration was applied twice"
    finally:
        conn.close()


def test_foreign_keys_enabled(db_conn):
    row = db_conn.execute("PRAGMA foreign_keys").fetchone()
    assert row[0] == 1


def test_reopen_after_clean_shutdown_preserves_data(tmp_path):
    path = tmp_path / "state.db"
    conn = db.connect(path)
    db.migrate(conn)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO tasks (task_id, description, repo_root, repo_id, "
            "worktree_id, created_at, updated_at, state) "
            "VALUES ('t1', 'd', '/r', 'repo-1', 'wt-1', 'now', 'now', 'CREATED')"
        )
    conn.close()

    reopened = db.connect(path)
    try:
        assert db.schema_version(reopened) == db.known_schema_version()
        row = reopened.execute("SELECT * FROM tasks WHERE task_id = 't1'").fetchone()
        assert row is not None
        assert row["state"] == "CREATED"
    finally:
        reopened.close()


def test_migration_failure_leaves_schema_and_version_unapplied(tmp_path, monkeypatch):
    """A migration that fails partway through must not leave the database
    at a half-applied version, and must not advance schema_migrations."""
    conn = db.connect(tmp_path / "state.db")
    try:
        db.migrate(conn)  # apply the real migration 0001 first
        before = db.schema_version(conn)

        broken_migrations = [
            (1, "init", "BOGUS"),  # will be filtered out by version <= current
            (
                before + 1,
                "broken",
                "BEGIN;\n"
                "CREATE TABLE bogus_ok (id INTEGER);\n"
                "CREATE TABLE bogus_ok (id INTEGER);\n",  # fails: table already exists
            ),
        ]
        monkeypatch.setattr(db, "_discover_migrations", lambda: broken_migrations)

        with pytest.raises(sqlite3.OperationalError):
            db.migrate(conn)

        assert db.schema_version(conn) == before, "version must not have advanced"
        assert not conn.in_transaction, "a failed migration must not leave an open transaction"
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'bogus_ok'"
        ).fetchone()
        assert row is None, "the failed migration's partial DDL must have been rolled back"
    finally:
        conn.close()


def test_newer_schema_version_fails_closed(tmp_path):
    """If the database claims a schema version newer than this code knows
    about, migrate() must refuse to open it rather than guess."""
    conn = db.connect(tmp_path / "state.db")
    try:
        db.migrate(conn)
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (db.known_schema_version() + 1000, "future"),
            )
        with pytest.raises(db.SchemaError):
            db.migrate(conn)
    finally:
        conn.close()


def test_transaction_rolls_back_on_exception(db_conn):
    with pytest.raises(RuntimeError):
        with db.transaction(db_conn):
            db_conn.execute(
                "INSERT INTO tasks (task_id, description, repo_root, repo_id, "
                "worktree_id, created_at, updated_at, state) "
                "VALUES ('t-rollback', 'd', '/r', 'repo-1', 'wt-1', 'now', 'now', 'CREATED')"
            )
            raise RuntimeError("boom")
    row = db_conn.execute("SELECT * FROM tasks WHERE task_id = 't-rollback'").fetchone()
    assert row is None
