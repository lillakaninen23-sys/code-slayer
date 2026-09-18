# Code Slayer (CSLR)

Code Slayer (short name/brand: **CSLR**) is a local, persistent,
repo-level software-engineering agent system. It is not a chat wrapper
around a model: models are replaceable *workers*; Code Slayer's own
durable state — task state, audit history, checkpoints, worker leases —
is the system's source of truth.

The full design is recorded in **Code Slayer v0.1 Foundation Plan,
Revision 2.1** (owner-approved). This repository implements it
phase by phase; see `adr/` for the design decisions Phase 1 depends on.

- [Verification Standard](docs/VERIFICATION_STANDARD.md) — normative no-inherent-trust rule: CLAIM → VERIFY → EVIDENCE → ACTION.
- [Vision](docs/CODE_SLAYER_VISION.md) — long-term product/engineering direction.
- [Security & Privacy Architecture](docs/SECURITY_PRIVACY_ARCHITECTURE.md) — normative security/privacy invariants.
- [Permissions Model](docs/PERMISSIONS_MODEL.md) — the scoped permission vocabulary those invariants are expressed through.
- [Product Principles](docs/PRODUCT_PRINCIPLES.md) — the product/UX commitments every surface follows.

Existing package/service names (`codeslayer`, `code_slayer`) are unchanged
by the CSLR brand — see [`AGENTS.md`](AGENTS.md) for the rules every future
change to this project must follow.

## Local service

From this checkout, first-time install:

```bash
./cslr install-service
```

systemd --user starts CSLR on boot and binds `http://127.0.0.1:8765`.
The current Engineering Control Room preserves the existing Projects, Tasks,
Models, Intelligence, Planning, Privacy & Security, Audit, and Settings views.
Runtime configuration lives under Models; Certification Center lives under
Privacy & Security; System/deployment and Tailscale Serve live under Settings.
The Dashboard adds read-only summaries from the same backend GET projections
without live-attesting runtime, starting certification, or mutating admin state.

Ordinary administration (Ollama origins, runtime identity, Certification
Center, Tailscale Serve, updates) is in the WebUI. The terminal is for
install and emergency `./cslr status|start|stop|restart` only. Persistent
config is `~/.config/codeslayer/config.toml`, not this repository.
Remote access is optional Tailscale Serve of localhost — never a
`0.0.0.0` bind. See [`docs/WEBUI_API.md`](docs/WEBUI_API.md) and
[`docs/OPERATIONS_UX.md`](docs/OPERATIONS_UX.md).

## Status: Phase 7 / Local Worker Runtime — VERIFIED / ACCEPTED

Phase 1 implements *only* the foundation a later agent loop will stand on:

- project scaffolding
- external durable state (outside the target repo's working tree)
- repository/worktree identity (Git-metadata based, not an untracked file)
- SQLite schema v1 and a migration runner
- task persistence primitives
- append-only, hash-chained audit
- a content-addressed evidence/blob store
- a durable tool-operation journal (structure + repository API)

Phase 2 adds the typed task-state graph, guarded transitions, durable
resume/reconciliation origins, and atomic state/audit persistence. See
[`docs/STATE_MACHINE.md`](docs/STATE_MACHINE.md) for the exact API,
transition, concurrency, and recovery contracts.

Phase 3 adds read-only repository inspection, pre-existing path protection,
scoped document discovery and immutable baseline evidence, integrated with
the state machine. See [`docs/REPOSITORY_BASELINES.md`](docs/REPOSITORY_BASELINES.md)
for capture, scope, evidence and target-preservation contracts.

Phase 4 adds a closed capability registry (`read_file`, `create_file`,
`write_file`, `apply_patch`, one read-only `run_command` Git profile), a
pure policy engine deciding ALLOW/DENY/REQUIRE_APPROVAL over explicit
scope/baseline/ownership facts, and a controlled executor that journals
every effect before it runs and records ownership, evidence, and audit
atomically with its result. See
[`docs/TOOLS_AND_POLICY.md`](docs/TOOLS_AND_POLICY.md) for the capability,
policy, journal, crash, and evidence contracts.

Phase 5 adds durable Git checkpoints: a task's `READY_FOR_CHECKPOINT`
state can produce one immutable commit representing exactly the content
it owns (Phase 4), revalidated against its baseline (Phase 3) and current
repository reality, under a dedicated ref namespace the user's branch,
HEAD, and index are never touched by — journaled and crash-recoverable
across the Git/SQLite durability boundary. See
[`docs/CHECKPOINTS.md`](docs/CHECKPOINTS.md) for the trust boundary,
ownership, Git/index strategy, and crash/recovery contracts.

Phase 6 adds durable worktree leases and fencing: one current ownership
epoch per worktree, a monotonic fencing token every Phase 4/5 mutation
path now checks (both before policy and immediately before its actual
effect), and a generic recovery framework that discovers unresolved
operations and dispatches only to the reconcilers that already exist,
never guessing an outcome from a stale epoch or a vanished process. See
[`docs/LEASES_AND_RECOVERY.md`](docs/LEASES_AND_RECOVERY.md) for the
lease/fencing model, expiry-vs-fencing distinction, and known limitations.

Phase 7 adds the persistent LocalWorkerRunner, a real local model adapter, exact
conformance-backed read-only trust, QuestionGate and human resolutions, isolated
job worktrees, and explicit cloud transport authorization.

WebUI Foundation 1 adds `codeslayer serve`: a loopback HTTP/application API that
serves the existing sidecar and delegates actions to LocalWorkerRunner. See
[`docs/WEBUI_API.md`](docs/WEBUI_API.md) for startup, runtime wiring, JSON contract
and safety boundaries. No Phase-8 intelligence, AUTO, mutation trust, or worktree
promotion is enabled.

## Development

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

pytest -q
ruff check .
mypy src
```

All state used by tests lives under a temporary directory — nothing in
this repository's own test suite ever touches a real user's
`~/.local/share/codeslayer`, and no test ever runs against a real,
non-temporary Git repository.

## Package layout

```
src/code_slayer/
├── core/      # task states, transition graph/guards, durable state-machine API
├── store/     # SQLite schema, migrations, connection/transaction handling,
│              #   task persistence, content-addressed blobs, the operation
│              #   journal, durable checkpoint records
├── audit/     # append-only audit event log, canonical hashing, chain verification
├── repo/      # safe Git reads, identity, inspection, rule discovery, baseline
│              #   service, checkpoint Git plumbing and manager
├── tools/     # closed capability registry, file/command primitives, controlled executor
├── policy/    # pure ALLOW/DENY/REQUIRE_APPROVAL decisions over explicit facts
├── lease/     # durable worktree lease/fencing manager, generic recovery
└── cli/       # minimal CLI wiring (`codeslayer inspect`)
```
