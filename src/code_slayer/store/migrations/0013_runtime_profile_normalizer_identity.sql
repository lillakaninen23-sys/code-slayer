-- Code Slayer schema v13 (Tool Protocol Compatibility Layer identity).
--
-- Additive only: two nullable identity columns on each existing
-- certificate table. NULL/NULL means native-only transport (no
-- compatibility normalizer configured) -- which is exactly what every
-- previously recorded certificate actually was. Existing rows are
-- never rewritten.
--
-- These columns are part of RuntimeProfileIdentity's exact-match
-- binding (`workers.security_baseline` / `workers.production_
-- eligibility`): a certificate issued against a native-only runtime
-- must never silently authorize a runtime that uses a compatibility
-- normalizer, and vice versa. This migration does not issue, upgrade,
-- or invalidate any certificate.
--
-- ALTER TABLE ADD COLUMN does not fire the append-only UPDATE abort
-- triggers on these tables.

BEGIN;

ALTER TABLE worker_baseline_security_certificates ADD COLUMN normalizer_id TEXT;
ALTER TABLE worker_baseline_security_certificates ADD COLUMN normalizer_version INTEGER;

ALTER TABLE worker_role_certificates ADD COLUMN normalizer_id TEXT;
ALTER TABLE worker_role_certificates ADD COLUMN normalizer_version INTEGER;
