# Isolated, disposable job worktrees (Phase 7.5c)

Phase 7.5c establishes the repository-isolation boundary
[`docs/ROADMAP.md`](ROADMAP.md)'s Local Worker Runtime stage calls
"Isolated mutating jobs" (step 4) — the foundation required before any
real worker may earn or exercise mutating trust
([`docs/CODE_SLAYER_VISION.md` §37](CODE_SLAYER_VISION.md#37-isolated-job-execution)).
It creates a Code-Slayer-owned, disposable Git linked worktree, pinned to
an explicit base revision, that a task then runs entirely inside using
the *unmodified* Phase 1–6 stack — no new identity system, no new policy,
no new lease mechanism, only the isolated place those already-real
components operate against.

## Critical safety boundary

**A Git linked worktree is a repository-state isolation boundary, not an
OS-level sandbox.** It reliably keeps one worktree's `HEAD`/index/working
tree separate from another's. It does **not** by itself prevent a process
running with the job worktree as its working directory from writing
`/tmp`, `$HOME`, `/etc`, or any other absolute host path a process could
otherwise reach.

This phase introduces no mechanism by which a worker receives arbitrary
shell, arbitrary subprocess, arbitrary host filesystem access, or
arbitrary network access. The *only* way mutation can occur inside a job
worktree — in this phase, or any later one built on it — is through Code
Slayer's existing closed capability set
(`tools.registry.CAPABILITIES`), authorized per-call by
`policy.engine.PolicyEngine` and enforced by `tools.executor.ToolExecutor`,
with `repo_root` simply pointing at the job worktree instead of the
primary one. Every existing confinement primitive
(`tools.file_tools.relative_path`/`parent_fd` — no absolute paths, no
traversal) is unmodified and unconditionally still applies.

OS-level containment (a dedicated unprivileged user, namespaces, seccomp,
read-only host mounts —
[`docs/CODE_SLAYER_VISION.md` §38](CODE_SLAYER_VISION.md#38-os-level-containment))
remains a stated **future** goal. This phase does not claim it exists.

## Lifecycle

`repo.job_worktree.create_job_worktree(primary_path, *, base_revision=None)`:

1. Resolves `primary_path`'s identity (`repo.identity.resolve`) and
   freezes `base_revision` (or the primary worktree's current `HEAD`, if
   none given) to a concrete commit sha *right now*
   (`job_worktree_git.resolve_commit`) — a moving branch tip after this
   call can never silently change what the job is based on.
2. Generates a Code-Slayer-owned path —
   `state_root()/job-worktrees/<repo_id>/<job_id>` (`job_id` a fresh
   `uuid4`) — outside the primary repository's own working tree. This
   path is **never** a caller-supplied parameter: no request, prompt, or
   model output can ever choose it.
3. Creates a real linked, **detached** worktree there
   (`git worktree add --detach <path> <sha>`) — never a branch, and never
   creates or moves any of the user's own branches.
4. Resolves the new worktree's own identity: it shares `repo_id` with the
   primary repository (the same `--local` git config value) and receives
   a fresh `worktree_id` (a new `codeslayer-id` file under its own
   private `.git/worktrees/<name>/`) — the *same* identity system every
   other worktree already uses, not a new one.
5. Writes a small JSON sidecar (`job_worktree.json`) into the job's own
   state directory recording `repo_id`/`worktree_id`/`path`/
   `primary_repo_root`/`base_revision`/`created_at`, then opens and
   migrates that worktree's own `state.db` and records a
   `JOB_WORKTREE_CREATED` audit event (`task_id=None` — a job worktree
   may exist before any task is created against it).

Because `store.location`'s durable-state paths are keyed by `(repo_id,
worktree_id)` (`adr/0002-external-durable-state.md`), the job worktree
receives its own `state.db`/`blobs/`/`tmp/` the moment its identity is
resolved — no separate mechanism was needed to keep job state from
leaking into, or reading, the primary worktree's own state.

A task created against the job worktree (`repo_root=str(handle.path)`,
`repo_id=handle.repo_id`, `worktree_id=handle.worktree_id`) then runs the
*exact* Phase 1–6 flow — baseline, state machine, lease/fencing,
`ToolExecutor`, `PolicyEngine`, `CheckpointManager` — entirely unmodified,
simply rooted at the job worktree instead of any other repository.

## No schema migration

A job worktree's association with its task_id, source repo identity, and
worktree_id is recoverable entirely from existing, already-durable state
once a task exists: `tasks.repo_root`/`repo_id`/`worktree_id` (Phase 1),
`worker_leases` (Phase 6), and `checkpoints` (Phase 5). The one fact none
of those already carry — the frozen base commit a job worktree itself
was created from — lives in the JSON sidecar described above (step 5),
keyed by exactly the `(repo_id, worktree_id)` pair `store.location`
already uses for every other piece of that worktree's durable state. No
new table was needed, so none was added; migrations 0001–0004 are
unmodified.

The one genuine gap that pre-dates any task existing — the narrow window
between `git worktree add` succeeding and a task's own durable state
existing — is closed by the JSON sidecar (step 5 above), plus the fact
that Git's own worktree registry (`.git/worktrees/`, `git worktree list`)
is already independent, durable ground truth that a linked worktree
exists, regardless of what Code Slayer's own bookkeeping managed to
record. If even the sidecar/database setup fails after `git worktree add`
succeeds, `create_job_worktree()` raises
`JobWorktreeSetupIncompleteError` and deliberately does **not** remove
the worktree — recoverable, never silently discarded.

## Cleanup: conservative, refusal-first

`repo.job_worktree.release_job_worktree(handle)` never deletes a job
worktree unless durable evidence positively confirms it is safe:

- no active or quiescing lease for its `worktree_id`,
- no unresolved (`STARTED`/`UNKNOWN`) tool operation,
- no non-terminal task currently using it, and
- no task-owned path that was never covered by at least one durable
  checkpoint.

Any of those — or simply being unable to open the job's own database at
all (setup never completed) — is a refusal
(`CleanupResult(ok=False, reason=...)`), never a best-effort deletion.
Even a successful cleanup only removes the disposable Git working tree
itself; the job's own `state.db`/`blobs/` audit trail is deliberately
left in place as a historical record.

## No promotion into the primary worktree

Nothing in this phase fast-forwards, merges, cherry-picks, or updates any
branch in the primary worktree. A job worktree's checkpoints live under
`refs/codeslayer/checkpoints/<task_id>/<seq>` exactly as
`repo.checkpoint.CheckpointManager` already creates them for any other
worktree — visible from the primary worktree too, since Git's object/ref
storage is shared by every linked worktree of one repository, but never
applied to the primary worktree's own `HEAD`, index, or working tree.
Promotion into a user's real branch is explicitly out of scope for this
phase and belongs after stronger finalization/review infrastructure
exists (`docs/ROADMAP.md`).

## Tests

`tests/integration/test_job_worktree.py` exercises: creation under a
Code-Slayer-owned path with no path parameter a caller could ever supply;
an explicit, frozen base revision (including that a later commit on the
primary is invisible from the pinned job worktree, and that nothing is
created/moved on the primary's own branches); shared `repo_id`/distinct
`worktree_id` identity and the state (db/blobs/tmp) separation that falls
out of it; a real `ToolExecutor` mutation confined to the job worktree
and provably absent from the primary; the primary worktree's `HEAD`,
branch, index, tracked contents, and a pre-existing untracked file all
byte-for-byte unchanged after that mutation; a real `CheckpointManager`
checkpoint built from that mutation, and that creating it does not alter
the primary's `HEAD`/index/working tree either; existing lease-fencing,
policy-scope, and absolute/traversal path-confinement behavior all
holding unmodified inside a job worktree; distinct identity across two
job worktrees created from the same primary repository; cleanup refusing
an active lease, a non-terminal task, an unresolved operation, and
uncheckpointed owned content, then succeeding once none of those apply;
and that a simulated setup failure after `git worktree add` leaves the
worktree recoverable (visible in Git's own `git worktree list`) rather
than silently discarded.
