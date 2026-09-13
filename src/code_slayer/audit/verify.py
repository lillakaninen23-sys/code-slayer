"""Audit hash-chain verification — an integrity check, not a security
boundary (Foundation Plan §07).

Recomputes every event's hash from its stored fields and checks sequence
and previous-hash linkage. Because the hash covers the full event tuple,
altering any single stored field (payload, event_type, actor, timestamp,
sequence) after the fact is caught as a hash mismatch, not just a
payload check.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from code_slayer.audit.canonical import event_hash


@dataclass(frozen=True)
class VerificationIssue:
    row_id: int
    task_id: str | None
    seq: int
    reason: str  # seq_gap_or_out_of_order | prev_hash_mismatch | hash_mismatch


@dataclass(frozen=True)
class VerificationResult:
    events_checked: int
    issues: list[VerificationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def verify_chain(conn: sqlite3.Connection, *, task_id: str | None = None) -> VerificationResult:
    """Verify one task's chain (`task_id` given) or every task's chain
    independently (`task_id=None`)."""
    if task_id is not None:
        task_ids: list[str | None] = [task_id]
    else:
        task_ids = [r["task_id"] for r in conn.execute(
            "SELECT DISTINCT task_id FROM audit_events"
        ).fetchall()]

    issues: list[VerificationIssue] = []
    checked = 0
    for tid in task_ids:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE task_id IS ? ORDER BY seq ASC", (tid,)
        ).fetchall()
        result = _verify_rows(rows)
        issues.extend(result.issues)
        checked += result.events_checked
    return VerificationResult(events_checked=checked, issues=issues)


def _verify_rows(rows: list[sqlite3.Row]) -> VerificationResult:
    issues: list[VerificationIssue] = []
    expected_prev: str | None = None
    expected_seq = 1
    for row in rows:
        if row["seq"] != expected_seq:
            issues.append(
                VerificationIssue(row["id"], row["task_id"], row["seq"], "seq_gap_or_out_of_order")
            )
        if row["prev_event_hash"] != expected_prev:
            issues.append(
                VerificationIssue(row["id"], row["task_id"], row["seq"], "prev_hash_mismatch")
            )
        recomputed = event_hash(
            task_id=row["task_id"],
            seq=row["seq"],
            event_type=row["event_type"],
            occurred_at=row["occurred_at"],
            actor_type=row["actor_type"],
            actor_id=row["actor_id"],
            payload=json.loads(row["payload_json"]),
            prev_event_hash=row["prev_event_hash"],
        )
        if recomputed != row["event_hash"]:
            issues.append(
                VerificationIssue(row["id"], row["task_id"], row["seq"], "hash_mismatch")
            )
        expected_prev = row["event_hash"]
        expected_seq = row["seq"] + 1
    return VerificationResult(events_checked=len(rows), issues=issues)
