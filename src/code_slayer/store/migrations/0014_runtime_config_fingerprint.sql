-- Code Slayer schema v14 (qualification-relevant runtime configuration
-- fingerprint).
--
-- Additive only: one nullable identity column on each existing
-- certificate table. This is a FORWARD migration for databases that
-- have already applied schema v13; 0013 is never rewritten.
--
-- `runtime_config_fingerprint` is the SHA-256 of the canonical
-- `runtime-config-spec-v1` document (`workers.security_baseline`).
-- NULL means the certificate was recorded before this identity
-- component existed (or an evaluation that never established the
-- qualification-relevant inference/runtime configuration). Existing
-- rows are never rewritten.
--
-- Exact-match production eligibility (`RuntimeProfileIdentity.matches`
-- / `is_fully_specified`) treats NULL as a missing required production
-- identity component: an old certificate cannot silently authorize a
-- fully-specified current runtime, and a current profile without a
-- fingerprint cannot be treated as production-authoritative. This
-- migration does not issue, upgrade, or invalidate any certificate.
--
-- ALTER TABLE ADD COLUMN does not fire the append-only UPDATE abort
-- triggers on these tables.

BEGIN;

ALTER TABLE worker_baseline_security_certificates
  ADD COLUMN runtime_config_fingerprint TEXT;

ALTER TABLE worker_role_certificates
  ADD COLUMN runtime_config_fingerprint TEXT;
