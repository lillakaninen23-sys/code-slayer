# Controlled tools and policy (Phase 4)

Phase 4 gives a task the ability to have a narrow, explicit set of
capabilities executed on its behalf, only after an explicit policy
decision, with the effect journaled before it happens and the result
recorded atomically with ownership and audit. It introduces no model,
provider, worker, lease, checkpoint, scheduler, orchestrator, or agent
loop — see [`docs/ROADMAP.md`](ROADMAP.md) for what remains deferred.

## Capability registry

`tools.registry.CAPABILITIES` is a closed, code-defined mapping — never
extended by configuration, a request, or a discovered executable name:

| Capability | Risk | Mutation |
| --- | --- | --- |
| `read_file` | `READ_ONLY` | no |
| `create_file` | `WRITE_OWNED` | yes |
| `write_file` | `WRITE_OWNED` | yes |
| `apply_patch` | `WRITE_OWNED` | yes |
| `run_command` | `GIT_READ` | no |

`run_command` has exactly one profile, `git_rev_parse`: a single,
argv-only revision lookup (`git rev-parse --verify --end-of-options
<revision>`) run against a trusted Git binary resolved once from
`os.defpath`, never the caller's `PATH`. There is no generic shell
capability, no arbitrary command runner, and no capability that reduces to
one: `pytest`/`python`/build-tool profiles are deliberately not
implemented, because an allowlisted executable can still run
repository-controlled code, reach the network, or spawn descendants —
properties this phase's argv/env sanitization alone cannot bound. Adding
such a profile safely needs the sandbox/environment isolation the
[Environments](ROADMAP.md#environments) stage introduces, not this one.

## Policy

`policy.engine.PolicyEngine.evaluate()` is a pure function from an
explicit `PolicyInput` to `ALLOW` / `DENY` / `REQUIRE_APPROVAL`. It never
touches the filesystem, the database, or a subprocess itself — every fact
it decides over (state, scope, protection, ownership, risk, an unresolved
operation) is computed and validated by `ToolExecutor._facts()` first.
Malformed, unknown, or ambiguous input denies:

- a non-`PolicyInput`, a non-enum `state`/`risk`, a non-`bool` flag, or an
  empty/non-`str` identity field all deny `malformed_policy_input`;
- an unrecognized `tool` denies `unknown_tool`;
- `network=True` or a `NETWORK` risk always denies, unconditionally.

For a mutating capability: `protected` (a baseline-recorded pre-existing
path) and `unresolved` (a STARTED/UNKNOWN operation anywhere in the
worktree) both deny outright, checked before ownership or risk. `
create_file` against a path that already exists or that this task already
owns is **always** `DENY "not_a_new_path"` — never `REQUIRE_APPROVAL` —
because no future approver could make `O_CREAT|O_EXCL` succeed there; this
is checked ahead of risk classification specifically so an upstream
`WRITE_EXISTING` classification can never turn it into an approval. A
`write_file`/`apply_patch` against a path this task does not already own
is `REQUIRE_APPROVAL`: Phase 4 implements no approval handler, so this
decision never executes anything, matching the existing-non-owned-path
contract without inventing an execution path for it.

## Scope, baseline, and ownership facts

`ToolExecutor._facts()` derives every fact the policy engine sees:

- **Scope** comes only from `task.config_json["tool_policy"]["scope"]`, a
  required, non-empty list of relative, `relative_path()`-validated
  directories (or `"."`). A JSON document whose top level is not an
  object — a list, string, number, or bare literal — denies rather than
  raising an `AttributeError` past `execute()`.
- **Baseline identity** re-reads the task's immutable baseline manifest
  (Phase 3) and rejects a repo/worktree identity mismatch, a resolved
  storage location inside the target or its Git directories, a changed
  repo root, or a protected-path set that disagrees with the durably
  recorded one.
- **Gitlink/nested-repository/mount boundaries**: a resource inside a
  gitlink's path is denied; `file_tools.parent_fd()` opens every ancestor
  directory relative to the previous directory's own file descriptor with
  `O_NOFOLLOW`, rejecting a symlinked or since-replaced ancestor, refusing
  to cross a device boundary, and refusing a directory that itself
  contains a nested `.git`.
- **Existence and ownership**: `exists` is a live `O_NOFOLLOW` stat, not a
  replay of baseline metadata, so a path created by a third party *after*
  baseline is still caught as `pre_existing` for `create_file`. An
  ownership row (`task_owned_paths`) is trusted only when its linked
  `tool_operations` row is `SUCCEEDED`, bound to this exact task and
  resource, and carries a non-empty `after_evidence` hash — an ownership
  row an operation failed to complete, or that points at the wrong
  resource, is `invalid_ownership_evidence`, denied, never treated as
  proof.
- **Hardlinks**: a mutation target with `st_nlink != 1` is denied before
  any journal entry exists, both at this pre-check and again, atomically,
  at the moment of open inside `_file_effect()`.

Protection guards against Code Slayer overwriting pre-existing user
content; it does not restrict reads. A `read_file` against a protected or
externally-created path is allowed as long as it is in scope and the task
is past `INSPECTING` — reading does not create ownership and cannot
overwrite anything.

## Journal, crash, and reconciliation semantics

Every `ALLOW`ed call inserts a `STARTED` `tool_operations` row in the same
transaction as the policy decision's audit events, committed *before* any
subprocess or filesystem mutation runs (ADR 0005). `CommandRunner`
re-verifies live identity (repo root, Git dir, `codeslayer.repo-id`,
`codeslayer-id`, and — for a mutation — that HEAD has not moved since
baseline and that the resource is not tracked in the index) immediately
before the effect, closing the gap between the first policy read and the
actual mutation.

A crash or exception is classified, never guessed as a success:

- **Before any mutation observably began** (`effect["mutated"]` still
  `False` and, for `run_command`, its own subprocess capture already
  resolved to a definite `CommandOutput`): the operation finishes
  `FAILED` with the triggering reason.
- **After a mutation began but before its result could be verified and
  durably recorded** (a `create_file`'s `open()` succeeded, a write
  actually happened, or a `run_command`'s child was spawned but its
  capture was interrupted): the operation finishes `UNKNOWN`, never
  `FAILED` — a real state distinct from failure, exactly because Code
  Slayer cannot prove the side effect did not happen.
- **A process crash between the STARTED commit and any later write**
  leaves the row `STARTED` durably, with no partial ownership or result;
  see `test_process_crash_after_started_before_mutation_leaves_unresolved_journal`.

Any `STARTED`/`UNKNOWN` operation anywhere in the worktree blocks every
further mutation (`reconciliation_required`) until it is explicitly
resolved. Phase 4 performs no automatic retry or reconciliation of an
unresolved operation — SQLite serializes managed mutations through short
writer transactions, which is not a lease or fencing implementation, and
external concurrent edits are only bounded by the live identity/hash
re-checks described above, not eliminated.

## Content-addressed evidence

`ContentStore.put()`'s existing immutability and deduplication semantics
(Phase 1/3) are unchanged. `ToolExecutor._finish()` stores real command
output (`run_command`'s stdout/stderr) as `source_kind="command_output"`,
non-exportable, bounded by the request's `output_limit`. A `read_file`'s
bytes are never persisted as a blob under this or any label — its content
hash is already durably recorded as the operation's `after_evidence`, and
storing the same bytes again under a `command_output` label would
misclassify file content as command output. If identical bytes already
exist in the store under a conflicting `source_kind`/`exportable`
classification, `_finish()` fails closed (`evidence_classification_conflict`)
rather than silently accepting the pre-existing, differently-classified
blob as if it were fresh command output.

## Subprocess security model

All execution is structured argv through `subprocess.Popen(..., shell=False)`
— never `shell=True`, `os.system`, or a string command. The environment is
replaced outright with a small, fixed set of variables (never merged with
the calling process's environment, so nothing from it — secrets included —
can leak into a child or into a log). Output capture is bounded per the
request's `output_limit`; a command exceeding its wall-clock timeout, or
producing more output than its limit, is killed via `SIGKILL` to its whole
process group (`start_new_session=True`) and reaped with a bounded wait.
The child's pid is durably recorded (`tool_operations.child_pid`) as soon
as it is known. No raw command output, file content, or environment value
is ever placed in an audit payload — only content hashes, reasons, risk
classes, decisions, and structural identifiers are.

## Request identity

`request_hash` is `compute_request_hash(tool, params)` (Phase 1) over the
capability name plus its validated, structured parameters — task/repo/
worktree identity, path, content/patch hashes (never raw bytes), and the
structured `CommandRequest` fields. It never hashes an ambiguous free-form
command string; canonical JSON serialization (sorted keys) means the same
logical call always hashes identically regardless of field order.

## Tests

`tests/unit/test_policy_engine.py` exercises the decision table directly
against explicit facts. `tests/unit/test_file_tools.py` and
`tests/unit/test_command_tools.py` exercise path safety, symlink/hardlink/
nested-repository/ancestor-replacement defenses, and real subprocess
timeout/output-bound/PID/environment behavior. `tests/integration/
test_tool_execution.py` exercises the full executor against real temporary
Git repositories: allow/deny/require-approval, scope and protected-path
enforcement, ownership transfer and forged-ownership rejection, evidence
classification-conflict handling, and a real subprocess crash between the
STARTED commit and any mutation.

## Explicitly deferred

Lease/fencing, checkpoints, the agent loop, a scheduler/orchestrator, any
model or provider adapter, a WebUI, environments/sandboxing, knowledge,
and training remain out of scope for this phase, per
[`docs/ROADMAP.md`](ROADMAP.md). Automatic reconciliation of an `UNKNOWN`
or crash-orphaned `STARTED` operation is not implemented — Phase 4 blocks
further mutation instead of guessing. No schema migration was needed or
made; schema v1 already carried every column this phase populates.
