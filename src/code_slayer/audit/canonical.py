"""Canonical JSON serialization and the audit event hash.

Foundation Plan §07, Revision 2.1: the hash covers the full event tuple —
task_id, seq, event_type, occurred_at, actor_type, actor_id, payload,
prev_event_hash — not payload alone, so a forged actor, timestamp,
sequence number, or event type is also detectable on re-verification.
This is an integrity signal, not a security boundary: it does not stop a
local user with filesystem access from rewriting the database file.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """A deterministic JSON serialization: sorted keys (recursively, since
    `json.dumps(sort_keys=True)` sorts nested dict keys too), fixed
    separators (no incidental whitespace differences), UTF-8 safe."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_hash(
    *,
    task_id: str | None,
    seq: int,
    event_type: str,
    occurred_at: str,
    actor_type: str,
    actor_id: str | None,
    payload: dict[str, Any],
    prev_event_hash: str | None,
) -> str:
    """Compute one audit event's hash over its full, canonical tuple."""
    tuple_repr = {
        "task_id": task_id,
        "seq": seq,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "payload": payload,
        "prev_event_hash": prev_event_hash,
    }
    return hashlib.sha256(canonical_json(tuple_repr).encode("utf-8")).hexdigest()
