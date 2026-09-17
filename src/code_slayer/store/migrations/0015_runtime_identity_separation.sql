-- Code Slayer schema v15 (common runtime identity vs role/evaluation
-- identity).
--
-- Additive only: new nullable identity columns. This is a FORWARD
-- migration for databases that have already applied schema v14;
-- 0013 and 0014 are never rewritten, and existing certificate rows
-- are never rewritten.
--
-- `runtime_identity_fingerprint` is the SHA-256 of the canonical
-- `runtime-identity-spec-v2` document (`workers.security_baseline`).
-- It is the role-independent exact-match identity of the concrete
-- worker runtime (model/provider/endpoint/runtime/normalizer/context
-- capacity/sampling temperature). It does NOT include role/request-
-- specific fields such as output_token_budget or
-- tool_choice_enforcement.
--
-- `role_evaluation_fingerprint` is the SHA-256 of the canonical
-- `role-evaluation-spec-v1` document (`workers.role_qualification`).
-- It binds a role certificate to the evaluation configuration that
-- materially affected that role's qualification (common runtime
-- identity fingerprint, output_token_budget, tool_choice_enforcement,
-- role, policy/version). Baseline Security certificates do not carry
-- this column: they must not reuse a Planner (or any other role)
-- evaluation profile.
--
-- `runtime_config_fingerprint` (schema v14, `runtime-config-spec-v1`)
-- is left untouched and is NEVER reinterpreted under v2 semantics.
-- A v1 hash is historical evidence of the identity space that
-- produced it. NULL in any of the new columns means the certificate
-- was recorded before this identity component existed. NULL is never
-- a wildcard: production eligibility fails closed against a current
-- fully specified profile that has the new identity components.
--
-- ALTER TABLE ADD COLUMN does not fire the append-only UPDATE abort
-- triggers on these tables. This migration does not issue, upgrade,
-- or invalidate any certificate.

BEGIN;

ALTER TABLE worker_baseline_security_certificates
  ADD COLUMN runtime_identity_fingerprint TEXT;

ALTER TABLE worker_role_certificates
  ADD COLUMN runtime_identity_fingerprint TEXT;

ALTER TABLE worker_role_certificates
  ADD COLUMN role_evaluation_fingerprint TEXT;
