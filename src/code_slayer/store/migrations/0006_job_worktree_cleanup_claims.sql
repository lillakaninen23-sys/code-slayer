-- Code Slayer schema v6 (Phase 7.7b — job-worktree cleanup integrity
-- hardening).
--
-- Purely additive: no ALTER of any table; migrations 0001-0005 are
-- byte-for-byte unchanged.
--
-- `release_job_worktree()` cannot hold one SQLite transaction open across
-- its own `git worktree remove` subprocess call (an external, possibly
-- slow operation neither SQLite's own locking nor this project's
-- transaction discipline is meant to span), so the "safe to remove"
-- decision and the actual removal cannot be a single atomic step.
-- Instead, this table gives that decision a durable, checkable claim: a
-- row present for `worktree_id` means "cleanup has proven this worktree
-- safe to remove and is acting on it" -- durably recorded in one
-- committed transaction *before* Git is ever invoked. `store.task_repo.
-- TaskRepo.create()` and `lease.manager.LeaseManager` (fresh-epoch grant
-- only; a straightforward renew/release of an already-held lease raises
-- no new ownership) both refuse to establish new ownership for a
-- `worktree_id` a row here names, closing the check/delete race a lone
-- in-process re-check could not: the two paths that could otherwise race
-- past a completed cleanup decision are the same durable database this
-- claim itself lives in, so they see it under the same write-lock
-- discipline `store.db.transaction()` already provides everywhere else
-- in this codebase, with no new filesystem lock invented.
--
-- `status` distinguishes "validated, about to attempt Git removal"
-- (CLAIMED) from "a `git worktree remove` attempt is or was in flight"
-- (REMOVING) -- the latter is what lets a retried `release_job_worktree()`
-- call, after a crash mid-removal, tell "Git actually already removed
-- this before we crashed" (the path is simply gone from `git worktree
-- list` now) apart from "Git genuinely never got there" (still listed;
-- safe to attempt again), without ever guessing.
--
-- A row here is deleted only once cleanup either finishes (the worktree
-- is confirmed removed) or is aborted (a fresh safety check, run after
-- the claim was recorded, found a reason to refuse after all) -- never
-- left to represent anything but "cleanup is currently authoritative for
-- this worktree, or was, and has not yet been resolved".

BEGIN;

CREATE TABLE job_worktree_cleanup_claims (
  worktree_id  TEXT PRIMARY KEY,
  claim_token  TEXT NOT NULL,
  claimed_at   TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('CLAIMED', 'REMOVING'))
);
