# Code Slayer

Code Slayer is a local, persistent, repo-level software-engineering agent
system. It is not a chat wrapper around a model: models are replaceable
*workers*; Code Slayer's own durable state — task state, audit history,
checkpoints, worker leases — is the system's source of truth.

The full design is recorded in **Code Slayer v0.1 Foundation Plan,
Revision 2.1** (owner-approved). This repository implements it
phase by phase; see `adr/` for the design decisions Phase 1 depends on.

## Status: Phase 1 — durable foundation

Phase 1 implements *only* the foundation a later agent loop will stand on:

- project scaffolding
- external durable state (outside the target repo's working tree)
- repository/worktree identity (Git-metadata based, not an untracked file)
- SQLite schema v1 and a migration runner
- task persistence primitives
- append-only, hash-chained audit
- a content-addressed evidence/blob store
- a durable tool-operation journal (structure + repository API)

There is **no** agent loop, model integration, checkpoint Git-plumbing,
lease manager, scheduler, or daemon yet. See the ADRs and the Foundation
Plan for what comes after Phase 1.

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
├── store/     # SQLite schema, migrations, connection/transaction handling,
│              #   task persistence, content-addressed blobs, the operation journal
├── audit/     # append-only audit event log, canonical hashing, chain verification
├── repo/      # thin `git` subprocess wrapper + repo/worktree identity
└── cli/       # minimal CLI wiring (`codeslayer inspect`)
```
