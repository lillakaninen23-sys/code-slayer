"""Bounded HTTP projections over durable state. Connections are SQLite read-only.

No raw prompt, answer, provider config, arbitrary blob, lease or fencing fields
cross this boundary. Historical provenance without run_id is labelled as shared
prompt identity, never asserted to be unique run evidence.
"""

import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict

from code_slayer.repo import git
from code_slayer.store import db, location
from code_slayer.store.checkpoint_repo import CheckpointRepo, parse_verified
from code_slayer.store.conformance_repo import ConformanceRepo
from code_slayer.store.runner_repo import RunnerRepo
from code_slayer.store.task_repo import TaskRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.conformance import SUITE_VERSION
from code_slayer.workers.prompt_provenance import read_prompt_analysis
from code_slayer.workers.trust import WorkerTrustManager


class ResourceNotFound(Exception):
    pass


def reason_code(value):
    """Only the machine code; diagnostic suffixes can contain paths or provider text."""
    if value is None:
        return None
    prefix = value.split(":", 1)[0]
    return prefix if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]{0,127}", prefix) else "details_omitted"


@contextmanager
def readonly(path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()


class ReadModels:
    def __init__(self, identity, state_root=None):
        self.identity, self.state_root = identity, state_root

    def path(self, worktree_id):
        return location.db_path(self.identity.repo_id, worktree_id, override=self.state_root)

    def __enter__(self):
        self._context = readonly(self.path(self.identity.worktree_id))
        self.conn = self._context.__enter__()
        return self

    def __exit__(self, *args):
        return self._context.__exit__(*args)

    def project(self):
        head = git.head_sha(self.identity.repo_root)
        branch = git.current_branch(self.identity.repo_root)
        return {
            "repo_id": self.identity.repo_id,
            "worktree_id": self.identity.worktree_id,
            "display_name": self.identity.repo_root.name,
            "repository_path": str(self.identity.repo_root),
            "head": head,
            "branch": branch,
            "detached": head is not None and branch is None,
            "control_plane_available": True,
            "schema_version": db.schema_version(self.conn),
        }

    def run(self, run_id):
        run = RunnerRepo(self.conn).get_or_none(run_id)
        if run is None:
            raise ResourceNotFound()
        return run

    def list_runs(self, limit, offset):
        rows = self.conn.execute(
            "SELECT run_id FROM runner_runs ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (limit + 1, offset),
        ).fetchall()
        return {
            "runs": [self.summary(self.run(row["run_id"])) for row in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def summary(self, run):
        return {
            name: getattr(run, name)
            for name in (
                "run_id",
                "task_id",
                "status",
                "worker_id",
                "role",
                "created_at",
                "updated_at",
                "execution_worktree_id",
            )
        } | {
            "reason": reason_code(run.reason),
            "questions": json.loads(run.questions_json) if run.questions_json else [],
        }

    def questions(self, run):
        if run.status != "BLOCKED_ON_QUESTIONS" or not run.analysis_content_hash:
            return []
        analysis = read_prompt_analysis(
            self.conn,
            location.blobs_dir(
                self.identity.repo_id,
                self.identity.worktree_id,
                override=self.state_root,
            ),
            run.analysis_content_hash,
        )
        gate = self.conn.execute(
            "SELECT payload_json FROM audit_events WHERE event_type='QUESTION_GATE_DECISION' "
            "AND json_extract(payload_json, '$.run_id') = ? ORDER BY id DESC LIMIT 1",
            (run.run_id,),
        ).fetchone()
        unresolved = (
            None
            if gate is None
            else {
                reason.rsplit(":unresolved_", 1)[0]
                for reason in json.loads(gate["payload_json"]).get("reasons", [])
                if ":unresolved_" in reason
            }
        )
        # Legacy records only have question strings; conservatively show matching
        # ambiguities. The real runner still decides whether a resolution suffices.
        texts = json.loads(run.questions_json or "[]")
        answers = {
            r.ambiguity_id for r in RunnerRepo(self.conn).latest_human_resolutions(run.run_id)
        }
        return [
            {
                "ambiguity_id": a.id,
                "question": a.question,
                "risk_class": a.risk_class.value,
                "answer_recorded": a.id in answers,
            }
            for a in analysis.ambiguities
            if (a.id in unresolved if unresolved is not None else a.question in texts)
        ]

    def detail(self, run_id):
        run = self.run(run_id)
        data = self.summary(run) | {
            "original_prompt_hash": run.original_prompt_hash,
            "analysis_content_hash": run.analysis_content_hash,
            "questions": self.questions(run),
            "task_status": None,
            "checkpoint_refs": [],
            "execution": {
                "repo_id": run.repo_id,
                "worktree_id": run.execution_worktree_id,
                "isolated": run.job_worktree_path is not None,
            },
            "tool_operation_id": run.tool_operation_id,
            "trust": self.trust(run.worker_id, role=run.role, include_history=False),
            "next_safe_action": {
                "ANALYZING": "wait",
                "RUNNING": "wait",
                "READY": "resume",
                "BLOCKED_ON_QUESTIONS": "answer_then_resume",
                "INTERRUPTED_RESUMABLE": "operator_reconciliation",
            }.get(run.status, "none"),
            "execution_state_available": run.task_id is None,
        }
        if run.task_id:
            try:
                with readonly(self.path(run.execution_worktree_id)) as conn:
                    data["task_status"] = TaskRepo(conn).get(run.task_id).state
                    data["checkpoint_refs"] = [
                        {
                            "checkpoint_id": c.checkpoint_id,
                            "git_ref": c.git_ref,
                            "commit_sha": parse_verified(c).commit_sha,
                            "created_at": c.created_at,
                        }
                        for c in CheckpointRepo(conn).list_for_task(run.task_id)[-100:]
                    ]
                    data["execution_state_available"] = True
            except (sqlite3.OperationalError, KeyError):
                data["execution_state_available"] = False
        return data

    def worker(self, worker_id):
        return WorkersRepo(self.conn).get(worker_id)

    def workers(self):
        rows = self.conn.execute("SELECT worker_id FROM workers ORDER BY worker_id").fetchall()
        return [
            {
                key: getattr(worker, key)
                for key in (
                    "worker_id",
                    "kind",
                    "network_class",
                    "availability_state",
                    "last_probe_at",
                )
            }
            | {"trust": self.trust(worker.worker_id, include_history=False)}
            for row in rows
            if (worker := self.worker(row["worker_id"]))
        ]

    def trust(self, worker_id, *, role=None, include_history=True):
        manager = WorkerTrustManager(self.conn)
        rows = self.conn.execute(
            "SELECT DISTINCT role, capability FROM worker_trust_events WHERE worker_id=?",
            (worker_id,),
        ).fetchall()
        scopes = {(row["role"], row["capability"]) for row in rows}
        # Display explicit LOCKED read/mutation scopes for relevant roles, asking
        # the existing trust service for each. No inheritance or UI-derived trust.
        roles = (
            {
                r["role"]
                for r in self.conn.execute(
                    "SELECT DISTINCT role FROM runner_runs WHERE worker_id=? UNION "
                    "SELECT DISTINCT role FROM worker_conformance_runs WHERE worker_id=?",
                    (worker_id, worker_id),
                )
            }
            | {r for r, _ in scopes}
            | ({role} if role else set())
        )
        scopes |= {(r, cap) for r in roles for cap in (None, "read_file", "write_file")}
        result = []
        for r, cap in sorted(scopes, key=lambda pair: (pair[0], pair[1] or "")):
            if role is not None and r != role:
                continue
            item = {
                "worker_id": worker_id,
                "role": r,
                "capability": cap,
                "level": manager.current_trust(worker_id, r, cap).value,
            }
            if include_history:
                history = manager.history(worker_id, r, cap)
                item["history"] = [
                    {
                        "id": e.id,
                        "from_level": e.from_level,
                        "to_level": e.to_level,
                        "occurred_at": e.occurred_at,
                        "reason": reason_code(e.reason),
                    }
                    for e in history[-100:]
                ]
                item["history_truncated"] = len(history) > 100
            result.append(item)
        return {"scopes": result, "unrecorded_scope_level": "LOCKED", "matching": "exact"}

    def conformance(self, worker_id):
        repo = ConformanceRepo(self.conn)
        rows = self.conn.execute(
            "SELECT run_id FROM worker_conformance_runs WHERE worker_id=? "
            "ORDER BY started_at DESC, rowid DESC LIMIT 20",
            (worker_id,),
        ).fetchall()
        return {
            "current_suite_version": SUITE_VERSION,
            "runs": [
                asdict(repo.get_run(row["run_id"]))
                | {
                    "results": [
                        {
                            "case_name": r.case_name,
                            "passed": r.passed,
                            "reason": reason_code(r.reason),
                            "occurred_at": r.occurred_at,
                        }
                        for r in repo.list_results(row["run_id"])
                    ]
                }
                for row in rows
            ],
        }

    def audit(self, run_id, limit):
        run = self.run(run_id)
        events = self._audit_events(self.conn, run, limit, "control")
        execution_available = True
        if run.task_id and run.execution_worktree_id != self.identity.worktree_id:
            try:
                with readonly(self.path(run.execution_worktree_id)) as conn:
                    events += self._audit_events(conn, run, limit, "execution")
            except sqlite3.OperationalError:
                execution_available = False
        events.sort(key=lambda e: (e["occurred_at"], e["plane"], e["id"]))
        return {
            "events": events[-limit:],
            "limit": limit,
            "execution_state_available": execution_available,
            "integrity": "not_verified_by_this_endpoint",
        }

    @staticmethod
    def _audit_events(conn, run, limit, plane):
        rows = conn.execute(
            "SELECT id, occurred_at, event_type, payload_json, task_id FROM audit_events "
            "WHERE json_extract(payload_json, '$.run_id')=? OR (task_id IS NOT NULL AND task_id=?) "
            "OR (event_type IN ('PROMPT_ANALYSIS_RECORDED', 'QUESTION_GATE_DECISION') "
            "AND json_extract(payload_json, '$.run_id') IS NULL "
            "AND json_extract(payload_json, '$.analysis_content_hash')=?) "
            "ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (run.run_id, run.task_id, run.analysis_content_hash, limit),
        ).fetchall()
        events = []
        # Only short machine codes / enum fields. No payload dumping, tool params,
        # prompts, answers, arbitrary reason strings, resource paths or credentials.
        for row in rows:
            payload = json.loads(row["payload_json"])
            details = {
                key: reason_code(payload[key])
                for key in (
                    "decision",
                    "status",
                    "reason",
                    "from_level",
                    "to_level",
                    "resolution_kind",
                    "trust_level",
                    "outcome",
                    "tool",
                )
                if isinstance(payload.get(key), str)
            }
            events.append(
                {
                    "id": row["id"],
                    "occurred_at": row["occurred_at"],
                    "event_type": row["event_type"],
                    "details": details,
                    "plane": plane,
                    "association": "run"
                    if payload.get("run_id") == run.run_id
                    or (row["task_id"] is not None and row["task_id"] == run.task_id)
                    else "shared_prompt_identity",
                }
            )
        return events
