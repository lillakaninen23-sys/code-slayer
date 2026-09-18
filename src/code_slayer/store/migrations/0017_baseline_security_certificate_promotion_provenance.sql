-- Code Slayer schema v17 (Baseline Security PRODUCTION promotion
-- provenance/idempotency, H.1).
--
-- Purely additive: one nullable column, one partial UNIQUE index. No
-- ALTER of any other table, no CHECK constraint on any existing
-- column, no rewrite or deletion of any existing row.
--
-- `promoted_from_validation_certificate_id` is set ONLY by
-- `code_slayer.security.production_promotion` when it durably carries
-- a `PASS` VALIDATION certificate forward into PRODUCTION -- the exact
-- server-reverified `certificate_id` of the VALIDATION row that
-- promotion re-verified (`code_slayer.workers.security_baseline.
-- record_baseline_certificate`'s own new, equally server-only
-- parameter of the same name). It is never a client-supplied value:
-- nothing in the WebUI/API surface accepts it, and the low-level
-- recorder never derives it from anything but its caller's own
-- already-durable VALIDATION certificate row.
--
-- Every OTHER certificate row leaves this column NULL: every ordinary
-- VALIDATION certificate ever recorded (a VALIDATION certificate is
-- never itself "promoted from" anything), every historical row from
-- before this column existed, and any future non-promotion PRODUCTION
-- certificate. NULL is a complete statement of "not a promotion",
-- never a wildcard -- the same convention `runtime_identity_
-- fingerprint` (schema v15) already established for this table.
--
-- The partial UNIQUE index enforces exactly and only the PRODUCTION
-- promotion-authority invariant H.1 needs: two rows can never claim
-- provenance from the SAME VALIDATION certificate. SQLite omits
-- NULL-valued rows from a partial index's WHERE clause entirely, so:
--
--   - ordinary VALIDATION certificate recording (this column always
--     NULL there) is completely unaffected and unrestricted -- exactly
--     as many rows as ever, including multiple rows that happen to
--     share the same (worker_id, runtime_identity_fingerprint,
--     evidence_ref) triple, which this schema has never forbidden;
--   - ordinary non-promotion certificate recording is unaffected
--     identically, for the same reason;
--   - a database already holding duplicate historical
--     (worker_id, runtime_identity_fingerprint, evidence_ref) triples
--     upgrades safely: this column is NULL on every existing row, so
--     the index applies to none of them, and the `ALTER TABLE ADD
--     COLUMN` step cannot fail or touch row content.
--
-- Concurrency: once two concurrent promotion attempts for the SAME
-- VALIDATION certificate both reach `record_baseline_certificate()`'s
-- own `BEGIN IMMEDIATE` write transaction, SQLite's write lock already
-- serializes their two INSERT attempts against each other; this index
-- is what makes the SECOND of those two serialized attempts fail with
-- `sqlite3.IntegrityError` instead of silently succeeding as a
-- duplicate PRODUCTION authority row for the same VALIDATION
-- provenance. `production_promotion` catches exactly that failure and
-- resolves it to the row the other attempt already committed.
--
-- A genuinely later, independent VALIDATION certificate (even against
-- the identical runtime and evidence content, however unlikely) has
-- its own distinct `certificate_id` and is therefore never
-- deduplicated against an earlier, different promotion.

BEGIN;

ALTER TABLE worker_baseline_security_certificates
  ADD COLUMN promoted_from_validation_certificate_id TEXT;

CREATE UNIQUE INDEX ux_worker_baseline_security_certificates_promotion_provenance
  ON worker_baseline_security_certificates(promoted_from_validation_certificate_id)
  WHERE promoted_from_validation_certificate_id IS NOT NULL;
