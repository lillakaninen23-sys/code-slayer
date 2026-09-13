"""Content-addressed evidence/blob store foundation (Foundation Plan §07/§11,
Revision 2.1).

A blob's identity is the SHA-256 of its raw bytes. Metadata records what
kind of evidence it is (`source_kind`) and whether it may ever leave the
machine (`exportable`), which defaults to `False` and must be set
explicitly by the caller. This module makes no judgment about what
*should* be exportable — that belongs to whatever later produces the
bytes. Nothing here inspects byte content for secret-looking patterns; no
heuristic secret scanner is used or required.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from code_slayer.store.models import ContentBlob

DEFAULT_MAX_BLOB_BYTES = 10 * 1024 * 1024  # 10 MiB (Foundation Plan §17)


class BlobTooLargeError(RuntimeError):
    pass


def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ContentStore:
    """Content-addressed blob storage: SQLite metadata + files on disk."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        blobs_dir: Path | str,
        *,
        max_bytes: int = DEFAULT_MAX_BLOB_BYTES,
    ) -> None:
        self._conn = conn
        self._blobs_dir = Path(blobs_dir)
        self._max_bytes = max_bytes
        self._blobs_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, content_hash: str) -> Path:
        return self._blobs_dir / content_hash[:2] / content_hash

    def put(
        self,
        data: bytes,
        *,
        media_type: str,
        source_kind: str,
        exportable: bool = False,
        truncated: bool = False,
    ) -> ContentBlob:
        """Store `data`, deduplicating by content hash.

        Immutable once stored: calling `put()` again with identical bytes
        is a safe no-op that returns the existing metadata unchanged — it
        never rewrites the file or reclassifies an already-stored blob's
        `source_kind`/`exportable` from a later, different call.
        """
        if len(data) > self._max_bytes:
            raise BlobTooLargeError(
                f"blob of {len(data)} bytes exceeds max_bytes={self._max_bytes}"
            )
        content_hash = hashlib.sha256(data).hexdigest()
        existing = self.get_meta(content_hash)
        if existing is not None:
            return existing

        path = self._path_for(content_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_bytes(data)
        tmp_path.replace(path)  # atomic rename within the same filesystem
        path.chmod(0o444)  # read-only: an extra, verifiable immutability signal

        created_at = utcnow_iso()
        self._conn.execute(
            "INSERT INTO content_blobs "
            "(content_hash, media_type, source_kind, byte_size, truncated, "
            " exportable, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (content_hash, media_type, source_kind, len(data), int(truncated),
             1 if exportable else 0, created_at),
        )
        meta = self.get_meta(content_hash)
        assert meta is not None
        return meta

    def get_meta(self, content_hash: str) -> ContentBlob | None:
        row = self._conn.execute(
            "SELECT * FROM content_blobs WHERE content_hash = ?", (content_hash,)
        ).fetchone()
        if row is None:
            return None
        return ContentBlob(
            content_hash=row["content_hash"],
            media_type=row["media_type"],
            source_kind=row["source_kind"],
            byte_size=row["byte_size"],
            truncated=bool(row["truncated"]),
            exportable=bool(row["exportable"]),
            created_at=row["created_at"],
        )

    def read(self, content_hash: str) -> bytes:
        meta = self.get_meta(content_hash)
        if meta is None:
            raise KeyError(f"no such blob: {content_hash}")
        return self._path_for(content_hash).read_bytes()
