# ADR 0003 — Repository/worktree identity via Git metadata, not a working-tree file

## Status
Accepted (Foundation Plan v0.1, Revision 2.1). Implemented in Phase 1.

## Context
An earlier revision of the Foundation Plan (Revision 1) proposed an
untracked `.codeslayer-id` marker file in the working tree as repo
identity. Owner review correctly rejected this: `git clean -fdx` operates
on the working tree and would delete exactly the file identity depends
on — destroying the link to durable state via the very operation that
state is supposed to survive.

## Decision
Two identities, both resolved by `code_slayer.repo.identity`, purely from
Git's own metadata:

- **`repo_id`** — a UUID written once via
  `git config --local codeslayer.repo-id <uuid>`. `--local` is passed
  explicitly (not the default scope) so it always lands in the shared
  repository config, even on a repository with `extensions.worktreeConfig`
  enabled. Visible from every linked worktree of that repository.
- **`worktree_id`** — a UUID written to a file at
  `$(git rev-parse --git-dir)/codeslayer-id`. For the main worktree this
  resolves inside `.git/`; for a linked worktree it resolves inside that
  worktree's private `.git/worktrees/<name>/` directory — exactly where
  Git itself keeps per-worktree private state (`HEAD`, `index`,
  `logs/HEAD`), so this follows Git's own convention rather than
  inventing a new one.

Both survive `git clean -fdx` and `git reset --hard`, since neither
touches anything under `.git/`.

## Alternatives considered
- **The untracked working-tree marker file (Revision 1).** Rejected for
  the reason above.
- **A path-based identity (canonicalized working-tree path).** Rejected:
  breaks on any `mv`, which content-based Git metadata survives for free.

## Consequences / known limitations
- `git clone` does not copy arbitrary custom config keys, so a clone has
  no `repo_id` of its own — it is correctly treated as an unrelated,
  unknown repository (`identity.resolve(create=False)` raises
  `UnknownIdentityError`) rather than silently inheriting the source
  machine's durable state identity. Establishing a *deliberate* link
  between a clone and prior state (an explicit `codeslayer link` command)
  is not built in Phase 1.
- `rm -rf .git` destroys the repository itself, which is outside what any
  identity scheme can protect against; the external state directory is
  merely orphaned, not corrupted.
- Not yet verified against multiple real Git versions: whether
  `git config --local` always targets the shared config under
  `extensions.worktreeConfig` is believed true from Git's documented
  scope semantics, but should be checked empirically before this is
  relied on in a mixed-Git-version fleet (Foundation Plan §21).

## Tested by
`tests/unit/test_identity.py`: same worktree resolves to the same
identity; a linked worktree (`git worktree add`) shares `repo_id` but gets
a distinct `worktree_id`; `git clean -fdx` + `git reset --hard` do not
destroy identity; a `git clone` does not inherit the source's identity.
