# Repository inspection and rule discovery (Phase 3)

Phase 3 records what existed before later engineering work begins. A live
Git status or a path to today's instructions is insufficient: it cannot
prove which changes predated the task or which instruction bytes were
observed. This layer captures that evidence without changing the target.

## Read model

`repo.inspection.inspect_repository(path)` resolves the canonical worktree
root using Foundation identity primitives. Its frozen `RepositoryInspection`
includes repo/worktree IDs, per-worktree and common Git directories, HEAD,
branch, detached/unborn state, object format, shallow status, operation
markers, index object IDs/modes/stages, and Git's path changes.

Git porcelain v2 with `-z` preserves spaces, tabs, CR/LF and Unicode in
filenames. Index/staged and worktree/unstaged status are separate. Rename
records retain Git's similarity result and both paths. Rename detection
uses an explicit 50% threshold and limit of 1000; no rename is guessed from
similar-looking paths. Conflicts retain their unmerged classification and
index stages. Unstaged moves may simply be a deletion plus an untracked
path when Git does not report a rename.

Tracked, untracked, ignored, and index-masked paths are separate. No ordinary
file body, patch, config dump, environment value, or remote URL is included
in inspection metadata. Index/HEAD object IDs are references, not copied
file contents. The observation hash is SHA-256 of canonical metadata with
the observation timestamp omitted, so identical observations hash identically.

## Target preservation and Git execution

All Git invocations use argv through `repo.git`; there is no command shell
or project-code execution. Inherited `GIT_*` overrides cannot redirect the
selected repository or index. System/global Git configuration is excluded;
repository/worktree configuration remains available for repository semantics.
Transport protocols, lazy fetching, prompting, optional index locks/writes,
filesystem monitors, pagers, submodule summaries and recursion are disabled
on the inspection path. Filter commands are disabled, including filters
defined in included and worktree-specific configuration. Such files may
conservatively appear dirty without their configured clean filter.

Gitlinks are reported from HEAD and index, including staged addition,
modification and deletion. Their working trees are **not inspected**. This
phase supplies no submodule mutation support. Git's clean flag here describes
the inspected worktree scope, excluding gitlink interiors and ignored files;
it is not a claim that nested repositories are clean or safe to mutate.

The only permitted target write is Foundation identity establishment:
`codeslayer.repo-id` in shared local Git config and `codeslayer-id` in the
per-worktree Git directory. `establish_identity=False` requires existing
identity. Once established, inspection preserves working-tree, index, HEAD,
refs and Git metadata bytes. Filesystem access times are not a byte-content
guarantee. State databases and evidence must live outside the target and
its Git directories; capture rejects an internal storage location.

## Pre-existing path protection

The task's original protected-path set is recorded once in
`baseline_protected_paths` and in the immutable manifest:

- Any tracked change, staged change, deletion, conflict, or rename endpoint
  receives `pre_existing_dirty`.
- Non-ignored untracked paths and enumerated ignored paths receive
  `pre_existing_untracked`. Ignored files such as `.env` are protected even
  when Git otherwise reports a clean worktree. Their contents are not read.
- Paths marked assume-unchanged or skip-worktree are conservatively protected
  as `pre_existing_dirty`, because ordinary status can hide their changes.
  These paths are explicitly listed as masked, and prevent a clean result.
- An opaque untracked directory, such as an embedded repository, protects
  its descendants. `BaselineRepo.is_protected()` uses path components, not
  a raw string prefix.

Protection does not expire if a user subsequently restores or deletes a
file. A second capture cannot overwrite the original baseline. No rows are
written to `task_owned_paths`: Phase 3 creates no Code Slayer-owned changes,
override decisions, or mutation permission. Submodules and excluded scopes
must still be handled as unsupported boundaries by future mutation policy.

## Document discovery, scope and precedence

Discovery considers tracked paths and non-ignored untracked paths inside
the selected worktree. Names are matched exactly and case-sensitively:

| Filename | Source kind | Role | Precedence |
| --- | --- | --- | --- |
| `AGENTS.md` | instruction | authoritative instruction | directory depth |
| `CLAUDE.md` | instruction | discovered instruction, not activated | -1 |
| `README.md` | documentation | informational document | -1 |
| `CONTRIBUTING.md` | documentation | informational document | -1 |

Only `AGENTS.md` participates in the instruction stack. Its scope is its
containing directory and descendants; root is `.`. Applicable instructions
are returned root-to-deepest, with deeper scope taking precedence for that
subtree. Source-path order breaks discovery-order ties; sibling scopes do
not override one another. An informational document cannot override an
instruction. Rank -1 means excluded from authoritative precedence, not
"low-priority instructions." Textual conflicts are not interpreted here.

External instruction formats are discovered as evidence candidates; their
imports, includes, private/global rule locations, commands and activation
semantics are not invented or executed. No Code Slayer-specific project
rule filename/format was defined by the committed design documents, so this
phase introduces none. Discovery does not fetch instructions from the internet.

Every document records its relative source path, source kind, role, directory
scope, precedence rank, raw-byte SHA-256 and discovery time. Symlinks, Git
internals, ignored documents, gitlink interiors and embedded repositories are
not traversed. Each component of a document path is opened without following
symlinks. Missing tracked documents and unsafe/special paths are reported as
skipped where Git enumerates them. Unreadable or oversized regular documents
fail capture; the default maximum is 1 MiB per document, with no truncation.

## Immutable evidence and schema-v1 representation

Discovery initially returns bytes in memory, without persistence. Successful
capture stores discovered document bytes through `ContentStore`, with blob
`source_kind=rules_snapshot` and `exportable=False`. This blob kind denotes
the captured discovery evidence; it does not confer instruction authority.
Document-level source kind and role remain separate manifest fields.

Raw bytes, including line endings and encoding, determine identity. Later
file edits do not alter earlier evidence. Full bodies never enter audit or
metadata. Ordinary dirty/untracked bodies are not stored as blobs; the named
discovery documents are the deliberate exception. There is no secret-pattern
scanner and no automatic export. Existing blobs are never reclassified:
capture rejects a deduplication hit with an incompatible kind or export flag,
and verifies stored bytes against their content hash.

The existing schema represents one original baseline per task:

- `repo_baselines`: task binding, recording time, HEAD, branch, clean flag
  and a JSON list of structured dirty-path records.
- `baseline_protected_paths`: the original path/reason set.
- `rules_snapshots`: source path, immutable content hash, precedence rank
  and discovery time for each document, including informational evidence.
- `content_blobs`: document evidence and a canonical JSON manifest with
  `source_kind=repository_baseline`, also non-exportable.
- `BASELINE_RECORDED` audit payload: an explicit `baseline_id` →
  `manifest_hash` reference committed with those rows.

The manifest has a versioned format, task/time binding, full inspection
metadata, observation hash, protected paths, rule scope/role/evidence metadata
and skipped documents. Its own SHA-256 is the baseline's content identity.
`BaselineRepo.get()` follows the stored audit reference; it does not guess
which current file belongs to an old snapshot. `InspectionService.read_manifest()`
verifies the manifest hash. No schema migration, config_json extension, audit
hash change, or repurposing of `dirty_files_json` into a different JSON shape
is needed. The manifest's format version is not a SQLite schema version.

## Explicit service and state-machine integration

The caller creates a normal task bound to Foundation repo/worktree IDs,
opens the external database using existing `store.location` paths, then:

```python
from code_slayer.repo.baseline import InspectionService

service = InspectionService(conn, blobs_dir=external_blobs_dir)
service.start(task.task_id)                 # CREATED → INSPECTING
baseline = service.capture(task.task_id)    # INSPECTING → BASELINED
manifest = service.read_manifest(task.task_id)
```

These are explicit calls, with no loop, retries, scheduling or orchestration.
The start transition is the durable inspection-start event. Capture checks
task identity and state, collects observations/documents outside the database
transaction, and repeats metadata/document reads to detect observable changes.

One `BEGIN IMMEDIATE` then covers evidence metadata, baseline/protected/rule
rows, completion audits and the validated transition to `BASELINED` through
`TaskStateMachine.transition_in_transaction()`. This new composition entry
point uses the same graph and guards as `transition()`. It does not make the
transaction helper re-entrant, bypass the engine, or turn guards into writers.

Capture appends `REPO_INSPECTED`, `RULES_LOADED`, `BASELINE_RECORDED` and the
matching `STATE_TRANSITION`. Payloads carry baseline/manifest references,
HEAD/branch identity, structured dirty counts, document identities and
protected-path decisions, without document bodies. Rejection or write failure
rolls all those database writes back. A failed capture normally leaves the
already-started task in `INSPECTING`; a concurrent state change is respected.

As with Foundation content storage, SQLite cannot roll back a filesystem
rename. Failed capture may leave unreferenced immutable blob files outside
the target. They have no committed `content_blobs`, rule or baseline reference
and do not constitute published evidence. A retry safely registers matching
bytes. Automatic orphan-file cleanup is deferred.

## Limits and evidence

This is an observation, not a filesystem transaction or worktree lease.
Repeated reads detect changes in observable Git metadata, candidate paths and
rule bytes; they cannot exclude all concurrent edits, including content changes
that leave an already-dirty status unchanged or changes after the final read.
Later mutation phases must revalidate their preconditions against this baseline.

Non-UTF-8 Git paths fail closed because this API stores literal paths in
schema-v1 TEXT and unchanged canonical UTF-8 audit payloads. It does not invent
a lossy path encoding. Large manifests are bounded by the existing content
store limit. Sparse/masked paths, nested repositories, submodules and unavailable
documents are not silently treated as fully inspected project code.

Tests use temporary Git repositories and check all requested status classes,
worktrees, detached/unborn HEAD, conflicts, ignored/masked paths, rename endpoints,
rule scope/classification, exact bytes, symlink boundaries, disabled helper
execution, storage location, target byte preservation, immutable history and
dirty-content hygiene. Fault injection targets every baseline table and audit
stage. Real subprocess exits before and after commit verify that reopened
databases contain either the original state or complete baseline/evidence/audit.

Phase 4+ remains deferred: policy/planning execution, tools, target mutation,
adapters, models, checkpoint/lease managers, scheduler, orchestrator, agent loop,
daemon, WebUI, environment management, knowledge and training. No installation,
dependency execution, tag, release or network discovery is part of this phase.
