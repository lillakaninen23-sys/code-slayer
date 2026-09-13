"""Append-only, hash-chained audit log.

`AuditWriter` (writer.py) is the sole write path into `audit_events`.
`verify.py` recomputes the chain to detect tampering or corruption — an
integrity signal, not a security boundary (Foundation Plan §07).
"""
