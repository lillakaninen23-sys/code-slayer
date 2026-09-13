# ADR 0004 — Audit hash covers the full event tuple

## Status
Accepted (Foundation Plan v0.1, Revision 2.1, INV-4). Implemented in Phase 1.

## Context
Revision 1 of the Foundation Plan defined the audit chain hash as
`sha256(prev_event_hash + canonical_json(payload))` — payload only. Owner
review pointed out this does not detect a forged `actor`, `event_type`,
`occurred_at`, or `seq` if a row were edited directly in the database:
only the payload's own integrity was actually protected.

## Decision
`code_slayer.audit.canonical.event_hash()` computes:

```
event_hash = sha256(canonical_json({
    task_id, seq, event_type, occurred_at,
    actor_type, actor_id, payload, prev_event_hash,
}))
```

`canonical_json()` uses `json.dumps(sort_keys=True, separators=(",", ":"))`
so the same logical value always serializes identically regardless of
dict insertion order (including recursively, inside nested payload
objects) — a hard requirement for the hash to be reproducible across
processes and Python versions.

`code_slayer.audit.verify.verify_chain()` recomputes this hash for every
stored row and compares it, catching alteration of *any* of the covered
fields, not payload alone. `audit_events` also has `BEFORE UPDATE`/
`BEFORE DELETE` triggers that unconditionally abort — defense in depth
alongside `AuditWriter` being the only code path that ever writes to the
table.

This is explicitly an **integrity signal, not a security boundary**: it
lets Code Slayer detect that a row was altered outside of `AuditWriter`
(a bug, manual DB surgery, corruption); it does not stop a local user
with filesystem access from rewriting the database file wholesale,
recomputing a self-consistent chain, and defeating verification. No
threat model in the Foundation Plan requires it to.

## Alternatives considered
- **Payload-only hash (Revision 1).** Rejected for the reason above.
- **A cryptographic signature (e.g. HMAC with a secret key).** Out of
  scope: it would imply a key-management story local-first Code Slayer
  does not need for an integrity check against accidental corruption or
  in-process bugs, as opposed to a hostile actor with database access.

## Tested by
`tests/unit/test_audit.py`: hash-chain validity across many events;
tampering with `payload_json`, `event_type`, or `actor_id` directly on the
row (with the update trigger deliberately disabled, to isolate what
*verification* — as opposed to the trigger — catches) is detected as a
`hash_mismatch`; `UPDATE`/`DELETE` are separately rejected by the DB
trigger.
