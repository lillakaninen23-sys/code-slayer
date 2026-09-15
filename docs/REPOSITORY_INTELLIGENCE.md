# Repository Intelligence — Phase 8.1 foundation

Deterministic, evidence-only repository understanding — never a model
guess treated as fact. See `src/code_slayer/intelligence/` (the
implementation) and `docs/ROADMAP.md`'s Engineering Intelligence section
for where this sits in the overall dependency chain.

```
repository
    ↓
deterministic inspection (repo.inspection.inspect_repository, unmodified)
    ↓
intelligence.builder.build_snapshot()  -- inventory, detection, commands,
    ↓                                     symbols, import graph
durable Snapshot (content-addressed blob + a small pointer row)
    ↓
intelligence.query.rank() / build_context_pack()
    ↓
bounded ContextPack -- for a future Prompt Analyst / planner / worker
```

Never: *repository → ask a model to guess everything → treat the answer
as truth.* Every fact this package returns cites the concrete file(s) it
came from; nothing here is authoritative merely because a model said so.

## What is indexed

The candidate file list is exactly `repo.inspection.inspect_repository()`'s
own tracked (stage-0) index entries plus untracked-but-not-ignored
paths — the same Git-authoritative, already-tested classification every
other part of this codebase already relies on. A file is then read
(content hashed, and — for a registered extractor — parsed for symbols)
only if it is not binary and not larger than
`intelligence.limits.MAX_TEXT_FILE_BYTES` (512 KiB), and only until the
aggregate `MAX_TOTAL_INDEXED_BYTES` (32 MiB) budget for one snapshot is
used up; anything past either bound is still listed (path/size/language
known) but marked `"oversized"`, never read.

## What is excluded

- `git`-ignored paths, by default (a repository's own `.gitignore`
  already curated these — `inspect_repository()`'s `ignored_paths` is
  never in the candidate set).
- A fixed, defense-in-depth deny-list of unambiguous dependency/cache/
  build-output directory names, regardless of `.gitignore`:
  `.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, `.tox`, `.nox`,
  `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `dist`, `build`,
  `target`, `.next`, `.nuxt`, `coverage`, `.idea`, `.vscode`, and any
  `*.egg-info` directory (`intelligence.paths.EXCLUDED_DIR_NAMES`).
- Code Slayer's own external durable state root, even if a caller's
  `CODESLAYER_STATE_ROOT` happens to be configured *inside* the
  repository being inspected.
- Anything a candidate path resolves (symlinks included) to *outside*
  the repository root — fail closed, never followed.

## Limits (`intelligence.limits`)

`MAX_INVENTORY_FILES` (5000) bounds how many candidate files one
snapshot ever inspects at all — beyond it, the inventory is truncated
deterministically (sorted-path order) and `Snapshot.inventory_truncated`
records that it was. `MAX_QUERY_RESULTS` (50) and context-pack
`max_files`/`max_bytes`/`per_file_bytes` bound query/context-pack
responses the same way — every bound is a hard ceiling a caller can only
narrow, never widen.

## Evidence model

Every `ProjectEvidence` names the concrete marker file(s) that produced
it (`pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `*.sln`/
`*.csproj`, `pom.xml`/`build.gradle[.kts]`, ...) — a file extension
existing somewhere in the repository is never, by itself, evidence of a
framework. Every `CommandCandidate` names its exact `evidence_source`
(a `package.json` script name, a `pyproject.toml` `[tool.*]` section, a
`Makefile` target, `tox.ini`/`noxfile.py` presence) and a `confidence`
reflecting how directly that evidence names a real command — **nothing
discovered here is ever executed** by this package; discovery and
execution are deliberately different decisions.

Python symbol extraction uses the standard library's own `ast` module
only (`intelligence.symbols`, a small per-extension registry so a later
language gets its own narrow extractor without touching this one) — a
malformed file is recorded in `Snapshot.symbol_errors` and skipped;
it never invalidates the rest of the snapshot. The import graph
(`intelligence.graph`) resolves only real AST import statements against
files that actually exist in this same snapshot's own inventory — an
import of a package the repository does not itself contain resolves to
nothing, never a filename-similarity guess.

## Refresh / invalidation

A snapshot is bound to `(repo_id, worktree_id, head_sha,
working_tree_dirty, working_tree_fingerprint)`. `working_tree_fingerprint`
is a cheap `stat()`-only fingerprint (path, size, mtime — never file
content) over the exact bounded candidate set, so a caller can detect
that the working tree has changed since the snapshot was taken even
when `head_sha` has not moved, without re-reading a single byte.
`RepositoryIntelligenceService.status()`/`.query()`/`.build_context_pack()`
never rebuild as a side effect — they read the latest durable snapshot
and report `current`/`stale` honestly; only `.inspect()` (explicit, or
the WebUI's `POST /api/intelligence/refresh`) performs a fresh, full,
deterministic rebuild. There is no incremental indexing in Phase 8.1 —
a full bounded rebuild is the entire refresh model.

## Durability

`repository_intelligence_snapshots` (migration `0007`, purely additive —
`0001`–`0006` are unchanged) is a small, append-only pointer row per
snapshot: identity fields plus `snapshot_content_hash`, referencing the
actual bounded inventory/project/command/symbol/graph payload as one
content-addressed JSON blob in the existing `content_blobs` table — the
same "thin row, content-addressed payload" shape every other durable
evidence table in this codebase already uses. A fresh `.inspect()` never
overwrites a prior snapshot row; "current" is simply the most recent one
for a given `(repo_id, worktree_id)` scope.

## Context-pack semantics

A `ContextPack` is never durably stored — it is computed on demand from
the latest durable snapshot for one `(query text, budgets)` request.
Candidates are ranked deterministically (`intelligence.query.rank()`,
fixed integer weights, no model of any kind) and then materialized
in ranked order until `max_files`/`max_bytes`/`per_file_bytes` is
exhausted; anything selected but not fitting is recorded in `omitted`,
and `budget_exhausted` is set — a context pack never silently becomes
"the whole repository." `stale=True` means the repository has changed
since the underlying snapshot was taken; the caller decides whether to
refresh first.

## Security / scope

Repository Intelligence is read-only end to end: it never writes a
repository file, never executes a discovered command, and never grants
worker trust, mutation authority, checkpoints, lease changes, or cloud
transport authorization — nothing in this package imports
`tools.executor`, `lease.manager`, `repo.checkpoint`, or `workers.
cloud_escalation`. `RepositoryIntelligenceService` is a *new* durable-
evidence source, not a new decision authority: a future Prompt Analyst
may use it as one more authoritative input, but a repository fact never
becomes trusted `workers.question_gate.ResolutionEvidence` except
through that existing, hardened, application-owned authority path —
this package cannot construct one itself.

## WebUI / HTTP API

`GET /api/intelligence/status`, `POST /api/intelligence/query`,
`POST /api/intelligence/context-pack`, `POST /api/intelligence/refresh` —
see `docs/WEBUI_API.md`. The repository is always the server's own
configured project; no request body field can ever name a filesystem
root, database path, trust level, or tool permission.
