# Role Certification V1

Status: implementation in review; no production workers recertified or activated.

Extends `workers.role_qualification`, `evaluate_production_eligibility`, append-only
role/baseline certificate repositories, ContentStore, the existing OpenAI-compatible
adapter and Ollama attestation. No database migration and no historical schema change.

`engineering-role-evidence-v1` freezes the case/check vocabulary and contains worker,
exact role, common runtime spec/hash, historical-format runtime config spec/hash,
role evaluation spec/hash, qualification policy, evaluated/start timestamps, code-owned
provenance, case observation hashes/checks, outcome and stable failure reason. The
certificate supplies issuance time, ID and the content-addressed evidence reference.
Existing v1/v2/v3 Planner evidence remains under its existing verifiers. Never upgrade
an old hash into a new schema. Qualification never grants baseline Security or tool trust.

Production `run_coding_job` accepts server configuration loading, not adapters. Four
roles are resolved before workspace preparation; every inference rechecks current
config, active lifecycle, exact runtime/evaluation, certificate identities, evidence
and freshness, then live-attests through the existing endpoint probe. Final checkpoint
also rechecks bindings. No role substitution, reroute, or uncertified fallback.
The explicitly named `coding.testing.run_coding_job_for_testing` is for injected tests.

Engineering qualifications and baseline evidence must be less than 30 days old,
measured from evaluation start, with ordered, timezone-aware timestamps. New role
policy is `engineering-role-certification-v1`. All cases must pass. Missing, malformed,
hash-mismatched, worker/role/config/evaluation-mismatched evidence fails closed.
Eligibility keeps canonical lifecycle/baseline/role denial reasons and adds
`missing_durable_role_evidence`, `missing_or_malformed_role_evidence`,
`role_certificate_evidence_mismatch`, `baseline_certificate_evidence_mismatch`,
`missing_or_malformed_baseline_evidence`, `certificate_expired`,
`certificate_timestamp_mismatch`, and schema/aggregate/fingerprint mismatch reasons.

Routing follows Planner's current zero/one/multiple rule: V1 pass/fail qualifications
do not establish comparative strength. Multiple eligible candidates fail closed;
no model name, configured score, certificate recency or arbitrary ordering pretends
to measure strength. Future measured ranking can refine selection without replacing
eligibility. Security is not hardcoded. Reviewer and Security exclude mutator worker
IDs and immutable model digests; Security additionally excludes the Reviewer.
Coder and Repairer may share a model only with separate exact-role certificates.
Model-family independence is not asserted: the registry has no verified family identity.

## Permission/security design review

Follows Verification Standard §§4,7, Security & Privacy §§3,5,7,10, Permissions Model
§§3–5 and Vision §§41–48. This adds role-qualified use of the EXISTING explicitly
configured local inference/attestation path; no discovery, new transport, destination,
cloud, credentials collection, service activation or live configuration mutation.
Only current approved local config candidates are used. Native protocol only; configured
normalizers are refused rather than silently dropped. Certification is an explicit
backend operation, never an automatic consequence of missing eligibility. No new API
or CLI invokes it. Evidence retains bounded hashes/checks, not raw model text or secrets.
The application's existing server-owned configuration/explicit execution authority
boundary remains in force; certificates never confer network or tool permission.

## Limits and parent observations

The fixtures are intentionally small static/in-memory qualification exercises, not a
claim of broad engineering competence. Mutations are independently byte-checked, never
executed as Python. Reviewer/Security negative and positive diffs never reach an executor.
Production mutations still use ToolExecutor and Finalizer. No ranking benchmarks, UI,
CLI, scheduler, promotion workflow or cloud integration are introduced here.

Production `read_file` and Coder/Repairer qualification share
`coding.tool_loop.format_authorized_read_result`: authorized reads return bounded
JSON `{content, expected_hash, truncated}` from ToolExecutor evidence (never a second
host-path read). Unauthorized reads still return a status-only summary with no
content or hash.


## Validation and review handoff

Observed branch: `role-certification-v1`. Approved parent:
`5df0f731572ef20a48b5572275c2125207490edf`. Reviewed feature HEAD before the
blocker-fix pass: `3670256`. This pass implementation commit:
`b848328d894b73c3017e5e07013166a4ed043c06` (narrow merge-blocker fix only:
final revalidation TaskState containment, Reviewer fail-closed without Repairer,
production/qualification `read_file` contract alignment).

Files:

- `src/code_slayer/workers/engineering_roles.py`: frozen engineering evidence policy,
  identity/evidence verification and freshness.
- `src/code_slayer/workers/production_eligibility.py`: engineering evidence gate.
- `src/code_slayer/coding/qualification.py`: safe per-role fixtures and explicit issuance.
- `src/code_slayer/coding/routing.py`: configured candidate selection, identity-bound
  transport, independent review and execution revalidation.
- `src/code_slayer/coding/pipeline.py`: certified production entry and distinct Repairer.
- `src/code_slayer/coding/tool_loop.py`: exact Coder/Repairer request role.
- `src/code_slayer/coding/testing.py`: explicit injected-worker test entry.
- `tests/unit/test_engineering_role_certification.py`: 83 new unit cases.
- `tests/integration/test_engineering_role_pipeline.py`: three production-path cases.
- `tests/unit/test_production_eligibility.py`: updated evidence-store/clock signature check.
- `tests/integration/test_coding_pipeline.py`: explicitly uses the test harness.
- `docs/ROLE_CERTIFICATION_V1.md`: architecture, permission review and handoff.

Final focused validation before this pass: **554 passed**, no failures or skips
(528 + 26). This blocker-fix pass: documented Role Certification selection
**573 passed**; post-rebase coding/role subset **212 passed** (190 baseline plus
new containment, Reviewer fail-closed, and `read_file` contract tests).
The first sandboxed broad run had 432 passes and 96 setup errors because temporary
localhost HTTP sockets were prohibited. The same 528-test selection passed with
socket access; no live model/server was contacted. The additional 26 tests cover
baseline production promotion and historical promotion-provenance migration.

Main selection (all run with `python3 -m pytest -q -p no:cacheprovider`,
`PYTHONDONTWRITEBYTECODE=1`, and the existing development-tool environment):

```text
tests/unit/test_engineering_role_certification.py
tests/integration/test_engineering_role_pipeline.py
tests/unit/test_role_qualification.py
tests/unit/test_production_eligibility.py
tests/unit/test_runtime_identity_separation.py
tests/unit/test_planner_certification.py
tests/unit/test_planner_qualification_evidence.py
tests/unit/test_security_evaluation_evidence.py
tests/unit/test_security_evaluation.py
tests/unit/test_security_baseline_certification.py
tests/unit/test_security_live_certification.py
tests/unit/test_live_planner_certification.py
tests/unit/test_certification_center.py
tests/unit/test_certification_center_planner.py
tests/unit/test_worker_lifecycle_certification.py
tests/unit/test_planner_routing.py
tests/unit/test_coding_tool_loop.py
tests/unit/test_coding_handoff.py
tests/unit/test_coding_pipeline_types.py
tests/integration/test_coding_pipeline.py
tests/integration/test_coding_workspace.py
tests/integration/test_coding_mutation_guard.py
```

Additional selection:
`tests/unit/test_production_promotion.py tests/unit/test_promotion_provenance_migration.py`.
Ruff on all affected Python files and `git diff --check`: PASS.
A full repository suite was not run. Live-model qualification/capability is UNVERIFIED;
only deterministic fixtures and temporary local HTTP servers were exercised.

No push, merge, deployment, service restart, live configuration/worker mutation or
production recertification was performed. Work is confined to this dedicated worktree;
no parent implementation bug was silently fixed. Permission/verification changes are
explicitly described above for review against the normative governance documents.
