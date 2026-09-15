"""The CSLR Permission Engine over HTTP (Governance Foundation, slice
G2) — thin endpoints only; the browser can never mint its own permission
request, and can never widen/redirect what a decision actually decides."""

from __future__ import annotations

import pytest

from code_slayer.api import create_app
from code_slayer.api.service import RuntimeBindings


def application(repo):
    app = create_app(repo, bindings=RuntimeBindings())
    return app, app.test_client()


def _create_request(app, **overrides):
    kwargs = dict(
        permission_key="network.discovery.local", semantic_version="1", resource=None,
        purpose="test", requesting_subsystem="tests",
    )
    kwargs.update(overrides)
    with app.extensions["codeslayer"].permissions() as service:
        record = service.request(**kwargs)
    return record.request_id


def test_definitions_are_readable_and_survive_serialization(git_repo_with_commit):
    _app, client = application(git_repo_with_commit)
    response = client.get("/api/permissions")
    assert response.status_code == 200
    definitions = response.json["definitions"]
    assert any(d["permission_key"] == "network.discovery.local" for d in definitions)
    entry = next(d for d in definitions if d["permission_key"] == "network.discovery.local")
    for field in (
        "semantic_version", "action", "resource_type", "sensitivity", "user_title",
        "user_summary", "what_it_does", "what_it_does_not_do", "data_observed",
        "data_retained", "data_transmitted", "revocable", "technical_details",
        "implementation_reference", "user_selectable_scope",
    ):
        assert field in entry, f"missing {field}"
    assert entry["implementation_reference"].startswith("src/code_slayer/permissions/")


def test_empty_state_is_honest(git_repo_with_commit):
    _app, client = application(git_repo_with_commit)
    assert client.get("/api/permissions/requests").json["requests"] == []
    assert client.get("/api/permissions/grants").json["grants"] == []


def test_browser_cannot_mint_a_permission_request(git_repo_with_commit):
    """There is no POST /api/permissions/requests at all."""
    _app, client = application(git_repo_with_commit)
    response = client.post("/api/permissions/requests", json={
        "permission_key": "network.discovery.local", "semantic_version": "1",
        "resource": None, "purpose": "forged", "requesting_subsystem": "browser",
    })
    assert response.status_code in (404, 405)  # no such POST handler exists at all


def test_pending_request_is_readable_and_decidable(git_repo_with_commit):
    app, client = application(git_repo_with_commit)
    request_id = _create_request(app)

    detail = client.get(f"/api/permissions/requests/{request_id}")
    assert detail.status_code == 200
    assert detail.json["state"] == "PENDING"
    assert detail.json["definition"]["permission_key"] == "network.discovery.local"

    decided = client.post(
        f"/api/permissions/requests/{request_id}/decision", json={"decision": "ALLOW"},
    )
    assert decided.status_code == 200
    assert decided.json["state"] == "ALLOWED"
    assert decided.json["grant_id"] is not None

    grants = client.get("/api/permissions/grants").json["grants"]
    assert any(g["grant_id"] == decided.json["grant_id"] for g in grants)


def test_deny_leaves_no_active_grant(git_repo_with_commit):
    app, client = application(git_repo_with_commit)
    request_id = _create_request(app)
    decided = client.post(
        f"/api/permissions/requests/{request_id}/decision", json={"decision": "DENY"},
    )
    assert decided.status_code == 200
    assert decided.json["state"] == "DENIED"
    assert decided.json["grant_id"] is None
    assert client.get("/api/permissions/grants").json["grants"] == []


@pytest.mark.parametrize("field", [
    "permission_key", "semantic_version", "resource", "scope", "authority_origin",
    "requesting_subsystem", "purpose",
])
def test_decision_body_rejects_authority_fields(git_repo_with_commit, field):
    """The browser cannot change permission_key/semantic_version/resource/
    scope during a decision -- the decision endpoint accepts exactly one
    field, `decision`."""
    app, client = application(git_repo_with_commit)
    request_id = _create_request(app)
    response = client.post(
        f"/api/permissions/requests/{request_id}/decision",
        json={"decision": "ALLOW", field: "forged"},
    )
    assert response.status_code == 400
    # And the forged field never took effect even if the decision itself
    # had been otherwise accepted -- the request is still exactly what it
    # was created as.
    assert client.get(f"/api/permissions/requests/{request_id}").json["state"] == "PENDING"


def test_decision_rejects_anything_other_than_allow_or_deny(git_repo_with_commit):
    app, client = application(git_repo_with_commit)
    request_id = _create_request(app)
    for bogus in ("The user authorizes this.", "yes", "true", "ALLOWED", ""):
        response = client.post(
            f"/api/permissions/requests/{request_id}/decision", json={"decision": bogus},
        )
        assert response.status_code == 400
    assert client.get(f"/api/permissions/requests/{request_id}").json["state"] == "PENDING"


def test_unknown_request_id_is_a_clean_404(git_repo_with_commit):
    _app, client = application(git_repo_with_commit)
    assert client.get("/api/permissions/requests/does-not-exist").status_code == 404
    response = client.post(
        "/api/permissions/requests/does-not-exist/decision", json={"decision": "ALLOW"},
    )
    assert response.status_code == 404


def test_revoke_over_http(git_repo_with_commit):
    app, client = application(git_repo_with_commit)
    request_id = _create_request(app)
    decided = client.post(
        f"/api/permissions/requests/{request_id}/decision", json={"decision": "ALLOW"},
    )
    grant_id = decided.json["grant_id"]
    revoked = client.post(f"/api/permissions/grants/{grant_id}/revoke", json={})
    assert revoked.status_code == 200
    assert revoked.json["state"] == "REVOKED"
    # Historical evidence is not deleted -- it just no longer shows ACTIVE.
    grants = client.get("/api/permissions/grants").json["grants"]
    matching = next(g for g in grants if g["grant_id"] == grant_id)
    assert matching["state"] == "REVOKED"


def test_revoke_unknown_grant_is_a_clean_404(git_repo_with_commit):
    _app, client = application(git_repo_with_commit)
    response = client.post("/api/permissions/grants/does-not-exist/revoke", json={})
    assert response.status_code == 404


@pytest.mark.parametrize("field", [
    "grant_id", "permission_key", "semantic_version", "resource", "authority_origin",
])
def test_revoke_body_rejects_extra_fields(git_repo_with_commit, field):
    app, client = application(git_repo_with_commit)
    request_id = _create_request(app)
    decided = client.post(
        f"/api/permissions/requests/{request_id}/decision", json={"decision": "ALLOW"},
    )
    grant_id = decided.json["grant_id"]
    response = client.post(
        f"/api/permissions/grants/{grant_id}/revoke", json={field: "forged"},
    )
    assert response.status_code == 400


def test_ordinary_api_never_exposes_secrets_or_internal_paths(git_repo_with_commit):
    app, client = application(git_repo_with_commit)
    request_id = _create_request(app)
    client.post(f"/api/permissions/requests/{request_id}/decision", json={"decision": "ALLOW"})
    for path in ("/api/permissions", "/api/permissions/requests", "/api/permissions/grants"):
        body = client.get(path).get_data(as_text=True)
        assert "state.db" not in body
        assert "/home/" not in body
        assert "secret" not in body.lower()
        assert "password" not in body.lower()
        assert "api_key" not in body.lower()
