"""Evidence validation: the one place a planner's raw claim either earns
authoritative status or is corrected/dropped (Phase 8.2).

## Core invariant

A model-generated claim is not repository fact. Every concrete
repository claim in a `planning.planner.PlannerStructuredOutput` —
"this file exists," "this command is discovered," "this symbol exists"
— is checked here against exactly one authoritative source: an already
-built `intelligence.models.Snapshot` (Phase 8.1/8.1a). Nothing here
re-reads the filesystem, re-runs Git, or calls a model; this module is
pure and deterministic over `(output, snapshot)`.

## Existing-file claims never become fact "because the planner said so"

`AffectedFile.exists_in_repository` is set here, from `snapshot.files`,
never from `PlannerAffectedFileProposal` itself. A `create` action
against a path that genuinely does not exist yet is a legitimate
proposal — `exists_in_repository=False`, tagged with a `"file_absent"`
evidence reference (itself real evidence: the authoritative snapshot was
checked and the path was confirmed absent). A `modify`/`delete`/
`inspect` action against a path that does not exist, or a `create`
against a path that already does, is a genuine **defect**: `blocking=
True` is returned for that case, and the caller (`planning.service`)
must never let such a plan reach `READY` — it requires a fresh
`replan()`, never a silent auto-correction of the action or the claim.

## Advisory-only claims are dropped, never fabricated, never blocking

A `discovered_commands` entry only survives if its exact `command`
string matches an authoritative `intelligence.models.CommandCandidate`
in `snapshot.commands` — and when it does, this module substitutes that
candidate's own `purpose`/`evidence_source`, never the planner's own
(possibly fabricated) description of why it's discovered. An
unmatched command is simply dropped, with an advisory issue recorded —
never blocking, since a plan's verification suggestions being
over-eager is not the same defect class as claiming false repository
facts about files it says to change.

An `evidence_claims` entry (`"file_exists"` / `"symbol_exists"` /
`"command_discovered"`) that does not check out against the snapshot is
likewise dropped with an advisory issue — see `docs/
ENGINEERING_PLANNING.md` for the full mapping.
"""

from __future__ import annotations

from dataclasses import dataclass

from code_slayer.intelligence.models import Snapshot
from code_slayer.planning.models import (
    AffectedFile,
    AffectedFileAction,
    DiscoveredCommandRef,
    EngineeringPlanContent,
    EvidenceRef,
    OpenQuestion,
    PlannedChange,
)
from code_slayer.planning.planner import PlannerStructuredOutput

_CREATE = AffectedFileAction.CREATE


@dataclass(frozen=True)
class EvidenceValidationResult:
    """`blocking` is `True` iff at least one affected-file claim
    genuinely contradicts authoritative repository fact (an existing
    path claimed absent for `create`, or a nonexistent path claimed
    present for `inspect`/`modify`/`delete`) — the only class of defect
    this module treats as disqualifying for `READY`. Every other
    correction (`issues`) is advisory: the claim was simply dropped or
    corrected, never promoted, but does not by itself block the plan."""

    content: EngineeringPlanContent
    blocking: bool
    issues: tuple[str, ...]


def validate_plan_against_intelligence(
    output: PlannerStructuredOutput, snapshot: Snapshot,
) -> EvidenceValidationResult:
    if not isinstance(output, PlannerStructuredOutput):
        raise TypeError("output must be a PlannerStructuredOutput")
    if not isinstance(snapshot, Snapshot):
        raise TypeError("snapshot must be an intelligence.models.Snapshot")

    existing_paths = {f.path for f in snapshot.files}
    symbol_names = {s.qualname for s in snapshot.symbols}
    commands_by_name = {c.command: c for c in snapshot.commands}

    issues: list[str] = []
    blocking = False

    affected_files: list[AffectedFile] = []
    for proposal in output.affected_files:
        action = AffectedFileAction(proposal.action)
        exists = proposal.path in existing_paths
        if action == _CREATE:
            if exists:
                issues.append(f"affected_file:{proposal.path}:create_but_already_exists")
                blocking = True
            evidence = (EvidenceRef(
                kind="file_absent" if not exists else "file_exists",
                key=proposal.path, snapshot_id=snapshot.snapshot_id,
            ),)
        else:
            if not exists:
                issues.append(
                    f"affected_file:{proposal.path}:{action.value}_but_does_not_exist"
                )
                blocking = True
            evidence = (EvidenceRef(
                kind="file_exists" if exists else "file_missing",
                key=proposal.path, snapshot_id=snapshot.snapshot_id,
            ),)
        affected_files.append(AffectedFile(
            path=proposal.path, action=action, reason=proposal.reason,
            exists_in_repository=exists, evidence=evidence,
        ))

    discovered_commands: list[DiscoveredCommandRef] = []
    for proposal in output.discovered_commands:
        match = commands_by_name.get(proposal.command)
        if match is None:
            issues.append(f"discovered_command:{proposal.command}:not_found_in_repository")
            continue
        # The authoritative candidate's own fields replace the planner's
        # own claim entirely -- never a mix of trusted command name with
        # an untrusted purpose/evidence_source.
        discovered_commands.append(DiscoveredCommandRef(
            command=match.command, purpose=match.purpose, evidence_source=match.evidence_source,
        ))

    evidence_refs: list[EvidenceRef] = []
    for claim in output.evidence_claims:
        if claim.kind == "file_exists":
            if claim.key in existing_paths:
                evidence_refs.append(EvidenceRef(claim.kind, claim.key, snapshot.snapshot_id))
            else:
                issues.append(f"evidence_claim:file_exists:{claim.key}:unsupported")
        elif claim.kind == "symbol_exists":
            if claim.key in symbol_names:
                evidence_refs.append(EvidenceRef(claim.kind, claim.key, snapshot.snapshot_id))
            else:
                issues.append(f"evidence_claim:symbol_exists:{claim.key}:unsupported")
        elif claim.kind == "command_discovered":
            if claim.key in commands_by_name:
                evidence_refs.append(EvidenceRef(claim.kind, claim.key, snapshot.snapshot_id))
            else:
                issues.append(f"evidence_claim:command_discovered:{claim.key}:unsupported")
        else:
            issues.append(f"evidence_claim:{claim.kind}:unknown_kind")

    open_questions = tuple(
        OpenQuestion(
            ambiguity_id=a.id, question=a.question, risk_class=a.risk_class.value,
            resolved=False,
        )
        for a in output.ambiguities
    )

    content = EngineeringPlanContent(
        goal=output.goal, requirements=output.requirements, assumptions=output.assumptions,
        affected_files=tuple(affected_files),
        planned_changes=tuple(
            PlannedChange(description=c.description, paths=c.paths)
            for c in output.planned_changes
        ),
        dependencies=output.dependencies, risks=output.risks,
        verification_steps=output.verification_steps,
        discovered_commands=tuple(discovered_commands),
        authority_requirements=output.authority_requirements,
        open_questions=open_questions, evidence_refs=tuple(evidence_refs),
        validation_issues=tuple(issues),
    )
    return EvidenceValidationResult(content=content, blocking=blocking, issues=tuple(issues))
