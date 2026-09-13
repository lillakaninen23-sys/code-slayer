"""Content-addressed evidence/blob store."""

from __future__ import annotations

import os
import stat

import pytest

from code_slayer.store.content_store import BlobTooLargeError, ContentStore


@pytest.fixture
def store(db_conn, tmp_path) -> ContentStore:
    return ContentStore(db_conn, tmp_path / "blobs", max_bytes=1024)


def test_identical_bytes_deduplicate(store):
    a = store.put(b"same bytes", media_type="text/plain", source_kind="test")
    b = store.put(b"same bytes", media_type="text/plain", source_kind="test")
    assert a.content_hash == b.content_hash
    files = list((store._blobs_dir).rglob("*"))
    stored_files = [f for f in files if f.is_file()]
    assert len(stored_files) == 1


def test_different_bytes_get_different_ids(store):
    a = store.put(b"content A", media_type="text/plain", source_kind="test")
    b = store.put(b"content B", media_type="text/plain", source_kind="test")
    assert a.content_hash != b.content_hash


def test_blob_is_immutable(store):
    meta = store.put(b"immutable content", media_type="text/plain", source_kind="test")
    path = store._path_for(meta.content_hash)
    mode = path.stat().st_mode
    assert not (mode & stat.S_IWUSR), "stored blob must not be user-writable"

    # A second put() with the same bytes but a different classification
    # must not silently reclassify the already-stored blob.
    again = store.put(
        b"immutable content", media_type="text/plain", source_kind="different_kind",
        exportable=True,
    )
    assert again.source_kind == "test"
    assert again.exportable is False


def test_exportable_defaults_false(store):
    meta = store.put(b"some evidence", media_type="text/plain", source_kind="command_output")
    assert meta.exportable is False


def test_exportable_can_be_set_explicitly(store):
    meta = store.put(
        b"a rules file", media_type="text/plain", source_kind="rules_snapshot", exportable=True
    )
    assert meta.exportable is True


def test_oversized_blob_rejected(store):
    with pytest.raises(BlobTooLargeError):
        store.put(b"x" * 2000, media_type="text/plain", source_kind="test")
    # and nothing was persisted
    row = store._conn.execute("SELECT COUNT(*) AS c FROM content_blobs").fetchone()
    assert row["c"] == 0


def test_read_round_trips(store):
    data = b"round trip me"
    meta = store.put(data, media_type="text/plain", source_kind="test")
    assert store.read(meta.content_hash) == data


def test_environment_value_not_accidentally_persisted(store, monkeypatch, tmp_path):
    """content_store.put()'s public API takes explicit bytes only; a
    caller cannot accidentally leak the process environment into a blob or
    its metadata through this API."""
    secret_marker = "CODESLAYER-TEST-SECRET-DO-NOT-PERSIST-8f2a1c"
    monkeypatch.setenv("SOME_SECRET_TOKEN", secret_marker)

    store.put(b"ordinary evidence, unrelated to env", media_type="text/plain", source_kind="test")
    store.put(os.environ.get("PATH", "").encode(), media_type="text/plain", source_kind="test")

    marker_bytes = secret_marker.encode()
    for path in (store._blobs_dir).rglob("*"):
        if path.is_file():
            assert marker_bytes not in path.read_bytes()
    rows = store._conn.execute("SELECT * FROM content_blobs").fetchall()
    for row in rows:
        for value in row.keys():
            assert secret_marker not in str(row[value])
