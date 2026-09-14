# Durable Git checkpoints and recovery (Phase 5)

Phase 5 gives a task a trustworthy, auditable boundary between mutable
working state and durable, accepted Code Slayer work: a Git commit that
represents exactly the content a task legitimately owns (Phase 4), never
more, created only after revalidating that ownership against the task's
immutable baseline (Phase 3) and current repository reality.

## What a checkpoint is

A checkpoint is one immutable Git commit, built from every path in
`task_owned_paths` the task currently owns (non-deleted, backed by a
`SUCCEEDED` Phase 4 `tool_operations` row), overlaid onto the tree of the
task's **original baseline HEAD** — never the user's live index, and
never a path Code Slayer does not durably record owning. It is bound to a
dedicated ref, `refs/codeslayer/checkpoints/<task_id>/<seq>`, that Code
Slayer alone ever writes, once, and never moves again.

Only `READY_FOR_CHECKPOINT` may request one; success transitions the task
to `CHECKPOINTED` (`docs/STATE_MACHINE.md`'s existing edge), atomically
with the durable checkpoint record. Creating a checkpoint never implies
`COMPLETED` — that remains an explicit, later, separate decision — and
`CHECKPOINTED -> IMPLEMENTING` remains available for further owned work
and a subsequent checkpoint, chained onto the first.

## Trust boundary and ownership

A checkpoint may include a path if and only if:

- it is durably recorded as owned by this task (`task_owned_paths`, not
  merely named in a request), and
- the row proving that ownership is `SUCCEEDED`, bound to this exact
  task/resource, with a recorded content hash, and
- that content hash still matches the path's **current** bytes, read
  through the same descriptor-relative, symlink/hardlink-resistant
  primitives Phase 4 uses (`tools.file_tools`) — not a cached belief.

Nothing else is ever added to the tree. A pre-existing dirty/untracked
file, a staged change, a rename, or any other file the user is working on
concurrently is invisible to checkpoint construction — not "protected" in
the Phase 3 sense (this is not a mutation), simply never a candidate for
inclusion, because it was never recorded as owned.

## Baseline revalidation

Before building a tree, `CheckpointManager._facts()` re-derives:

- **identity**: the task's `(repo_id, worktree_id)` still matches live
  Git identity;
- **baseline drift**: live `HEAD` and branch still equal the task's
  *original* baseline (`repo_baselines.head_sha`/`branch`, Phase 3) — a
  new *unrelated* untracked file appearing since baseline is normal and
  never blocks a checkpoint (it is simply excluded, not "drift"); only
  `HEAD`/branch moving, or the two durable copies of the original
  protected-path set disagreeing with each other, count as drift here;
- **ownership validity**: every currently-owned path's live content hash
  still matches its recorded evidence — an external edit, deletion, or a
  symlink swapped in for the real file, all deny the checkpoint outright
  (`owned_content_changed`) rather than silently checkpointing stale or
  substituted bytes.

Any of these failing denies the checkpoint (`baseline_drift_detected`,
`owned_content_changed`, `identity_mismatch`) — never a silent, partial,
or best-effort checkpoint.

## Git/index strategy

Tree construction never touches the repository's real `.git/index`,
`HEAD`, or the user's branch. `repo.checkpoint_git` redirects every
index-shaped plumbing call (`read-tree`, `update-index`, `write-tree`) to
a private, temporary index file via `GIT_INDEX_FILE`, created fresh per
attempt and removed afterward. `commit-tree` and `update-ref` do not
consult the index or `HEAD` at all. No `git add`, `git commit`,
`git reset`, `git stash`, or `git clean` is ever used. `update-ref <ref>
<sha> ''` is Git's own compare-and-swap — a checkpoint's ref can be
created exactly once and never force-moved.

The first checkpoint's commit parents the task's original baseline HEAD
(or is parentless, for an unborn-HEAD baseline); each later checkpoint's
commit parents the *previous checkpoint's own commit* — a real,
inspectable history under Code Slayer's private ref namespace — while
every checkpoint's tree is always the full baseline tree plus every
currently-owned path (not an incremental diff), so each checkpoint is a
complete, self-consistent snapshot on its own.

## Checkpoint identity

`checkpoints` (schema v1, previously schema-only) gains one durably
COMPLETE row per checkpoint: `checkpoint_id`, `task_id`, `seq`,
`parent_checkpoint`, `git_ref`, `git_branch`, and a structured
`verified_json` — `commit_sha`, `tree_sha`, `parent_commit_sha`,
`base_head_sha`, the checkpoint's canonical `request_hash`
(`compute_request_hash`, Phase 1), and the exact `(path, content_hash)`
manifest included. `changed_files_json` lists the same paths. No commit
message is ever parsed back for identity; identity is always read from
this durable row and cross-checked against the Git object itself.
`completed_json`/`pending_json`/`worker_id`/`next_action` are agent-loop
fields with no Phase 5 producer, left at their schema defaults.

## Policy

Checkpoint creation is its own narrow, closed capability
(`checkpoint_create`, `RiskClass.GIT_MUTATION`, registered in
`tools.registry.CAPABILITIES`) decided by its own pure function,
`policy.engine.evaluate_checkpoint()` — not the path-shaped
`PolicyEngine.evaluate()` Phase 4 file/command tools use, which has no
sensible meaning for "the whole set of paths a task owns." It never
returns `REQUIRE_APPROVAL`: a checkpoint is either safe to create right
now or it is not. Malformed facts, an unknown/wrong task state, an
unresolved worktree-wide operation, a baseline/ownership drift, or a
(structurally-should-never-happen) protected-path conflict all deny.

## Operation journal, crash, and recovery semantics

Checkpoint creation is journaled exactly like a Phase 4 tool call: a
`STARTED` `tool_operations` row (`tool_name="checkpoint_create"`, its
`target_resource` the checkpoint's dedicated ref) is committed *before*
any Git object or ref is created. The durable `checkpoints` row, the
journal's `SUCCEEDED` resolution, and the `READY_FOR_CHECKPOINT ->
CHECKPOINTED` transition are then written together, atomically, only once
the commit is known to exist.

Recovery is narrow and evidence-based, not a general reconciler:

- **Before any Git work**: nothing external happened; a crash leaves
  `STARTED` with no ref. Resolving it checks exactly one fact — does the
  ref exist? — and since Code Slayer is the sole, exclusive writer of
  `refs/codeslayer/checkpoints/*`, absence is unambiguous proof nothing
  durable happened (`FAILED`, safely retried at the same `seq`).
- **After the ref is created but before the durable bookkeeping commits**:
  the same ref-existence check now finds a real commit; its tree/parent
  are read back from the object itself (`git cat-file -p`), never
  re-derived from anything Code Slayer merely *intended* to write, and
  the pending journal entry is resolved to `SUCCEEDED` and finalized.
- **A caught Python exception during Git plumbing is always a
  deterministic failure** — the failing subprocess already ran to
  completion with a known, non-zero result (or a bounded timeout that
  Python itself already killed the child for) — never `UNKNOWN`.
  `UNKNOWN` is reserved for the one case recovery genuinely cannot
  resolve: the ref-existence check itself fails to run (e.g. `git` is
  unavailable). That case is left exactly as found, retried later, and
  never guessed at.

An unresolved `checkpoint_create` operation blocks every further mutation
in the worktree (Phase 4's existing worktree-wide invariant, reused
unchanged) until explicitly reconciled — `CheckpointManager.reconcile()`
is available standalone for that, and `create()` always attempts it first
before considering a new checkpoint.

## Audit and evidence

`TOOL_REQUESTED`, `POLICY_EVALUATED`, `POLICY_DENIED`, `OPERATION_STARTED`,
`OPERATION_FINISHED`, `CHECKPOINT_VALIDATED`, `CHECKPOINT_CREATED`,
`RECONCILIATION_STARTED`, `RECONCILIATION_FINDING`, and
`EXTERNAL_MODIFICATION_DETECTED` — all part of Phase 1's original audit
vocabulary, unused until now — carry structured facts (operation/
checkpoint id, decision, reason, resource, shas, counts). No file
content, patch text, or raw command output is ever recorded; content
identity is always a hash or a Git object id. Checkpoints do not use
`ContentStore`: the evidence *is* the Git object database itself,
referenced by sha from `verified_json`, not duplicated into Code Slayer's
own blob store.

## Tests

`tests/unit/test_checkpoint_git.py` exercises the plumbing directly
(empty-tree/root-commit handling, tree overlay and removal, real-index
non-interference, ref create-only semantics, control-character injection
into `--index-info`). `tests/unit/test_checkpoint_policy.py` exercises
`evaluate_checkpoint()`'s full decision table. `tests/integration/
test_checkpoint_manager.py` exercises the full manager against real
temporary Git repositories: normal and chained checkpoints, dirty-
repository preservation (unrelated untracked/staged/renamed content
byte-for-byte unchanged), HEAD/ownership drift denial, policy/state
gating, and real subprocess crashes at each of the three durability
boundaries described above, including full restart-and-recover
verification against a reopened database and repository.

## Explicitly deferred

No lease/fencing, no checkpoint-driven resume of in-progress work beyond
the `CHECKPOINTED -> IMPLEMENTING` edge the state machine already defines,
no scheduler/orchestrator/agent loop, no model or provider integration, no
network access, no WebUI, no environments/knowledge/training. Automatic
reconciliation is intentionally narrow: it resolves only this phase's own
`checkpoint_create` operation class via ref-existence evidence, never a
general "reconcile any stuck operation" mechanism. Schema v1 is unchanged;
`checkpoints` was already present, schema-only, since Phase 1.

> **Phase 6 update:** `CheckpointManager` now accepts an optional
> `lease: LeaseHandle` — required (and checked before policy) for
> `create()` to start a *new* attempt; not required for `reconcile()`,
> which remains the explicit, evidence-based recovery path described
> above, unchanged. `create()` also revalidates fencing a second time,
> deeper: immediately before `create_ref` — the last moment before a
> checkpoint becomes externally visible — refusing to publish a built
> (but still unreferenced, so harmless) commit under authority that may
> have been superseded while tree/commit construction ran. See
> [`docs/LEASES_AND_RECOVERY.md`](LEASES_AND_RECOVERY.md) for the full
> fencing/quiescence model and why checkpoint finalization itself
> (`_finalize()`) is still deliberately not re-gated by lease state.
