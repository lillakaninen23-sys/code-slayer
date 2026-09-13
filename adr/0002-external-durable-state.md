# ADR 0002 — Durable state lives outside the target repository

## Status
Accepted (Foundation Plan v0.1, Revision 2.1, INV-9). Implemented in Phase 1.

## Context
A tool whose entire purpose includes guarding against destructive
repository operations (`git clean -fdx`, `rm -rf`, ...) must not store its
own audit trail and task state somewhere those exact operations can
destroy it.

## Decision
Code Slayer's SQLite database and content-addressed blobs for a given
`(repo_id, worktree_id)` live under an XDG data directory:

```
$XDG_DATA_HOME/codeslayer/repos/<repo_id>/worktrees/<worktree_id>/
├── state.db
├── blobs/
└── tmp/
```

(`~/.local/share/codeslayer/...` when `XDG_DATA_HOME` is unset), resolved
by `code_slayer.store.location`. Resolution order is: an explicit
`override` argument, then `$CODESLAYER_STATE_ROOT` (what every test in
this repository sets, so no test can ever resolve to a real user path),
then `$XDG_DATA_HOME`.

Nothing here lives inside the target repository's working tree, and
nothing is gitignored inside it either — it simply never exists there.

## Alternatives considered
- **A tracked or gitignored `.codeslayer/` directory inside the repo.**
  Simpler to discover, but a `git clean -fdx` (gitignored) or a plain
  `rm -rf` on the repo (either way) would also delete Code Slayer's own
  record of what it did — exactly the class of mistake the tool exists to
  guard against. Rejected.

## Consequences / known limitations
- Two different (repo_id, worktree_id) pairs never share a database, so
  there is currently no single query across a whole repository's
  worktrees — acceptable for Phase 1, revisit if cross-worktree awareness
  (Foundation Plan §21/§28) is built later.
- Content-store retention/garbage collection is not designed yet — see
  Foundation Plan §21. `content_blobs` (and the files under `blobs/`)
  accumulate indefinitely in Phase 1.

## Tested by
`tests/unit/test_location.py` (resolution order, no path ever falls
inside a repo root) and `tests/unit/test_identity.py` indirectly, via the
`(repo_id, worktree_id)` pair location depends on.
