# ADR 0006 — Local model workers are the normal production architecture

## Status
Accepted (Foundation Plan v0.1, Revision 2.1, § Δ item 9). **Not
implemented in Phase 1** — this ADR documents a decision Phase 1's schema
and module boundaries are already shaped to not contradict, not a
delivered feature.

## Context
Code Slayer's worker abstraction must stay provider-neutral (INV-10): no
module outside a future `workers/*_adapter.py` may name a specific
provider. Separately, the *product* decision — which worker(s) actually
run by default — needed to be recorded so later phases don't
accidentally re-introduce a cloud-provider dependency by default.

## Decision
Normal production runtime is **Code Slayer → local model workers**, with
zero mandatory cloud dependency. The intended default deployment is a
local model (e.g. Qwen) served behind a generic local
OpenAI-compatible-endpoint adapter, covering every worker role: planner,
coder, reviewer, repair, and deep reviewer.

Claude and Astra are **optional cloud maintenance/escalation adapters**,
reachable only when a `cloud_escalation` policy explicitly allows it
(`disabled` by default), and only through a bounded, logged,
exportability-filtered handoff package — never given raw repository
access. See the Foundation Plan §11/§17 for the full escalation design.

## Consequences for Phase 1
- No worker, adapter, tool, or orchestrator code exists yet — this is
  intentionally out of Phase 1's scope.
- The schema already carries the columns this decision needs later
  without a migration: `content_blobs.exportable` (default `0`,
  Phase 1) and `workers.network_class` (`local`/`cloud`, table created as
  schema only in Phase 1's migration, unpopulated).
- Nothing in Phase 1's code names "Qwen," "Claude," or "Astra" anywhere
  outside this document and the Foundation Plan — consistent with INV-10,
  and there is nothing yet to name, since no adapter exists.

## Tested by
Not applicable in Phase 1 — there is no adapter behavior to test yet.
`tests/unit/test_db.py`'s schema-shape assertions (via `known_schema_version`
and direct table introspection in other tests) exercise the `workers` and
`content_blobs` tables' existence and columns, which is what this decision
depends on being present later.
