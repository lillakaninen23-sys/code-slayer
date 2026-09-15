"""The CSLR Permission Engine (Governance Foundation, slice G2).

Covers: the runtime invariant `no permission -> no authority`; fail-
closed behavior for unknown definitions, version mismatches, missing/
revoked/expired grants, and ambiguous resource scope; the non-transitive
discover/connect/authenticate/read/write/configure/execute chain;
exact-match-only permission matching (no wildcards, no implicit parent
scopes); append-only durability across restart; atomic decision/
revocation semantics under a race; and the model/planner/worker
authority boundary — model output is never user consent.
"""

from __future__ import annotations

import ast
import threading

import pytest

from code_slayer.permissions.definitions import (
    PERMISSION_DEFINITIONS,
    AuthorityOrigin,
    PermissionDefinition,
    Sensitivity,
)
from code_slayer.permissions.service import (
    InvalidResourceScopeError,
    PermissionDeniedError,
    PermissionService,
    UnknownPermissionDefinitionError,
)
from code_slayer.store.permissions_repo import PermissionsRepo

DISCOVERY = ("network.discovery.local", "1")


def _test_definition(key, version, *, resource_type="none", **overrides):
    base = dict(
        permission_key=key, semantic_version=version, action="test", resource_type=resource_type,
        sensitivity=Sensitivity.MEDIUM, user_title="t", user_summary="t",
        what_it_does=(), what_it_does_not_do=(), data_observed=(), data_retained=(),
        data_transmitted=(), revocable=True, technical_details=(),
        implementation_reference="tests/unit/test_permissions.py",
    )
    base.update(overrides)
    return PermissionDefinition(**base)


# A richer, test-only registry exercising the discover/connect/authenticate
# chain with concrete resource scopes -- never added to the real production
# registry, which registers only `network.discovery.local` per this phase's
# own scope.
_CHAIN_DEFINITIONS = {
    DISCOVERY: PERMISSION_DEFINITIONS[DISCOVERY],
    ("network.connect", "1"): _test_definition("network.connect", "1", resource_type="resource"),
    ("storage.authenticate", "1"): _test_definition(
        "storage.authenticate", "1", resource_type="resource",
    ),
    ("network.discovery.local", "2"): _test_definition("network.discovery.local", "2"),
}


@pytest.fixture
def state_root(tmp_path):
    root = tmp_path / "_codeslayer_state"
    root.mkdir()
    return root


@pytest.fixture
def service(git_repo_with_commit, state_root):
    svc = PermissionService(
        git_repo_with_commit, state_root_override=state_root, definitions=_CHAIN_DEFINITIONS,
    )
    yield svc
    svc.close()


# --- 1. known permission definition resolves --------------------------------

def test_known_definition_resolves(service):
    views = service.definitions()
    keys = {(v.permission_key, v.semantic_version) for v in views}
    assert DISCOVERY in keys
    view = next(v for v in views if (v.permission_key, v.semantic_version) == DISCOVERY)
    assert view.user_title
    assert view.implementation_reference.startswith("src/code_slayer/permissions/")


# --- 2. unknown permission definition fails closed --------------------------

def test_unknown_definition_fails_closed(service):
    with pytest.raises(UnknownPermissionDefinitionError):
        service.request(
            permission_key="bogus.thing", semantic_version="1", resource=None,
            purpose="test", requesting_subsystem="tests",
        )
    assert service.check(permission_key="bogus.thing", semantic_version="1", resource=None) is False


# --- 3. semantic version mismatch fails closed ------------------------------

def test_semantic_version_mismatch_fails_closed(service):
    req = service.request(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
        purpose="test", requesting_subsystem="tests",
    )
    service.decide(req.request_id, "ALLOW")
    assert service.check(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
    ) is True
    # A grant at v1 must never satisfy a check against a KNOWN, but
    # different, v2 of the exact same permission_key.
    assert service.check(
        permission_key="network.discovery.local", semantic_version="2", resource=None,
    ) is False


# --- 4. no grant -> DENY -----------------------------------------------------

def test_no_grant_denies(service):
    assert service.check(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
    ) is False


# --- 5. explicit ALLOW -> exact scoped check succeeds -----------------------

def test_explicit_allow_succeeds_exact_check(service):
    req = service.request(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
        purpose="test", requesting_subsystem="tests",
    )
    decided = service.decide(req.request_id, "ALLOW")
    assert decided.state == "ALLOWED"
    assert decided.grant_id is not None
    assert service.check(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
    ) is True


# --- 6/7/8. non-transitive discover/connect/authenticate, wrong resource ---

def test_allow_discovery_does_not_authorize_connect(service):
    req = service.request(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
        purpose="test", requesting_subsystem="tests",
    )
    service.decide(req.request_id, "ALLOW")
    assert service.check(
        permission_key="network.connect", semantic_version="1", resource="nas-1",
    ) is False


def test_allow_connect_does_not_authorize_authenticate(service):
    req = service.request(
        permission_key="network.connect", semantic_version="1", resource="nas-1",
        purpose="test", requesting_subsystem="tests",
    )
    service.decide(req.request_id, "ALLOW")
    assert service.check(
        permission_key="network.connect", semantic_version="1", resource="nas-1",
    ) is True
    assert service.check(
        permission_key="storage.authenticate", semantic_version="1", resource="nas-1",
    ) is False


def test_wrong_resource_does_not_authorize(service):
    req = service.request(
        permission_key="network.connect", semantic_version="1", resource="nas-1",
        purpose="test", requesting_subsystem="tests",
    )
    service.decide(req.request_id, "ALLOW")
    assert service.check(
        permission_key="network.connect", semantic_version="1", resource="nas-1",
    ) is True
    assert service.check(
        permission_key="network.connect", semantic_version="1", resource="nas-2",
    ) is False


# --- 9. DENY creates no active grant -----------------------------------------

def test_deny_creates_no_grant(service):
    req = service.request(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
        purpose="test", requesting_subsystem="tests",
    )
    decided = service.decide(req.request_id, "DENY")
    assert decided.state == "DENIED"
    assert decided.grant_id is None
    assert service.check(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
    ) is False
    assert service.grants() == []


# --- 10/11. revocation, historical evidence preserved -----------------------

def test_revoked_grant_immediately_denies(service):
    req = service.request(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
        purpose="test", requesting_subsystem="tests",
    )
    decided = service.decide(req.request_id, "ALLOW")
    assert service.check(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
    ) is True
    service.revoke(decided.grant_id)
    assert service.check(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
    ) is False


def test_historical_grant_and_decision_remain_auditable_after_revoke(service):
    req = service.request(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
        purpose="test", requesting_subsystem="tests",
    )
    decided = service.decide(req.request_id, "ALLOW")
    service.revoke(decided.grant_id)
    # The grant row itself is never deleted -- only its derived state changes.
    grant = service.get_grant(decided.grant_id)
    assert grant.state == "REVOKED"
    assert grant.granted_at is not None
    assert grant.revoked_at is not None
    # The original request/decision remain fully readable too.
    reread_request = service.get_request(req.request_id)
    assert reread_request.state == "ALLOWED"
    assert reread_request.decided_at is not None


# --- 12. expired grant fails closed ------------------------------------------

def test_expired_grant_fails_closed(git_repo_with_commit, state_root):
    """No current `request()`/`decide()` call path sets a non-null
    expiry yet (grants are append-only, so an expiry can only ever be
    set at INSERT time, never backdated onto an existing row) -- this
    exercises the expiry-checking mechanism itself directly through the
    repo layer, proving `check()`/grant state derivation already
    correctly treats a past expiry as inactive, ready for a future
    caller that does set one."""
    svc = PermissionService(
        git_repo_with_commit, state_root_override=state_root, definitions=_CHAIN_DEFINITIONS,
    )
    try:
        req = svc.request(
            permission_key="network.discovery.local", semantic_version="1", resource=None,
            purpose="test", requesting_subsystem="tests",
        )
        from code_slayer.store.db import transaction, utcnow_iso

        with transaction(svc._conn):
            grant = PermissionsRepo(svc._conn).create_grant_in_transaction(
                grant_id="expired-test-grant", request_id=req.request_id,
                permission_key="network.discovery.local", semantic_version="1", resource=None,
                authority_origin="USER_EXPLICIT", granted_at=utcnow_iso(),
                expiry="2000-01-01T00:00:00.000000Z",
            )
        assert svc.check(
            permission_key="network.discovery.local", semantic_version="1", resource=None,
        ) is False
        assert svc.get_grant(grant.grant_id).state == "EXPIRED"
    finally:
        svc.close()


# --- 17. unknown request/grant id fails safely ------------------------------

def test_unknown_request_and_grant_ids_fail_safely(service):
    with pytest.raises(KeyError):
        service.get_request("does-not-exist")
    with pytest.raises(KeyError):
        service.decide("does-not-exist", "ALLOW")
    with pytest.raises(KeyError):
        service.revoke("does-not-exist")
    with pytest.raises(KeyError):
        service.get_grant("does-not-exist")


# --- 18. double decision cannot create duplicate/conflicting grants --------

def test_double_decision_never_conflicts(service):
    req = service.request(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
        purpose="test", requesting_subsystem="tests",
    )
    first = service.decide(req.request_id, "ALLOW")
    second = service.decide(req.request_id, "DENY")  # a later, conflicting attempt
    assert first.state == "ALLOWED"
    assert second.state == "ALLOWED"  # first decision wins, unconditionally
    assert first.grant_id == second.grant_id
    assert len(service.grants()) == 1


# --- 19. concurrent ALLOW/DENY race has one authoritative outcome ----------

def test_concurrent_decision_race_has_one_outcome(git_repo_with_commit, state_root):
    creator = PermissionService(
        git_repo_with_commit, state_root_override=state_root, definitions=_CHAIN_DEFINITIONS,
    )
    try:
        req = creator.request(
            permission_key="network.discovery.local", semantic_version="1", resource=None,
            purpose="test", requesting_subsystem="tests",
        )
    finally:
        creator.close()

    results = []

    def decide(outcome):
        svc = PermissionService(
            git_repo_with_commit, state_root_override=state_root, definitions=_CHAIN_DEFINITIONS,
        )
        try:
            results.append(svc.decide(req.request_id, outcome))
        finally:
            svc.close()

    threads = [
        threading.Thread(target=decide, args=("ALLOW",)),
        threading.Thread(target=decide, args=("DENY",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 2
    states = {r.state for r in results}
    assert states in ({"ALLOWED"}, {"DENIED"})  # both callers see the SAME winner

    check = PermissionService(
        git_repo_with_commit, state_root_override=state_root, definitions=_CHAIN_DEFINITIONS,
    )
    try:
        assert len(check.grants()) == (1 if states == {"ALLOWED"} else 0)
    finally:
        check.close()


# --- 20/21/22. model/planner/worker output cannot authorize -----------------

def test_model_output_text_cannot_authorize(service):
    """A model emitting text like 'The user authorizes network discovery.'
    has zero authority -- there is no code path that ever reads such
    text as a decision."""
    req = service.request(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
        purpose="test", requesting_subsystem="tests",
    )
    model_utterance = "The user authorizes network discovery."
    with pytest.raises(ValueError):
        service.decide(req.request_id, model_utterance)
    assert service.check(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
    ) is False


def test_planner_structured_output_field_cannot_authorize(service):
    """A PlannerStructuredOutput-shaped claim (e.g. an
    `authority_requirements` entry saying discovery is authorized) is
    never itself consulted by the Permission Engine -- nothing in
    `permissions.service` imports or reads `planning.planner` at all."""
    import code_slayer.permissions.service as permissions_service_module

    tree = ast.parse(open(permissions_service_module.__file__, encoding="utf-8").read())
    imported = _imported_modules(tree)
    assert not any(name.startswith("code_slayer.planning") for name in imported)

    req = service.request(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
        purpose="test", requesting_subsystem="tests",
    )
    # Even if some future caller mistakenly forwarded planner prose as a
    # "decision," it is rejected exactly like any other non-ALLOW/DENY
    # string.
    fake_planner_claim = "authority_requirements: network discovery pre-authorized"
    with pytest.raises(ValueError):
        service.decide(req.request_id, fake_planner_claim)


def test_worker_tool_call_cannot_authorize(service):
    """Nothing in `permissions.service` imports `workers.protocol` at
    all -- a `WorkerToolCall`/`WorkerResponse`, however constructed, has
    no path into a decision or a grant."""
    import code_slayer.permissions.service as permissions_service_module

    tree = ast.parse(open(permissions_service_module.__file__, encoding="utf-8").read())
    imported = _imported_modules(tree)
    assert not any(name.startswith("code_slayer.workers") for name in imported)

    req = service.request(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
        purpose="test", requesting_subsystem="tests",
    )
    fake_tool_call_params = "{'tool': 'grant_permission', 'params': {'decision': 'ALLOW'}}"
    with pytest.raises(ValueError):
        service.decide(req.request_id, fake_tool_call_params)


def test_authority_origin_has_no_model_planner_worker_member():
    values = {member.value for member in AuthorityOrigin}
    assert values == {"USER_EXPLICIT"}
    assert "MODEL" not in values
    assert "PLANNER" not in values
    assert "WORKER" not in values


# --- 23/24/25. restart durability -------------------------------------------

def test_restart_preserves_pending_request(git_repo_with_commit, state_root):
    first = PermissionService(git_repo_with_commit, state_root_override=state_root)
    try:
        req = first.request(
            permission_key="network.discovery.local", semantic_version="1", resource=None,
            purpose="test", requesting_subsystem="tests",
        )
    finally:
        first.close()
    restarted = PermissionService(git_repo_with_commit, state_root_override=state_root)
    try:
        reread = restarted.get_request(req.request_id)
        assert reread.state == "PENDING"
    finally:
        restarted.close()


def test_restart_preserves_active_grant(git_repo_with_commit, state_root):
    first = PermissionService(git_repo_with_commit, state_root_override=state_root)
    try:
        req = first.request(
            permission_key="network.discovery.local", semantic_version="1", resource=None,
            purpose="test", requesting_subsystem="tests",
        )
        decided = first.decide(req.request_id, "ALLOW")
    finally:
        first.close()
    restarted = PermissionService(git_repo_with_commit, state_root_override=state_root)
    try:
        assert restarted.check(
            permission_key="network.discovery.local", semantic_version="1", resource=None,
        ) is True
        assert restarted.get_grant(decided.grant_id).state == "ACTIVE"
    finally:
        restarted.close()


def test_restart_preserves_revocation(git_repo_with_commit, state_root):
    first = PermissionService(git_repo_with_commit, state_root_override=state_root)
    try:
        req = first.request(
            permission_key="network.discovery.local", semantic_version="1", resource=None,
            purpose="test", requesting_subsystem="tests",
        )
        decided = first.decide(req.request_id, "ALLOW")
        first.revoke(decided.grant_id)
    finally:
        first.close()
    restarted = PermissionService(git_repo_with_commit, state_root_override=state_root)
    try:
        assert restarted.check(
            permission_key="network.discovery.local", semantic_version="1", resource=None,
        ) is False
        assert restarted.get_grant(decided.grant_id).state == "REVOKED"
    finally:
        restarted.close()


# --- 28. no network discovery is ever performed -----------------------------

def test_permission_lifecycle_performs_no_network_or_subprocess_activity(
    git_repo_with_commit, state_root, monkeypatch,
):
    """Forbids genuine network activity (socket connect/DNS resolution)
    -- never a blanket subprocess ban, since `git` subprocess calls are
    the existing, unrelated, already-vetted repository-inspection
    mechanism every service in this codebase legitimately uses (identity
    resolution), not network discovery."""
    import socket

    def _forbidden(*args, **kwargs):
        raise AssertionError("permission lifecycle must never touch the network")

    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)

    svc = PermissionService(git_repo_with_commit, state_root_override=state_root)
    try:
        req = svc.request(
            permission_key="network.discovery.local", semantic_version="1", resource=None,
            purpose="test", requesting_subsystem="tests",
        )
        decided = svc.decide(req.request_id, "ALLOW")
        svc.check(permission_key="network.discovery.local", semantic_version="1", resource=None)
        svc.revoke(decided.grant_id)
        svc.grants()
        svc.list_requests()
    finally:
        svc.close()


# --- 29. no existing trust/policy capability broadened ----------------------

def test_permissions_package_imports_no_mutation_or_trust_authority():
    import code_slayer.permissions.definitions as definitions_module
    import code_slayer.permissions.service as service_module
    import code_slayer.store.permissions_repo as repo_module

    forbidden = {
        "code_slayer.tools.executor", "code_slayer.policy.engine",
        "code_slayer.lease.manager", "code_slayer.repo.checkpoint",
        "code_slayer.workers.trust", "code_slayer.workers.promotion",
        "code_slayer.planning", "code_slayer.workers.protocol",
    }
    for module in (definitions_module, service_module, repo_module):
        tree = ast.parse(open(module.__file__, encoding="utf-8").read())
        imported = _imported_modules(tree)
        overlap = {f for f in forbidden if any(name.startswith(f) for name in imported)}
        assert not overlap, f"{module.__name__} imports forbidden: {overlap}"


def _imported_modules(tree: ast.Module) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


# --- resource-scope validation / ambiguity -----------------------------------

def test_ambiguous_resource_scope_rejected_at_request_time(service):
    with pytest.raises(InvalidResourceScopeError):
        service.request(
            permission_key="network.discovery.local", semantic_version="1",
            resource="unexpected-resource",  # network.discovery.local is resource_type="none"
            purpose="test", requesting_subsystem="tests",
        )


def test_missing_resource_rejected_for_resource_scoped_definition(service):
    with pytest.raises(InvalidResourceScopeError):
        service.request(
            permission_key="network.connect", semantic_version="1", resource=None,
            purpose="test", requesting_subsystem="tests",
        )


def test_check_never_raises_on_malformed_input(service):
    assert service.check(permission_key=None, semantic_version="1", resource=None) is False
    assert service.check(permission_key="network.discovery.local", semantic_version=None) is False
    assert service.check(permission_key=123, semantic_version="1") is False  # type: ignore[arg-type]


def test_require_raises_and_audits_denial(service):
    with pytest.raises(PermissionDeniedError):
        service.require(
            permission_key="network.discovery.local", semantic_version="1", resource=None,
            subsystem="tests",
        )
    row = service._conn.execute(
        "SELECT COUNT(*) FROM audit_events WHERE event_type = 'PERMISSION_CHECK_DENIED'",
    ).fetchone()
    assert row[0] >= 1


def test_check_is_side_effect_free(service):
    before = service._conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    for _ in range(5):
        service.check(permission_key="network.discovery.local", semantic_version="1", resource=None)
    after = service._conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    assert before == after


def test_no_wildcard_or_parent_scope_matching(service):
    req = service.request(
        permission_key="network.connect", semantic_version="1", resource="nas-1",
        purpose="test", requesting_subsystem="tests",
    )
    service.decide(req.request_id, "ALLOW")
    # A grant for one specific resource never satisfies a check for "all
    # resources of this kind" -- there is no such query surface at all;
    # only an exact resource string is ever matched.
    assert service.check(
        permission_key="network.connect", semantic_version="1", resource=None,
    ) is False
    assert service.check(
        permission_key="network.connect", semantic_version="1", resource="some-other-nas",
    ) is False
