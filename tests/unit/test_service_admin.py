"""Service, persistent config, runtime identity, Tailscale, and update admin."""

from __future__ import annotations

import ast
import http.server
import inspect
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from code_slayer.admin.hosts import exact_static_hosts, origin_allowed
from code_slayer.admin.process import ProcessError, ProcessResult, inspect_checkout
from code_slayer.admin.service import install_service, service_status
from code_slayer.admin.systemd import render_user_unit
from code_slayer.admin.tailscale import (
    TailscaleView,
    disable_serve,
    enable_serve,
    exact_desired_live_serve,
    intent_alignment,
    plan_disable,
    plan_enable,
)
from code_slayer.admin.tailscale import status as tailscale_status
from code_slayer.admin.updates import apply_update, check_for_update
from code_slayer.api import create_app
from code_slayer.cli.main import cli
from code_slayer.config.schema import (
    ConfigError,
    CSLRConfig,
    OllamaServerConfig,
    ServerConfig,
    TailscaleConfig,
    WorkerRuntimeConfig,
)
from code_slayer.config.store import load_config, save_config
from code_slayer.workers.security_baseline import runtime_profile_identity_from_config


class _Script:
    def __init__(self) -> None:
        self.version = "0.16.1"
        self.models = [{"name": "demo-model:1", "digest": "sha256:abc"}]


def _handler(script: _Script):
    class Handler(http.server.BaseHTTPRequestHandler):
        def _write(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/api/version":
                self._write(200, json.dumps({"version": script.version}).encode())
                return
            if path == "/api/tags":
                self._write(200, json.dumps({"models": script.models}).encode())
                return
            self._write(404, b"{}")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    return Handler


@pytest.fixture
def runtime_server():
    script = _Script()
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _handler(script))
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True,
    )
    thread.start()
    try:
        yield script, f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def admin_app(git_repo_with_commit, tmp_path):
    config = tmp_path / "config.toml"
    application = create_app(
        git_repo_with_commit,
        config_path=config,
        load_persistent_config=True,
    )
    client = application.test_client()
    yield client, application, config, git_repo_with_commit
    application.extensions["codeslayer"].close()


def _ok(argv, stdout=""):
    return ProcessResult(tuple(argv), 0, stdout, "")


def test_server_host_must_be_loopback():
    with pytest.raises(ConfigError, match="loopback"):
        ServerConfig(host="0.0.0.0")
    with pytest.raises(ConfigError, match="loopback"):
        ServerConfig.from_mapping({"host": "0.0.0.0", "port": 8765})
    assert ServerConfig().host == "127.0.0.1"


def test_unknown_config_keys_fail_closed(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[server]\nhost = \"127.0.0.1\"\nextra = true\n")
    with pytest.raises(ConfigError, match="unsupported"):
        load_config(path=path)


def test_digest_and_version_must_both_be_set_or_both_empty():
    with pytest.raises(ConfigError, match="approved_model_digest"):
        WorkerRuntimeConfig(
            worker_id="w1",
            kind="openai_compatible",
            network_class="local",
            ollama_server_id="local",
            model_tag="demo:1",
            approved_model_digest="sha256:abc",
            approved_runtime_version=None,
            effective_context_tokens=4096,
            temperature=0.0,
            normalizer_id=None,
            normalizer_version=None,
        )


def test_ollama_origin_must_be_http_origin_only():
    with pytest.raises(ConfigError):
        OllamaServerConfig(server_id="x", origin="file:///tmp")
    with pytest.raises(ConfigError):
        OllamaServerConfig(server_id="x", origin="http://127.0.0.1:11434/v1")
    OllamaServerConfig(server_id="x", origin="http://127.0.0.1:11434")


def test_config_round_trip_does_not_store_fingerprint(tmp_path):
    path = tmp_path / "config.toml"
    cfg = CSLRConfig(
        ollama_servers=(OllamaServerConfig(server_id="local", origin="http://127.0.0.1:11434"),),
        workers=(
            WorkerRuntimeConfig(
                worker_id="w1",
                kind="openai_compatible",
                network_class="local",
                ollama_server_id="local",
                model_tag="demo:1",
                approved_model_digest="sha256:abc",
                approved_runtime_version="0.16.1",
                effective_context_tokens=4096,
                temperature=0.0,
                normalizer_id=None,
                normalizer_version=None,
            ),
        ),
    )
    save_config(cfg, path=path)
    text = path.read_text()
    assert "fingerprint" not in text
    loaded = load_config(path=path)
    assert loaded.worker_by_id("w1").approved_model_digest == "sha256:abc"
    identity = runtime_profile_identity_from_config(
        model_tag="demo:1",
        model_digest="sha256:abc",
        endpoint="http://127.0.0.1:11434/v1",
        runtime_version="0.16.1",
        effective_context_tokens=4096,
        temperature=0.0,
        normalizer_id=None,
        normalizer_version=None,
    )
    assert len(identity.runtime_identity_fingerprint) == 64


def test_systemd_unit_is_loopback_and_has_no_cert_env():
    unit = render_user_unit(
        python_or_codeslayer=Path("/tmp/venv/bin/codeslayer"),
        checkout=Path("/tmp/code-slayer"),
        webui_dir=Path("/tmp/code-slayer/webui"),
    )
    assert "Restart=on-failure" in unit
    assert "0.0.0.0" not in unit
    assert all(
        "CODESLAYER_CERT" not in line or line.lstrip().startswith("#")
        for line in unit.splitlines()
    )
    assert "--host" not in unit
    assert "serve" in unit
    assert _working_directory_line(unit) == "WorkingDirectory=/tmp/code-slayer"
    assert 'WorkingDirectory="' not in unit
    assert 'ExecStart="/tmp/venv/bin/codeslayer" serve --repo "/tmp/code-slayer"' in unit


def _working_directory_line(unit: str) -> str:
    for line in unit.splitlines():
        if line.startswith("WorkingDirectory="):
            return line
    raise AssertionError("generated unit is missing WorkingDirectory")


def test_working_directory_with_spaces_is_unquoted_absolute():
    checkout = Path("/tmp/Code Slayer/code-slayer")
    webui = checkout / "webui"
    unit = render_user_unit(
        python_or_codeslayer=Path("/tmp/venv/bin/codeslayer"),
        checkout=checkout,
        webui_dir=webui,
    )
    assert _working_directory_line(unit) == (
        "WorkingDirectory=/tmp/Code Slayer/code-slayer"
    )
    assert 'WorkingDirectory="' not in unit
    assert "WorkingDirectory=\"/tmp/Code Slayer/code-slayer\"" not in unit
    assert '--repo "/tmp/Code Slayer/code-slayer"' in unit
    assert '--webui-dir "/tmp/Code Slayer/code-slayer/webui"' in unit
    assert "Restart=on-failure" in unit
    assert "0.0.0.0" not in unit


def test_working_directory_escapes_systemd_specifiers_without_quoting():
    checkout = Path("/tmp/100%ready/code-slayer")
    unit = render_user_unit(
        python_or_codeslayer=Path("/tmp/venv/bin/codeslayer"),
        checkout=checkout,
        webui_dir=checkout / "webui",
    )
    assert _working_directory_line(unit) == (
        "WorkingDirectory=/tmp/100%%ready/code-slayer"
    )
    assert 'WorkingDirectory="' not in unit
    assert '--repo "/tmp/100%%ready/code-slayer"' in unit


def test_generated_unit_passes_systemd_analyze_when_available(tmp_path):
    analyze = shutil.which("systemd-analyze")
    if analyze is None:
        pytest.skip("systemd-analyze is not available")
    checkout = tmp_path / "Code Slayer" / "code-slayer"
    webui = checkout / "webui"
    (webui / "static").mkdir(parents=True)
    (webui / "index.html").write_text("<html></html>")
    exe = tmp_path / "venv" / "bin" / "codeslayer"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    unit_text = render_user_unit(
        python_or_codeslayer=exe,
        checkout=checkout,
        webui_dir=webui,
    )
    assert 'WorkingDirectory="' not in unit_text
    assert _working_directory_line(unit_text) == f"WorkingDirectory={checkout}"
    unit_file = tmp_path / "codeslayer.service"
    unit_file.write_text(unit_text, encoding="utf-8")
    result = subprocess.run(
        [analyze, "verify", str(unit_file)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    assert "path is not absolute" not in combined
    assert result.returncode == 0, combined


def test_install_service_preserves_state_and_writes_unit(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname='code-slayer'\n")
    webui = checkout / "webui"
    (webui / "static").mkdir(parents=True)
    (webui / "index.html").write_text("<html></html>")
    (webui / "static" / "app.js").write_text("")
    state_root = tmp_path / "state"
    preexisting = state_root / "repos" / "r" / "worktrees" / "w" / "state.db"
    preexisting.parent.mkdir(parents=True)
    preexisting.write_text("keep-me")
    config_path = tmp_path / "config.toml"
    calls = []

    def fake_venv(path, with_pip=True, clear=False):
        bin_dir = Path(path) / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "python").write_text("#!/bin/sh\n")
        (bin_dir / "codeslayer").write_text("#!/bin/sh\n")

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _ok(argv, "active\n")

    result = install_service(
        checkout=checkout,
        config_path=config_path,
        state_root=state_root,
        runner=fake_run,
        venv_create=fake_venv,
    )
    assert preexisting.read_text() == "keep-me"
    unit = Path(result["unit_path"]).read_text()
    assert "Restart=on-failure" in unit
    assert all(
        "CODESLAYER_CERT" not in line or line.lstrip().startswith("#")
        for line in unit.splitlines()
    )
    assert "0.0.0.0" not in unit
    assert any(argv[:3] == ("systemctl", "--user", "enable") for argv in calls)
    loaded = load_config(path=config_path)
    assert loaded.server.checkout == str(checkout.resolve())
    assert loaded.server.host == "127.0.0.1"
    assert result["linger"]["source"] in {"VERIFIED", "UNVERIFIED"}


def _install_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text("[project]\nname='code-slayer'\n")
    webui = checkout / "webui"
    (webui / "static").mkdir(parents=True)
    (webui / "index.html").write_text("<html></html>")
    (webui / "static" / "app.js").write_text("")
    return checkout


def _fake_venv(path, with_pip=True, clear=False):
    bin_dir = Path(path) / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/bin/sh\n")
    (bin_dir / "codeslayer").write_text("#!/bin/sh\n")


def test_install_service_fails_closed_on_daemon_reload(tmp_path):
    checkout = _install_checkout(tmp_path)

    def fake_run(argv, **kwargs):
        if argv[:3] == ("systemctl", "--user", "daemon-reload"):
            return ProcessResult(tuple(argv), 1, "", "reload failed")
        return _ok(argv)

    with pytest.raises(ProcessError) as caught:
        install_service(
            checkout=checkout,
            config_path=tmp_path / "config.toml",
            state_root=tmp_path / "state",
            runner=fake_run,
            venv_create=_fake_venv,
        )
    assert caught.value.code == "systemctl_daemon_reload_failed"


def test_install_service_fails_closed_on_enable(tmp_path):
    checkout = _install_checkout(tmp_path)

    def fake_run(argv, **kwargs):
        if argv[:3] == ("systemctl", "--user", "enable"):
            return ProcessResult(tuple(argv), 1, "", "enable failed")
        return _ok(argv)

    with pytest.raises(ProcessError) as caught:
        install_service(
            checkout=checkout,
            config_path=tmp_path / "config.toml",
            state_root=tmp_path / "state",
            runner=fake_run,
            venv_create=_fake_venv,
        )
    assert caught.value.code == "systemctl_enable_failed"


def test_cli_install_service_does_not_print_installed_on_reload_or_enable_failure(
    tmp_path, monkeypatch,
):
    checkout = _install_checkout(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("CODESLAYER_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr("code_slayer.admin.service.venv.create", _fake_venv)

    def fail_reload(argv, **kwargs):
        if argv[:3] == ("systemctl", "--user", "daemon-reload"):
            return ProcessResult(tuple(argv), 5, "", "reload failed")
        return _ok(argv)

    monkeypatch.setattr("code_slayer.admin.service.run_fixed", fail_reload)
    reload_result = CliRunner().invoke(cli, ["install-service", "--checkout", str(checkout)])
    assert reload_result.exit_code != 0
    assert "installed" not in reload_result.output.lower()
    assert "systemctl_daemon_reload_failed" in reload_result.output

    def fail_enable(argv, **kwargs):
        if argv[:3] == ("systemctl", "--user", "enable"):
            return ProcessResult(tuple(argv), 1, "", "enable failed")
        return _ok(argv)

    monkeypatch.setattr("code_slayer.admin.service.run_fixed", fail_enable)
    enable_result = CliRunner().invoke(cli, ["install-service", "--checkout", str(checkout)])
    assert enable_result.exit_code != 0
    assert "installed" not in enable_result.output.lower()
    assert "systemctl_enable_failed" in enable_result.output


def test_install_service_linger_failure_is_unverified_not_blocking(tmp_path):
    checkout = _install_checkout(tmp_path)

    def fake_run(argv, **kwargs):
        if argv and argv[0] == "loginctl":
            return ProcessResult(tuple(argv), 1, "", "linger denied")
        return _ok(argv)

    result = install_service(
        checkout=checkout,
        config_path=tmp_path / "config.toml",
        state_root=tmp_path / "state",
        runner=fake_run,
        venv_create=_fake_venv,
    )
    assert result["enable_start"]["ok"] is True
    assert result["linger"]["ok"] is False
    assert result["linger"]["source"] == "UNVERIFIED"
    assert result["linger"]["source"] != "VERIFIED"


def test_get_runtime_is_config_bound_not_a_live_probe(admin_app):
    client, _app, config, _repo = admin_app
    save_config(
        CSLRConfig(
            ollama_servers=(
                OllamaServerConfig(server_id="local", origin="http://127.0.0.1:1"),
            ),
        ),
        path=config,
    )
    response = client.get("/api/runtime")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ollama_servers"][0]["origin_source"] == "CONFIG_BOUND"
    assert "live" not in payload["ollama_servers"][0]


def test_add_test_register_approve_identity(admin_app, runtime_server):
    client, application, config, _repo = admin_app
    script, origin = runtime_server
    added = client.post("/api/runtime/ollama-servers", json={"id": "local", "origin": origin})
    assert added.status_code == 200
    tested = client.post("/api/runtime/ollama-servers/local/test", json={})
    assert tested.status_code == 200
    assert tested.get_json()["status"] == "LIVE_ATTESTED"
    assert tested.get_json()["runtime_version"] == "0.16.1"
    registered = client.post(
        "/api/runtime/workers",
        json={
            "worker_id": "w1",
            "ollama_server_id": "local",
            "model_tag": "demo-model:1",
            "effective_context_tokens": 4096,
            "temperature": 0.0,
        },
    )
    assert registered.status_code == 200
    worker = next(item for item in registered.get_json()["workers"] if item["worker_id"] == "w1")
    assert worker["approved_model_digest"]["value"] is None
    rejected = client.post(
        "/api/runtime/workers",
        json={
            "worker_id": "w1",
            "ollama_server_id": "local",
            "model_tag": "demo-model:1",
            "digest": "sha256:evil",
            "outcome": "PASS",
        },
    )
    assert rejected.status_code == 400
    assert rejected.get_json()["error"]["code"] == "invalid_fields"
    approved = client.post("/api/runtime/workers/w1/approve", json={})
    assert approved.status_code == 200
    body = approved.get_json()
    assert body["status"] == "VERIFIED"
    assert body["certificates_transferred"] is False
    assert body["configured_digest"] == "sha256:abc"
    loaded = load_config(path=config)
    assert loaded.worker_by_id("w1").approved_model_digest == "sha256:abc"
    workers = client.get("/api/certification/workers").get_json()["workers"]
    assert any(item["worker_id"] == "w1" for item in workers)


# -- H.3: archive/reactivate admin routes ------------------------------------


def _register_worker(client, worker_id="w1"):
    return client.post(
        "/api/runtime/workers",
        json={"worker_id": worker_id, "ollama_server_id": "local", "model_tag": "demo-model:1"},
    )


def test_archive_reactivate_round_trip_via_http(admin_app, runtime_server):
    client, _app, _config, _repo = admin_app
    _script, origin = runtime_server
    client.post("/api/runtime/ollama-servers", json={"id": "local", "origin": origin})
    _register_worker(client)

    before = client.get("/api/runtime").get_json()
    worker_before = next(w for w in before["workers"] if w["worker_id"] == "w1")
    assert worker_before["lifecycle_state"] == "ACTIVE"
    assert worker_before["lifecycle_changed_at"] is None
    assert worker_before["archive_available"] is True
    assert worker_before["reactivate_available"] is False

    archived = client.post("/api/runtime/workers/w1/archive", json={})
    assert archived.status_code == 200
    body = archived.get_json()
    assert body["lifecycle_state"] == "ARCHIVED"
    assert body["lifecycle_changed_at"] is not None
    assert body["archive_available"] is False
    assert body["reactivate_available"] is True

    after = client.get("/api/runtime").get_json()
    worker_after = next(w for w in after["workers"] if w["worker_id"] == "w1")
    assert worker_after["lifecycle_state"] == "ARCHIVED"
    # ARCHIVED workers remain listed, never hidden from /api/runtime.
    assert any(w["worker_id"] == "w1" for w in after["workers"])

    reactivated = client.post("/api/runtime/workers/w1/reactivate", json={})
    assert reactivated.status_code == 200
    assert reactivated.get_json()["lifecycle_state"] == "ACTIVE"
    assert reactivated.get_json()["archive_available"] is True


def test_archive_reactivate_idempotent_no_op_returns_200(admin_app, runtime_server):
    client, _app, _config, _repo = admin_app
    _script, origin = runtime_server
    client.post("/api/runtime/ollama-servers", json={"id": "local", "origin": origin})
    _register_worker(client)

    client.post("/api/runtime/workers/w1/archive", json={})
    second = client.post("/api/runtime/workers/w1/archive", json={})
    assert second.status_code == 200
    assert second.get_json()["lifecycle_state"] == "ARCHIVED"

    client.post("/api/runtime/workers/w1/reactivate", json={})
    second_reactivate = client.post("/api/runtime/workers/w1/reactivate", json={})
    assert second_reactivate.status_code == 200
    assert second_reactivate.get_json()["lifecycle_state"] == "ACTIVE"


def test_archive_unknown_worker_is_404(admin_app):
    client, _app, _config, _repo = admin_app
    response = client.post("/api/runtime/workers/ghost/archive", json={})
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


def test_reactivate_unknown_worker_is_404(admin_app):
    client, _app, _config, _repo = admin_app
    response = client.post("/api/runtime/workers/ghost/reactivate", json={})
    assert response.status_code == 404


@pytest.mark.parametrize("route", ["archive", "reactivate"])
def test_archive_reactivate_accept_only_an_empty_body(admin_app, runtime_server, route):
    client, _app, _config, _repo = admin_app
    _script, origin = runtime_server
    client.post("/api/runtime/ollama-servers", json={"id": "local", "origin": origin})
    _register_worker(client)

    for bad_field, value in (
        ("state", "ARCHIVED"),
        ("reason", "operator request"),
        ("force", True),
        ("certificate_id", "cert-1"),
        ("fingerprint", "abc"),
        ("digest", "sha256:abc"),
        ("eligibility", True),
        ("cancel_active_work", True),
    ):
        response = client.post(f"/api/runtime/workers/w1/{route}", json={bad_field: value})
        assert response.status_code == 400, bad_field
        assert response.get_json()["error"]["code"] == "invalid_fields"

    ok = client.post(f"/api/runtime/workers/w1/{route}", json={})
    assert ok.status_code == 200


def test_archive_refuses_while_a_certification_run_is_active(admin_app, runtime_server):
    """The cross-DB precondition: a QUEUED/RUNNING Certification Center
    run for this worker refuses the archive attempt before the
    canonical lifecycle transaction is even opened."""
    from code_slayer.repo import identity as repo_identity
    from code_slayer.security.certification_service import CertificationService
    from code_slayer.store.certification_runs_repo import CertificationRunsRepo
    from code_slayer.store.db import transaction, utcnow_iso
    from code_slayer.store.workers_repo import WorkersRepo

    client, _app, _config, repo = admin_app
    _script, origin = runtime_server
    client.post("/api/runtime/ollama-servers", json={"id": "local", "origin": origin})
    _register_worker(client)

    resolved = repo_identity.resolve(repo, create=False)
    service = CertificationService(resolved.repo_id, resolved.worktree_id)
    try:
        WorkersRepo(service.validation_conn()).register(
            worker_id="w1", kind="fake", network_class="local",
        )
        with transaction(service.validation_conn()):
            CertificationRunsRepo(service.validation_conn()).create_in_transaction(
                run_id="run-active", worker_id="w1", kind="baseline_security",
                environment="VALIDATION", state="QUEUED", created_at=utcnow_iso(),
            )
    finally:
        service.close()

    response = client.post("/api/runtime/workers/w1/archive", json={})
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "worker_has_active_work"

    still_active = client.get("/api/runtime").get_json()
    worker = next(w for w in still_active["workers"] if w["worker_id"] == "w1")
    assert worker["lifecycle_state"] == "ACTIVE"


def test_identity_approval_preserves_archived_lifecycle(admin_app, runtime_server):
    """H.3: approving/replacing a runtime identity is a config-bound
    mutation entirely separate from administrative lifecycle -- it must
    never implicitly reactivate an archived worker."""
    client, _app, _config, _repo = admin_app
    _script, origin = runtime_server
    client.post("/api/runtime/ollama-servers", json={"id": "local", "origin": origin})
    _register_worker(client)
    client.post("/api/runtime/workers/w1/approve", json={})

    archived = client.post("/api/runtime/workers/w1/archive", json={})
    assert archived.status_code == 200
    assert archived.get_json()["lifecycle_state"] == "ARCHIVED"

    replaced = client.post("/api/runtime/workers/w1/approve-new-identity", json={})
    assert replaced.status_code == 200

    still_archived = client.get("/api/runtime").get_json()
    worker = next(w for w in still_archived["workers"] if w["worker_id"] == "w1")
    assert worker["lifecycle_state"] == "ARCHIVED"


def test_mismatch_without_replace_does_not_overwrite(admin_app, runtime_server):
    client, _app, config, _repo = admin_app
    script, origin = runtime_server
    client.post("/api/runtime/ollama-servers", json={"id": "local", "origin": origin})
    client.post(
        "/api/runtime/workers",
        json={"worker_id": "w1", "ollama_server_id": "local", "model_tag": "demo-model:1"},
    )
    first = client.post("/api/runtime/workers/w1/approve", json={}).get_json()
    assert first["status"] == "VERIFIED"
    script.models = [{"name": "demo-model:1", "digest": "sha256:other"}]
    mismatch = client.post("/api/runtime/workers/w1/approve", json={}).get_json()
    assert mismatch["status"] == "MISMATCH"
    assert mismatch["replaced"] is False
    loaded = load_config(path=config)
    assert loaded.worker_by_id("w1").approved_model_digest == "sha256:abc"
    replaced = client.post("/api/runtime/workers/w1/approve-new-identity", json={}).get_json()
    assert replaced["status"] == "VERIFIED"
    assert replaced["certificates_transferred"] is False
    loaded = load_config(path=config)
    assert loaded.worker_by_id("w1").approved_model_digest == "sha256:other"
    old = runtime_profile_identity_from_config(
        model_tag="demo-model:1",
        model_digest="sha256:abc",
        endpoint=origin.rstrip("/") + "/v1",
        runtime_version="0.16.1",
        effective_context_tokens=16384,
        temperature=0.0,
        normalizer_id=None,
        normalizer_version=None,
    )
    new = runtime_profile_identity_from_config(
        model_tag="demo-model:1",
        model_digest="sha256:other",
        endpoint=origin.rstrip("/") + "/v1",
        runtime_version="0.16.1",
        effective_context_tokens=16384,
        temperature=0.0,
        normalizer_id=None,
        normalizer_version=None,
    )
    assert old.runtime_identity_fingerprint != new.runtime_identity_fingerprint


def test_unreachable_origin_is_not_saved(admin_app):
    client, _app, config, _repo = admin_app
    response = client.post(
        "/api/runtime/ollama-servers",
        json={"id": "local", "origin": "http://127.0.0.1:1"},
    )
    assert response.status_code == 409
    assert load_config(path=config).ollama_servers == ()


def test_system_status_and_forbidden_authority_fields(admin_app):
    client, _app, _config, _repo = admin_app
    status = client.get("/api/system").get_json()
    assert status["network"]["bind_host"] == "127.0.0.1"
    assert status["service"]["process_commit_source"] == "VERIFIED"
    assert status["service"]["running_commit_source"] == "VERIFIED"
    assert status["service"]["process_commit"] == status["service"]["running_commit"]
    assert status["service"]["process_source_dirty"] is False
    assert status["service"]["process_source_state"] == "clean"
    assert status["service"]["checkout_source_dirty"] is False
    assert status["service"]["deployment_complete"] is True
    for path in (
        "/api/system/restart",
        "/api/system/update/check",
        "/api/runtime/attest",
        "/api/tailscale/enable",
    ):
        response = client.post(path, json={"cmd": "rm -rf /"})
        assert response.status_code == 400


def test_process_commit_does_not_follow_checkout_head(admin_app):
    client, _app, _config, repo = admin_app
    first = client.get("/api/system").get_json()["service"]
    process = first["process_commit"]
    assert process
    assert first["process_commit_source"] == "VERIFIED"
    assert first["running_commit"] == process
    assert first["running_commit_source"] == "VERIFIED"
    assert first["process_source_dirty"] is False
    assert first["process_source_state"] == "clean"
    assert first["checkout_head"] == process
    assert first["checkout_head_source"] == "OBSERVED"
    assert first["checkout_source_dirty"] is False
    assert first["deployment_status"] == "VERIFIED"
    assert first["deployment_complete"] is True
    (repo / "after-start.txt").write_text("new\n")
    subprocess.run(["git", "add", "after-start.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "after start"], cwd=repo, check=True, capture_output=True,
    )
    second = client.get("/api/system").get_json()["service"]
    assert second["process_commit"] == process
    assert second["running_commit"] == process
    assert second["checkout_head"] != process
    assert len(second["checkout_head"]) == 40
    assert second["checkout_head_source"] == "OBSERVED"
    assert second["deployment_complete"] is False
    assert second["deployment_status"] == "MISMATCH"
    assert second["process_commit_source"] == "VERIFIED"
    assert second["process_source_dirty"] is False


def _opened_admin(repo: Path, tmp_path: Path):
    return create_app(
        repo,
        config_path=tmp_path / "config.toml",
        load_persistent_config=True,
    )


def _system_service(application):
    return application.test_client().get("/api/system").get_json()["service"]


def test_inspect_checkout_status_failure_is_unverified_even_with_head():
    def runner(argv, **kwargs):
        if "rev-parse" in argv:
            return _ok(argv, "a" * 40)
        if "status" in argv:
            return ProcessResult(tuple(argv), 128, "", "not a git repository")
        return _ok(argv)

    identity = inspect_checkout(Path("/tmp"), runner=runner)
    assert identity.commit == "a" * 40
    assert identity.dirty is None
    assert identity.commit_source == "UNVERIFIED"
    assert identity.commit_source != "VERIFIED"
    assert identity.state == "unverified"


def test_inspect_checkout_untracked_and_tracked_porcelain_is_dirty():
    sha = "b" * 40

    def tracked(argv, **kwargs):
        if "rev-parse" in argv:
            return _ok(argv, sha)
        if "status" in argv:
            return _ok(argv, " M README.md\n")
        return _ok(argv)

    dirty_tracked = inspect_checkout(Path("/tmp"), runner=tracked)
    assert dirty_tracked.commit == sha
    assert dirty_tracked.dirty is True
    assert dirty_tracked.commit_source == "DIRTY"
    assert dirty_tracked.commit_source != "VERIFIED"

    def untracked(argv, **kwargs):
        if "rev-parse" in argv:
            return _ok(argv, sha)
        if "status" in argv:
            return _ok(argv, "?? extra_module.py\n")
        return _ok(argv)

    dirty_untracked = inspect_checkout(Path("/tmp"), runner=untracked)
    assert dirty_untracked.commit == sha
    assert dirty_untracked.dirty is True
    assert dirty_untracked.commit_source != "VERIFIED"
    assert dirty_untracked.commit_source == "DIRTY"


def test_dirty_tracked_file_at_startup_is_not_verified(git_repo_with_commit, tmp_path):
    (git_repo_with_commit / "README.md").write_text("changed tracked source\n")
    application = _opened_admin(git_repo_with_commit, tmp_path)
    try:
        service = _system_service(application)
        assert service["process_commit"]
        assert len(service["process_commit"]) == 40
        assert service["process_commit"] == service["checkout_head"]
        assert service["process_commit_source"] != "VERIFIED"
        assert service["process_commit_source"] == "DIRTY"
        assert service["running_commit_source"] == "DIRTY"
        assert service["process_source_dirty"] is True
        assert service["process_source_state"] == "dirty"
        assert service["checkout_source_dirty"] is True
        assert service["deployment_complete"] is False
        assert service["deployment_status"] == "DIRTY"
    finally:
        application.extensions["codeslayer"].close()


def test_untracked_source_file_at_startup_is_not_verified(git_repo_with_commit, tmp_path):
    (git_repo_with_commit / "extra_module.py").write_text("VALUE = 1\n")
    application = _opened_admin(git_repo_with_commit, tmp_path)
    try:
        service = _system_service(application)
        assert service["process_commit"]
        assert service["process_commit"] == service["checkout_head"]
        assert service["process_commit_source"] != "VERIFIED"
        assert service["process_commit_source"] == "DIRTY"
        assert service["process_source_dirty"] is True
        assert service["process_source_state"] == "dirty"
        assert service["checkout_source_dirty"] is True
        assert service["deployment_complete"] is False
        assert service["deployment_status"] == "DIRTY"
    finally:
        application.extensions["codeslayer"].close()


def test_dirty_after_startup_does_not_rewrite_process_source(admin_app):
    client, _application, _config, repo = admin_app
    first = client.get("/api/system").get_json()["service"]
    assert first["process_commit_source"] == "VERIFIED"
    assert first["process_source_dirty"] is False
    assert first["process_source_state"] == "clean"
    assert first["checkout_source_dirty"] is False
    assert first["deployment_complete"] is True
    process = first["process_commit"]
    (repo / "README.md").write_text("changed after process start\n")
    second = client.get("/api/system").get_json()["service"]
    assert second["process_commit"] == process
    assert second["running_commit"] == process
    assert second["process_commit_source"] == "VERIFIED"
    assert second["process_source_dirty"] is False
    assert second["process_source_state"] == "clean"
    assert second["checkout_head"] == process
    assert second["checkout_head_source"] == "OBSERVED"
    assert second["checkout_source_dirty"] is True
    assert second["checkout_source_state"] == "dirty"
    assert second["deployment_complete"] is False
    assert second["deployment_status"] == "DIRTY"


def test_deployment_complete_requires_verified_clean_process_source(
    git_repo_with_commit, tmp_path,
):
    readme = git_repo_with_commit / "README.md"
    original = readme.read_text()
    readme.write_text("dirty at process start\n")
    application = _opened_admin(git_repo_with_commit, tmp_path)
    try:
        client = application.test_client()
        dirty = client.get("/api/system").get_json()["service"]
        assert dirty["process_commit"] == dirty["checkout_head"]
        assert dirty["process_commit_source"] != "VERIFIED"
        assert dirty["deployment_complete"] is False
        assert dirty["deployment_status"] == "DIRTY"
        readme.write_text(original)
        cleaned = client.get("/api/system").get_json()["service"]
        assert cleaned["process_commit"] == cleaned["checkout_head"]
        assert cleaned["checkout_source_dirty"] is False
        assert cleaned["checkout_source_state"] == "clean"
        assert cleaned["process_commit_source"] != "VERIFIED"
        assert cleaned["process_source_dirty"] is True
        assert cleaned["process_source_state"] == "dirty"
        assert cleaned["deployment_complete"] is False
        assert cleaned["deployment_status"] == "DIRTY"
    finally:
        application.extensions["codeslayer"].close()


def test_tailscale_never_funnel_and_loopback_only():
    source = Path("src/code_slayer/admin/tailscale.py").read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "funnel":
            pytest.fail("tailscale funnel must never appear as an argv token")
    captured = []

    def runner(argv, **kwargs):
        captured.append(argv)
        return _ok(argv)

    enable_serve(runner=runner, backend_host="127.0.0.1", backend_port=8765)
    assert captured[0] == ("tailscale", "serve", "--bg", "http://127.0.0.1:8765")
    assert captured[0][0] == "tailscale"
    assert captured[0][1] == "serve"
    assert "--bg" in captured[0]
    assert "--yes" not in captured[0]
    assert "sudo" not in captured[0]
    assert "funnel" not in captured[0]
    assert "0.0.0.0" not in captured[0]
    assert "100.64.1.2" not in captured[0]
    disable_serve(runner=runner)
    assert captured[1] == ("tailscale", "serve", "reset")
    assert "--yes" not in captured[1]
    assert "sudo" not in captured[1]
    process_src = Path("src/code_slayer/admin/process.py").read_text()
    tree = ast.parse(process_src)
    shells = [
        kw.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in getattr(node, "keywords", ())
        if getattr(kw, "arg", None) == "shell" and isinstance(kw.value, ast.Constant)
    ]
    assert shells == [False]
    assert "sudo" not in process_src
    for rel in (
        "src/code_slayer/admin/tailscale.py",
        "src/code_slayer/api/admin.py",
        "src/code_slayer/admin/process.py",
    ):
        module = ast.parse(Path(rel).read_text())
        for node in ast.walk(module):
            if isinstance(node, ast.Attribute) and node.attr == "Popen":
                pytest.fail(f"{rel} must not use subprocess.Popen")
            if isinstance(node, ast.Constant) and node.value == "sudo":
                pytest.fail(f"{rel} must not mention sudo")
            if isinstance(node, ast.Constant) and node.value == "0.0.0.0":
                pytest.fail(f"{rel} must not bind 0.0.0.0")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("100.") and node.value[4:5].isdigit():
                    pytest.fail(f"{rel} must not bind a Tailscale 100.x address")
    with pytest.raises(ProcessError, match="loopback"):
        enable_serve(runner=runner, backend_host="0.0.0.0", backend_port=8765)
    with pytest.raises(ProcessError, match="loopback"):
        enable_serve(runner=runner, backend_host="100.64.1.2", backend_port=8765)


def _tailscale_runner(node_doc, serve_doc, *, serve_code=0, serve_raw=None):
    def runner(argv, **kwargs):
        if argv[:3] == ("tailscale", "status", "--json"):
            return _ok(argv, json.dumps(node_doc))
        if argv[:4] == ("tailscale", "serve", "status", "--json"):
            body = serve_raw if serve_raw is not None else json.dumps(serve_doc)
            return ProcessResult(tuple(argv), serve_code, body, "")
        return _ok(argv)
    return runner


def test_tailscale_connected_without_serve_is_not_remote_verified():
    view = tailscale_status(
        runner=_tailscale_runner(
            {"BackendState": "Running", "Self": {"DNSName": "node.ts.net."}},
            {},
        ),
        backend_host="127.0.0.1",
        backend_port=8765,
    )
    assert view.node_state == "Connected"
    assert view.serve_status == "not_configured"
    assert view.remote_access != "VERIFIED"
    assert view.remote_access == "UNVERIFIED"


def test_tailscale_wrong_serve_backend_is_mismatch():
    view = tailscale_status(
        runner=_tailscale_runner(
            {"BackendState": "Running", "Self": {"DNSName": "node.ts.net."}},
            {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {
                    "node.ts.net:443": {
                        "Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}},
                    },
                },
            },
        ),
    )
    assert view.serve_status == "MISMATCH"
    assert view.remote_access == "MISMATCH"
    assert view.observed_backend == "http://127.0.0.1:9999"


def test_tailscale_correct_serve_backend_is_verified():
    view = tailscale_status(
        runner=_tailscale_runner(
            {"BackendState": "Running", "Self": {"DNSName": "node.ts.net."}},
            {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {
                    "node.ts.net:443": {
                        "Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}},
                    },
                },
            },
        ),
    )
    assert view.serve_status == "VERIFIED"
    assert view.remote_access == "VERIFIED"
    assert view.node_state == "Connected"
    assert view.funnel_detected is False
    assert view.host_accepted is True
    assert view.dns_name == "node.ts.net"
    assert view.dns_name_source == "VERIFIED"
    assert _normalize_check(view.observed_backend) == "http://127.0.0.1:8765"
    assert exact_desired_live_serve(view) is True
    assert plan_enable(view) == "adopt"
    assert plan_disable(view) == "reset"


def _serve_with(
    *, dns="node.ts.net", handlers=None, extra_web=None, extra_tcp=None, extra_top=None,
):
    web = {
        f"{dns}:443": {
            "Handlers": handlers or {"/": {"Proxy": "http://127.0.0.1:8765"}},
        },
    }
    if extra_web:
        web.update(extra_web)
    tcp = {"443": {"HTTPS": True}}
    if extra_tcp:
        tcp.update(extra_tcp)
    doc = {"TCP": tcp, "Web": web}
    if extra_top:
        doc.update(extra_top)
    return doc


def test_exact_serve_topology_ignores_informational_fields():
    dns = "node.ts.net"
    view = tailscale_status(
        runner=_tailscale_runner(
            _connected(dns + "."),
            _serve_with(
                dns=dns,
                extra_top={"ETag": "abc", "Comment": "not forwarding"},
            ),
        ),
    )
    assert view.serve_status == "VERIFIED"
    assert view.remote_access == "VERIFIED"
    assert view.funnel_detected is False


@pytest.mark.parametrize(
    "serve_doc",
    [
        _serve_with(
            handlers={
                "/": {"Proxy": "http://127.0.0.1:8765"},
                "/extra": {"Proxy": "http://127.0.0.1:9999"},
            },
        ),
        _serve_with(
            handlers={
                "/": {"Proxy": "http://127.0.0.1:8765"},
                "/other": {"Proxy": "http://127.0.0.1:8765"},
            },
        ),
        _serve_with(
            extra_web={
                "other.ts.net:443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}},
                },
            },
        ),
        _serve_with(extra_web={"other.ts.net:443": {}}),
        _serve_with(extra_tcp={"22": {"TCPForward": "127.0.0.1:22"}}),
        _serve_with(extra_tcp={"8443": {"HTTPS": True}}),
        _serve_with(
            extra_web={
                "node.ts.net:8443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:9000"}},
                },
            },
        ),
        _serve_with(
            extra_top={
                "Foreground": {
                    "sess": {
                        "TCP": {"8443": {"HTTPS": True}},
                        "Web": {
                            "node.ts.net:8443": {
                                "Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}},
                            },
                        },
                    },
                },
            },
        ),
        _serve_with(
            extra_top={
                "Services": {
                    "svc": {
                        "TCP": {"443": {"HTTPS": True}},
                        "Web": {
                            "svc.ts.net:443": {
                                "Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}},
                            },
                        },
                    },
                },
            },
        ),
    ],
)
def test_mixed_serve_topology_is_mismatch_not_verified(serve_doc):
    view = tailscale_status(
        runner=_tailscale_runner(_connected("node.ts.net."), serve_doc),
    )
    assert view.serve_status == "MISMATCH"
    assert view.serve_status != "VERIFIED"
    assert view.remote_access != "VERIFIED"
    assert view.funnel_detected is False
    assert exact_desired_live_serve(view) is False
    assert plan_enable(view) == "tailscale_serve_mismatch"
    assert plan_disable(view) == "tailscale_serve_mismatch"


def _normalize_check(value: str | None) -> str:
    from code_slayer.admin.tailscale import _normalize_proxy
    assert value is not None
    return _normalize_proxy(value)


def test_tailscale_malformed_or_unavailable_serve_is_never_verified():
    connected = {"BackendState": "Running", "Self": {"DNSName": "node.ts.net."}}
    malformed = tailscale_status(
        runner=_tailscale_runner(connected, None, serve_raw="not-json"),
    )
    assert malformed.serve_status == "ERROR"
    assert malformed.remote_access != "VERIFIED"
    failed = tailscale_status(
        runner=_tailscale_runner(connected, {}, serve_code=1),
    )
    assert failed.serve_status == "ERROR"
    assert failed.remote_access != "VERIFIED"
    missing = tailscale_status(
        runner=lambda argv, **kwargs: (_ for _ in ()).throw(
            ProcessError("executable_missing", "tailscale"),
        ),
    )
    assert missing.remote_access != "VERIFIED"
    assert missing.serve_status == "UNVERIFIED"


def test_tailscale_funnel_mapping_is_mismatch_not_verified():
    view = tailscale_status(
        runner=_tailscale_runner(
            {"BackendState": "Running", "Self": {"DNSName": "node.ts.net."}},
            {
                "TCP": {"443": {"HTTPS": True}},
                "Web": {
                    "node.ts.net:443": {
                        "Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}},
                    },
                },
                "AllowFunnel": {"node.ts.net:443": True},
            },
        ),
    )
    assert view.funnel_detected is True
    assert view.serve_status == "MISMATCH"
    assert view.remote_access != "VERIFIED"
    assert view.remote_access == "MISMATCH"


def _connected(dns="adrian-kanon.tail64e440.ts.net."):
    return {"BackendState": "Running", "Self": {"DNSName": dns}}


def _matching_serve(dns="adrian-kanon.tail64e440.ts.net"):
    return {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {
            f"{dns}:443": {
                "Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}},
            },
        },
    }


def test_static_trusted_hosts_reject_wildcards_and_tailscale_ips():
    with pytest.raises(ValueError):
        exact_static_hosts([".ts.net"])
    with pytest.raises(ValueError):
        exact_static_hosts(["*"])
    with pytest.raises(ValueError):
        exact_static_hosts(["100.64.1.2"])
    hosts = exact_static_hosts(["127.0.0.1", "localhost", "[::1]"])
    assert all(not item.startswith(".") for item in hosts)
    assert "*" not in hosts


def test_loopback_host_is_accepted(admin_app):
    client, _app, _config, _repo = admin_app
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health", headers={"Host": "127.0.0.1"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "localhost"}).status_code == 200


def test_origin_allowed_helper_rejects_untrusted_and_mismatched_origins():
    dns = "adrian-kanon.tail64e440.ts.net"
    assert origin_allowed(
        "http://127.0.0.1:8765", "127.0.0.1:8765",
        bind_port=8765, observed_dns_name=dns,
    )
    assert origin_allowed(
        "http://localhost:8765", "localhost:8765",
        bind_port=8765, observed_dns_name=None,
    )
    assert origin_allowed(
        "http://localhost", "localhost",
        bind_port=8765, observed_dns_name=None,
    )
    assert origin_allowed(
        "http://localhost:80", "localhost",
        bind_port=8765, observed_dns_name=None,
    )
    assert origin_allowed(
        f"https://{dns}", dns,
        bind_port=8765, observed_dns_name=dns,
    )
    assert origin_allowed(
        f"https://{dns}:443", f"{dns}:443",
        bind_port=8765, observed_dns_name=dns + ".",
    )
    assert origin_allowed(
        "http://[::1]:8765", "[::1]:8765",
        bind_port=8765, observed_dns_name=None,
    )
    denied = (
        ("http://node.ts.net", dns),
        (f"https://{dns}", "127.0.0.1:8765"),
        ("https://evil.invalid", dns),
        (f"https://evil.{dns}", dns),
        (f"https://{dns}.evil.invalid", dns),
        (f"http://{dns}", dns),
        (f"https://user@{dns}", dns),
        ("null", dns),
        (f"https://{dns}/", dns),
        (f"https://{dns}?q=1", dns),
        (f"https://{dns},https://evil.invalid", dns),
        ("http://127.0.0.1:9999", "127.0.0.1:9999"),
        ("http://127.0.0.1", "127.0.0.1:8765"),
        ("http://localhost:8765", "127.0.0.1:8765"),
        ("https://127.0.0.1:8765", "127.0.0.1:8765"),
        (f"https://{dns}:8443", f"{dns}:8443"),
        (f"https://{dns}", "localhost"),
    )
    for origin, host in denied:
        assert origin_allowed(
            origin, host, bind_port=8765, observed_dns_name=dns,
        ) is False, origin
    assert origin_allowed(
        f"https://{dns}", dns, bind_port=8765, observed_dns_name=None,
    ) is False
    assert origin_allowed(
        f"https://{dns}", dns, bind_port=8765, observed_dns_name="other.ts.net",
    ) is False


def test_browser_origin_policy_loopback_and_tailscale(
    git_repo_with_commit, tmp_path,
):
    dns = "adrian-kanon.tail64e440.ts.net"
    https = f"https://{dns}"
    application, client, config = _opened(
        git_repo_with_commit,
        tmp_path,
        _tailscale_runner(_connected(dns + "."), _matching_serve(dns)),
    )
    try:
        loopback = {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765"}
        assert client.get("/api/health", headers=loopback).status_code == 200
        localhost = {"Host": "localhost:8765", "Origin": "http://localhost:8765"}
        assert client.get("/api/health", headers=localhost).status_code == 200
        assert client.get(
            "/api/health", headers={"Host": "localhost", "Origin": "http://localhost"},
        ).status_code == 200
        ts = {"Host": dns, "Origin": https}
        health = client.get("/api/health", headers=ts)
        assert health.status_code == 200
        assert "error" not in health.get_json()
        assert client.get(
            "/api/health", headers={"Host": dns, "Origin": f"http://{dns}"},
        ).status_code == 403
        assert client.get(
            "/api/health", headers={"Host": dns, "Origin": "https://evil.invalid"},
        ).status_code == 403
        assert client.get(
            "/api/health",
            headers={"Host": dns, "Origin": f"https://evil.{dns}"},
        ).status_code == 403
        assert client.get("/api/health", headers={"Host": "evil.invalid"}).status_code == 400
        assert client.get(
            "/api/health",
            headers={
                "Host": dns,
                "Origin": "https://evil.invalid",
                "X-Forwarded-Proto": "https",
            },
        ).status_code == 403
        assert client.get(
            "/api/health",
            headers={
                "Host": "localhost",
                "Origin": https,
                "X-Forwarded-Host": dns,
                "X-Forwarded-Proto": "https",
            },
        ).status_code == 403
        assert client.get(
            "/api/health",
            headers={**ts, "Sec-Fetch-Site": "cross-site"},
        ).status_code == 403
        assert client.get("/api/health", headers={"Host": dns}).status_code == 200
        enable = client.post("/api/tailscale/enable", json={}, headers=ts)
        assert enable.status_code == 200
        view = enable.get_json()
        assert "error" not in view
        assert view["enabled"] is True
        assert view["alignment"] == "VERIFIED"
        again = client.post("/api/tailscale/enable", json={}, headers=ts)
        assert again.status_code == 200
        assert again.get_json()["enabled"] is True
        assert load_config(path=config).tailscale.enabled is True
        assert client.get(
            "/api/health",
            headers={"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:80"},
        ).status_code == 403
        assert client.get(
            "/api/health",
            headers={"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765/"},
        ).status_code == 403
        assert client.get(
            "/api/health", headers={"Host": dns, "Origin": "null"},
        ).status_code == 403
        assert client.get(
            "/api/health",
            headers={"Host": dns, "Origin": f"https://user:pass@{dns}"},
        ).status_code == 403
    finally:
        application.extensions["codeslayer"].close()


def test_config_bound_tailscale_enabled_does_not_widen_origin(
    git_repo_with_commit, tmp_path,
):
    dns = "adrian-kanon.tail64e440.ts.net"
    save_config(
        CSLRConfig(tailscale=TailscaleConfig(enabled=True)),
        path=tmp_path / "config.toml",
    )
    application, client, _config = _opened(
        git_repo_with_commit,
        tmp_path,
        _tailscale_runner({"BackendState": "Stopped", "Self": {}}, {}),
        name="config.toml",
    )
    try:
        assert client.get(
            "/api/health",
            headers={"Host": dns, "Origin": f"https://{dns}"},
        ).status_code == 400
        assert client.get(
            "/api/health",
            headers={"Host": "localhost", "Origin": f"https://{dns}"},
        ).status_code == 403
    finally:
        application.extensions["codeslayer"].close()


def test_origin_policy_does_not_use_backend_url_or_proxy_headers():
    from code_slayer.admin import hosts as hosts_mod
    from code_slayer.api import app as app_mod

    src = Path(app_mod.__file__).read_text()
    hosts_text = Path(hosts_mod.__file__).read_text()
    assert "request.host_url" not in src
    assert "ProxyFix" not in src
    assert "X-Forwarded-" not in src
    assert "X-Forwarded-" not in hosts_text
    assert "origin_allowed(" in src


def test_exact_tailscale_dns_host_accepted_and_config_drift_is_mismatch(
    git_repo_with_commit, tmp_path,
):
    dns = "adrian-kanon.tail64e440.ts.net"
    application = create_app(
        git_repo_with_commit,
        config_path=tmp_path / "config.toml",
        load_persistent_config=True,
        tailscale_runner=_tailscale_runner(_connected(dns + "."), _matching_serve(dns)),
    )
    try:
        client = application.test_client()
        assert client.get("/api/health", headers={"Host": dns}).status_code == 200
        assert client.get("/api/health", headers={"Host": "evil.invalid"}).status_code == 400
        assert client.get("/api/health", headers={"Host": "100.64.1.2"}).status_code == 400
        view = client.get("/api/tailscale", headers={"Host": "localhost"}).get_json()
        assert view["serve"]["status"] == "VERIFIED"
        assert view["host"]["name"] == dns
        assert view["host"]["accepted"] is True
        assert view["remote_access"] == "VERIFIED"
        assert view["enabled"] is False
        assert view["intent"]["enabled"] is False
        assert view["intent"]["alignment"] == "MISMATCH"
        assert view["alignment"] == "MISMATCH"
    finally:
        application.extensions["codeslayer"].close()


def test_malformed_tailscale_dns_does_not_widen_trusted_hosts(
    git_repo_with_commit, tmp_path,
):
    dns = "adrian-kanon.tail64e440.ts.net"
    samples = ("*.ts.net", ".ts.net", "100.64.1.2", "*", "node", "http://evil.example")
    for index, raw in enumerate(samples):
        runner = _tailscale_runner(
            {
                "BackendState": "Running",
                "Self": {"DNSName": raw, "TailscaleIPs": ["100.64.1.2"]},
            },
            _matching_serve(dns),
        )
        application = create_app(
            git_repo_with_commit,
            config_path=tmp_path / f"config-{index}.toml",
            load_persistent_config=True,
            tailscale_runner=runner,
        )
        try:
            client = application.test_client()
            assert client.get("/api/health", headers={"Host": "localhost"}).status_code == 200
            assert client.get("/api/health", headers={"Host": raw}).status_code == 400
            assert client.get("/api/health", headers={"Host": dns}).status_code == 400
            assert client.get("/api/health", headers={"Host": "100.64.1.2"}).status_code == 400
            view = client.get("/api/tailscale").get_json()
            assert view["host"]["accepted"] is False
            assert view["remote_access"] != "VERIFIED"
        finally:
            application.extensions["codeslayer"].close()


def test_serve_match_without_accepted_host_is_not_remote_verified():
    view = tailscale_status(
        runner=_tailscale_runner(
            {"BackendState": "Running", "Self": {}},
            _matching_serve("node.ts.net"),
        ),
    )
    assert view.serve_status != "VERIFIED"
    assert view.serve_status == "MISMATCH"
    assert view.host_accepted is False
    assert view.remote_access != "VERIFIED"
    assert view.remote_access == "MISMATCH"


def test_serve_host_mismatch_is_not_remote_verified():
    view = tailscale_status(
        runner=_tailscale_runner(
            _connected("node.ts.net."),
            _matching_serve("other.ts.net"),
        ),
    )
    assert view.serve_status != "VERIFIED"
    assert view.serve_status == "MISMATCH"
    assert view.remote_access != "VERIFIED"
    assert view.host_accepted is False


def test_tailscale_host_discovery_refreshes_without_restart(
    git_repo_with_commit, tmp_path,
):
    state = {"dns": None}

    def runner(argv, **kwargs):
        if argv[:3] == ("tailscale", "status", "--json"):
            if not state["dns"]:
                return ProcessResult(tuple(argv), 1, "", "down")
            return _ok(argv, json.dumps(_connected(state["dns"])))
        if argv[:4] == ("tailscale", "serve", "status", "--json"):
            if not state["dns"]:
                return _ok(argv, "{}")
            return _ok(argv, json.dumps(_matching_serve("node.ts.net")))
        return _ok(argv)

    application = create_app(
        git_repo_with_commit,
        config_path=tmp_path / "config.toml",
        load_persistent_config=True,
        tailscale_runner=runner,
    )
    try:
        client = application.test_client()
        assert client.get("/api/health", headers={"Host": "node.ts.net"}).status_code == 400
        state["dns"] = "node.ts.net."
        assert client.get("/api/health", headers={"Host": "node.ts.net"}).status_code == 200
        view = client.get("/api/tailscale", headers={"Host": "localhost"}).get_json()
        assert view["remote_access"] == "VERIFIED"
        assert view["host"]["accepted"] is True
    finally:
        application.extensions["codeslayer"].close()


def test_intent_alignment_distinguishes_config_and_live_serve():
    assert intent_alignment(False, "not_configured") == "VERIFIED"
    assert intent_alignment(True, "VERIFIED") == "VERIFIED"
    assert intent_alignment(False, "VERIFIED") == "MISMATCH"
    assert intent_alignment(True, "not_configured") == "MISMATCH"
    assert intent_alignment(False, "UNVERIFIED") == "UNVERIFIED"


def _enable_calls(captured):
    return [argv for argv in captured if argv[:2] == ("tailscale", "serve") and "--bg" in argv]


def _reset_calls(captured):
    return [argv for argv in captured if argv[:3] == ("tailscale", "serve", "reset")]


def _live_runner(
    node_doc,
    serve_doc,
    *,
    enable_code=0,
    reset_code=0,
    serve_code=0,
    serve_raw=None,
    after_enable=None,
    after_reset=None,
):
    state = {"node": node_doc, "serve": serve_doc}
    captured = []

    def runner(argv, **kwargs):
        captured.append(argv)
        if argv[:3] == ("tailscale", "status", "--json"):
            return _ok(argv, json.dumps(state["node"]))
        if argv[:4] == ("tailscale", "serve", "status", "--json"):
            if serve_raw is not None:
                return ProcessResult(tuple(argv), serve_code, serve_raw, "")
            return ProcessResult(tuple(argv), serve_code, json.dumps(state["serve"]), "")
        if argv[:2] == ("tailscale", "serve") and "--bg" in argv:
            if enable_code != 0:
                return ProcessResult(tuple(argv), enable_code, "", "already serving")
            if after_enable is not None:
                state["serve"] = after_enable
            return _ok(argv)
        if argv[:3] == ("tailscale", "serve", "reset"):
            if reset_code != 0:
                return ProcessResult(tuple(argv), reset_code, "", "reset failed")
            state["serve"] = {} if after_reset is None else after_reset
            return _ok(argv)
        return _ok(argv)

    return runner, captured


def _opened(repo, tmp_path, runner, *, name="config.toml", config=None):
    path = tmp_path / name
    if config is not None:
        save_config(config, path=path)
    application = create_app(
        repo,
        config_path=path,
        load_persistent_config=True,
        tailscale_runner=runner,
    )
    return application, application.test_client(), path


def _view_base(**overrides):
    data = dict(
        node_state="Connected",
        node_source="OBSERVED",
        serve_status="VERIFIED",
        serve_source="LIVE_ATTESTED",
        remote_access="VERIFIED",
        url="https://node.ts.net",
        expected_backend="http://127.0.0.1:8765",
        observed_backend="http://127.0.0.1:8765",
        funnel_detected=False,
        dns_name="node.ts.net",
        dns_name_source="VERIFIED",
        host_accepted=True,
        host_accepted_source="VERIFIED",
        serve_hosts=("node.ts.net",),
    )
    data.update(overrides)
    return TailscaleView(**data)


def test_exact_desired_live_serve_requires_full_path():
    good = _view_base()
    assert exact_desired_live_serve(good) is True
    assert plan_enable(good) == "adopt"
    assert plan_disable(good) == "reset"
    down = _view_base(node_state="Disabled", remote_access="UNVERIFIED")
    assert exact_desired_live_serve(down) is False
    no_host = _view_base(host_accepted=False, remote_access="UNVERIFIED")
    assert exact_desired_live_serve(no_host) is False
    assert exact_desired_live_serve(_view_base(remote_access="UNVERIFIED")) is False
    funnel_view = _view_base(
        funnel_detected=True, serve_status="MISMATCH", remote_access="MISMATCH",
    )
    assert exact_desired_live_serve(funnel_view) is False


def test_plan_enable_and_disable_state_table():
    assert plan_enable(_view_base(
        serve_status="not_configured", observed_backend=None, remote_access="UNVERIFIED",
        host_accepted=True, serve_hosts=(),
    )) == "configure"
    absent_stopped = _view_base(
        node_state="Disabled", serve_status="not_configured", serve_source="LIVE_ATTESTED",
        observed_backend=None, remote_access="UNVERIFIED", host_accepted=False,
        dns_name=None, serve_hosts=(),
    )
    assert plan_enable(absent_stopped) == "tailscale_node_not_connected"
    assert plan_disable(absent_stopped) == "clear_intent"
    mismatch = _view_base(serve_status="MISMATCH", remote_access="MISMATCH")
    assert plan_enable(mismatch) == "tailscale_serve_mismatch"
    assert plan_disable(mismatch) == "tailscale_serve_mismatch"
    funnel = _view_base(
        funnel_detected=True, serve_status="MISMATCH", remote_access="MISMATCH",
    )
    assert plan_enable(funnel) == "tailscale_funnel_detected"
    assert plan_disable(funnel) == "tailscale_funnel_detected"
    error = _view_base(serve_status="ERROR", serve_source="UNVERIFIED", remote_access="ERROR")
    assert plan_enable(error) == "tailscale_serve_error"
    assert plan_disable(error) == "tailscale_serve_error"
    unverified = _view_base(
        serve_status="UNVERIFIED", serve_source="UNVERIFIED", remote_access="UNVERIFIED",
    )
    assert plan_enable(unverified) == "tailscale_serve_unverified"
    assert plan_disable(unverified) == "tailscale_serve_unverified"
    mapping_down = _view_base(node_state="Disabled", remote_access="UNVERIFIED")
    assert plan_enable(mapping_down) == "tailscale_node_not_connected"
    assert plan_disable(mapping_down) == "reset"
    no_host = _view_base(host_accepted=False, remote_access="UNVERIFIED")
    assert plan_enable(no_host) == "tailscale_host_not_accepted"
    assert plan_disable(no_host) == "reset"
    remote_gap = _view_base(remote_access="UNVERIFIED")
    assert exact_desired_live_serve(remote_gap) is False
    assert plan_enable(remote_gap) == "tailscale_remote_unverified"
    absent_no_host = _view_base(
        serve_status="not_configured",
        observed_backend=None,
        remote_access="UNVERIFIED",
        host_accepted=False,
        serve_hosts=(),
    )
    assert plan_enable(absent_no_host) == "tailscale_host_not_accepted"
    assert plan_disable(absent_no_host) == "clear_intent"


def test_enable_adopts_matching_live_serve_without_reconfiguring(
    git_repo_with_commit, tmp_path,
):
    dns = "adrian-kanon.tail64e440.ts.net"
    runner, captured = _live_runner(
        _connected(dns + "."), _matching_serve(dns), enable_code=1,
    )
    application, client, config = _opened(git_repo_with_commit, tmp_path, runner)
    try:
        before = client.get("/api/tailscale").get_json()
        assert before["enabled"] is False
        assert before["alignment"] == "MISMATCH"
        assert before["serve"]["status"] == "VERIFIED"
        assert before["remote_access"] == "VERIFIED"
        response = client.post("/api/tailscale/enable", json={})
        assert response.status_code == 200
        view = response.get_json()
        assert view["enabled"] is True
        assert view["enabled_source"] == "CONFIG_BOUND"
        assert view["alignment"] == "VERIFIED"
        assert view["intent"]["enabled"] is True
        assert view["intent"]["alignment"] == "VERIFIED"
        assert view["serve"]["status"] == "VERIFIED"
        assert view["remote_access"] == "VERIFIED"
        assert view["host"]["accepted"] is True
        assert load_config(path=config).tailscale.enabled is True
        assert _enable_calls(captured) == []
        again = client.post("/api/tailscale/enable", json={})
        assert again.status_code == 200
        assert again.get_json()["enabled"] is True
        assert _enable_calls(captured) == []
    finally:
        application.extensions["codeslayer"].close()


def test_enable_rejects_matching_backend_when_node_not_connected(
    git_repo_with_commit, tmp_path,
):
    dns = "node.ts.net"
    runner, captured = _live_runner(
        {"BackendState": "Stopped", "Self": {"DNSName": dns + "."}},
        _matching_serve(dns),
    )
    application, client, config = _opened(git_repo_with_commit, tmp_path, runner)
    try:
        view = client.get("/api/tailscale").get_json()
        assert view["serve"]["status"] == "VERIFIED"
        assert view["node"]["state"] != "Connected"
        assert view["remote_access"] != "VERIFIED"
        response = client.post("/api/tailscale/enable", json={})
        assert response.status_code == 409
        assert response.get_json()["error"]["code"] == "tailscale_node_not_connected"
        assert load_config(path=config).tailscale.enabled is False
        assert _enable_calls(captured) == []
        assert _reset_calls(captured) == []
    finally:
        application.extensions["codeslayer"].close()


def test_enable_rejects_when_magicdns_host_unknown(
    git_repo_with_commit, tmp_path,
):
    dns = "node.ts.net"
    runner, captured = _live_runner({"BackendState": "Running", "Self": {}}, _matching_serve(dns))
    application, client, config = _opened(git_repo_with_commit, tmp_path, runner)
    try:
        view = client.get("/api/tailscale").get_json()
        assert view["serve"]["status"] != "VERIFIED"
        assert view["serve"]["status"] == "MISMATCH"
        assert view["host"]["accepted"] is False
        assert view["remote_access"] != "VERIFIED"
        response = client.post("/api/tailscale/enable", json={})
        assert response.status_code == 409
        assert response.get_json()["error"]["code"] == "tailscale_serve_mismatch"
        assert load_config(path=config).tailscale.enabled is False
        assert _enable_calls(captured) == []
    finally:
        application.extensions["codeslayer"].close()


def test_enable_rejects_when_serve_host_is_not_machine_magicdns(
    git_repo_with_commit, tmp_path,
):
    runner, captured = _live_runner(
        _connected("node.ts.net."),
        _matching_serve("other.ts.net"),
    )
    application, client, config = _opened(git_repo_with_commit, tmp_path, runner)
    try:
        view = client.get("/api/tailscale").get_json()
        assert view["serve"]["status"] == "MISMATCH"
        assert view["remote_access"] != "VERIFIED"
        response = client.post("/api/tailscale/enable", json={})
        assert response.status_code == 409
        assert response.get_json()["error"]["code"] == "tailscale_serve_mismatch"
        assert load_config(path=config).tailscale.enabled is False
        assert _enable_calls(captured) == []
    finally:
        application.extensions["codeslayer"].close()


def test_enable_mismatch_backend_is_409_without_mutation(
    git_repo_with_commit, tmp_path,
):
    dns = "node.ts.net"
    serve = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {f"{dns}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}},
    }
    runner, captured = _live_runner(_connected(dns + "."), serve)
    application, client, config = _opened(git_repo_with_commit, tmp_path, runner)
    try:
        assert client.get("/api/tailscale").get_json()["serve"]["status"] == "MISMATCH"
        response = client.post("/api/tailscale/enable", json={})
        assert response.status_code == 409
        assert response.get_json()["error"]["code"] == "tailscale_serve_mismatch"
        assert load_config(path=config).tailscale.enabled is False
        assert _enable_calls(captured) == []
    finally:
        application.extensions["codeslayer"].close()


def test_enable_refuses_funnel_and_does_not_persist(
    git_repo_with_commit, tmp_path,
):
    dns = "adrian-kanon.tail64e440.ts.net"
    serve = dict(_matching_serve(dns))
    serve["AllowFunnel"] = {f"{dns}:443": True}
    runner, captured = _live_runner(_connected(dns + "."), serve)
    application, client, config = _opened(git_repo_with_commit, tmp_path, runner)
    try:
        response = client.post("/api/tailscale/enable", json={})
        assert response.status_code == 409
        assert response.get_json()["error"]["code"] == "tailscale_funnel_detected"
        assert load_config(path=config).tailscale.enabled is False
        assert _enable_calls(captured) == []
        assert _reset_calls(captured) == []
    finally:
        application.extensions["codeslayer"].close()


def test_enable_error_and_unverified_are_409_without_mutation(
    git_repo_with_commit, tmp_path,
):
    dns = "node.ts.net"
    error_runner, error_captured = _live_runner(
        _connected(dns + "."), {}, serve_code=1,
    )
    application, client, config = _opened(
        git_repo_with_commit, tmp_path, error_runner, name="error.toml",
    )
    try:
        response = client.post("/api/tailscale/enable", json={})
        assert response.status_code == 409
        assert response.get_json()["error"]["code"] == "tailscale_serve_error"
        assert load_config(path=config).tailscale.enabled is False
        assert _enable_calls(error_captured) == []
    finally:
        application.extensions["codeslayer"].close()
    unverified_runner, unverified_captured = _live_runner(
        _connected(dns + "."), {}, serve_raw="",
    )
    application, client, config = _opened(
        git_repo_with_commit, tmp_path, unverified_runner, name="unverified.toml",
    )
    try:
        response = client.post("/api/tailscale/enable", json={})
        assert response.status_code == 409
        assert response.get_json()["error"]["code"] == "tailscale_serve_unverified"
        assert load_config(path=config).tailscale.enabled is False
        assert _enable_calls(unverified_captured) == []
    finally:
        application.extensions["codeslayer"].close()


def test_enable_configures_when_not_configured_then_live_verifies(
    git_repo_with_commit, tmp_path,
):
    dns = "adrian-kanon.tail64e440.ts.net"
    runner, captured = _live_runner(
        _connected(dns + "."), {}, after_enable=_matching_serve(dns),
    )
    application, client, config = _opened(git_repo_with_commit, tmp_path, runner)
    try:
        response = client.post("/api/tailscale/enable", json={})
        assert response.status_code == 200
        view = response.get_json()
        assert _enable_calls(captured)
        assert all(
            argv == ("tailscale", "serve", "--bg", "http://127.0.0.1:8765")
            for argv in _enable_calls(captured)
        )
        assert view["enabled"] is True
        assert view["remote_access"] == "VERIFIED"
        assert view["alignment"] == "VERIFIED"
        assert load_config(path=config).tailscale.enabled is True
    finally:
        application.extensions["codeslayer"].close()


def test_enable_cli_failure_leaves_config_unchanged(
    git_repo_with_commit, tmp_path,
):
    dns = "node.ts.net"
    runner, captured = _live_runner(_connected(dns + "."), {}, enable_code=1)
    application, client, config = _opened(git_repo_with_commit, tmp_path, runner)
    try:
        response = client.post("/api/tailscale/enable", json={})
        assert response.status_code == 409
        assert response.get_json()["error"]["code"] == "tailscale_serve_failed"
        assert load_config(path=config).tailscale.enabled is False
        assert _enable_calls(captured)
    finally:
        application.extensions["codeslayer"].close()


def test_enable_cli_success_without_live_verify_does_not_claim_or_persist(
    git_repo_with_commit, tmp_path,
):
    dns = "node.ts.net"
    runner, captured = _live_runner(_connected(dns + "."), {})
    application, client, config = _opened(git_repo_with_commit, tmp_path, runner)
    try:
        response = client.post("/api/tailscale/enable", json={})
        assert response.status_code == 409
        body = response.get_json()
        assert body["error"]["code"] == "tailscale_enable_unverified"
        assert "remote_access" not in body
        assert body.get("alignment") != "VERIFIED"
        assert load_config(path=config).tailscale.enabled is False
        assert _enable_calls(captured)
        later = client.get("/api/tailscale").get_json()
        assert later["enabled"] is False
        assert later["remote_access"] != "VERIFIED"
        assert later["alignment"] != "VERIFIED" or later["serve"]["status"] == "not_configured"
    finally:
        application.extensions["codeslayer"].close()


def test_enable_not_configured_without_accepted_host_does_not_mutate(
    git_repo_with_commit, tmp_path,
):
    runner, captured = _live_runner({"BackendState": "Running", "Self": {}}, {})
    application, client, config = _opened(git_repo_with_commit, tmp_path, runner)
    try:
        view = client.get("/api/tailscale").get_json()
        assert view["serve"]["status"] == "not_configured"
        assert view["node"]["state"] == "Connected"
        assert view["host"]["accepted"] is False
        response = client.post("/api/tailscale/enable", json={})
        assert response.status_code == 409
        assert response.get_json()["error"]["code"] == "tailscale_host_not_accepted"
        assert load_config(path=config).tailscale.enabled is False
        assert _enable_calls(captured) == []
    finally:
        application.extensions["codeslayer"].close()


def test_disable_skips_reset_when_serve_not_configured(
    git_repo_with_commit, tmp_path,
):
    runner, captured = _live_runner(
        {"BackendState": "Stopped", "Self": {}}, {},
    )
    application, client, config = _opened(
        git_repo_with_commit, tmp_path, runner,
        config=CSLRConfig().with_tailscale_enabled(True),
    )
    try:
        response = client.post("/api/tailscale/disable", json={})
        assert response.status_code == 200
        assert response.get_json()["enabled"] is False
        assert load_config(path=config).tailscale.enabled is False
        assert _reset_calls(captured) == []
    finally:
        application.extensions["codeslayer"].close()


def test_disable_resets_expected_verified_mapping(
    git_repo_with_commit, tmp_path,
):
    dns = "node.ts.net"
    runner, captured = _live_runner(_connected(dns + "."), _matching_serve(dns))
    application, client, config = _opened(
        git_repo_with_commit, tmp_path, runner,
        config=CSLRConfig().with_tailscale_enabled(True),
    )
    try:
        response = client.post("/api/tailscale/disable", json={})
        assert response.status_code == 200
        assert response.get_json()["enabled"] is False
        assert load_config(path=config).tailscale.enabled is False
        assert _reset_calls(captured) == [("tailscale", "serve", "reset")]
        assert _enable_calls(captured) == []
    finally:
        application.extensions["codeslayer"].close()


def test_disable_reset_without_live_absence_does_not_persist(
    git_repo_with_commit, tmp_path,
):
    dns = "node.ts.net"
    serve = _matching_serve(dns)
    runner, captured = _live_runner(
        _connected(dns + "."), serve, after_reset=serve,
    )
    application, client, config = _opened(
        git_repo_with_commit, tmp_path, runner,
        config=CSLRConfig().with_tailscale_enabled(True),
    )
    try:
        response = client.post("/api/tailscale/disable", json={})
        assert response.status_code == 409
        body = response.get_json()
        assert body["error"]["code"] == "tailscale_disable_unverified"
        assert load_config(path=config).tailscale.enabled is True
        assert _reset_calls(captured) == [("tailscale", "serve", "reset")]
        assert body.get("alignment") != "VERIFIED"
    finally:
        application.extensions["codeslayer"].close()


def test_disable_mismatch_funnel_error_unverified_do_not_reset(
    git_repo_with_commit, tmp_path,
):
    dns = "node.ts.net"
    mismatch = {
        "TCP": {"443": {"HTTPS": True}},
        "Web": {f"{dns}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}},
    }
    cases = [
        ("mismatch.toml", _connected(dns + "."), mismatch, None, 0, "tailscale_serve_mismatch"),
        (
            "funnel.toml", _connected(dns + "."),
            {**_matching_serve(dns), "AllowFunnel": {f"{dns}:443": True}},
            None, 0, "tailscale_funnel_detected",
        ),
        ("error.toml", _connected(dns + "."), {}, None, 1, "tailscale_serve_error"),
        ("unverified.toml", _connected(dns + "."), {}, "", 0, "tailscale_serve_unverified"),
    ]
    for name, node, serve, serve_raw, serve_code, code in cases:
        runner, captured = _live_runner(
            node, serve, serve_raw=serve_raw, serve_code=serve_code,
        )
        application, client, config = _opened(
            git_repo_with_commit, tmp_path, runner, name=name,
            config=CSLRConfig().with_tailscale_enabled(True),
        )
        try:
            response = client.post("/api/tailscale/disable", json={})
            assert response.status_code == 409, code
            assert response.get_json()["error"]["code"] == code
            assert load_config(path=config).tailscale.enabled is True
            assert _reset_calls(captured) == []
        finally:
            application.extensions["codeslayer"].close()


def _mixed_topology_docs(dns="node.ts.net"):
    expected = {"/": {"Proxy": "http://127.0.0.1:8765"}}
    return [
        ("extra-proxy.toml", _serve_with(
            dns=dns,
            handlers={**expected, "/extra": {"Proxy": "http://127.0.0.1:9999"}},
        )),
        ("extra-path.toml", _serve_with(
            dns=dns,
            handlers={**expected, "/other": {"Proxy": "http://127.0.0.1:8765"}},
        )),
        ("extra-host.toml", _serve_with(
            dns=dns,
            extra_web={
                "other.ts.net:443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}},
                },
            },
        )),
        ("empty-extra-host.toml", _serve_with(
            dns=dns, extra_web={"other.ts.net:443": {}},
        )),
        ("extra-tcp.toml", _serve_with(
            dns=dns, extra_tcp={"22": {"TCPForward": "127.0.0.1:22"}},
        )),
        ("multi-proxy.toml", _serve_with(
            dns=dns,
            extra_web={
                f"{dns}:8443": {
                    "Handlers": {"/": {"Proxy": "http://127.0.0.1:9000"}},
                },
            },
        )),
    ]


def test_enable_does_not_adopt_or_overwrite_mixed_topology(
    git_repo_with_commit, tmp_path,
):
    dns = "node.ts.net"
    for name, serve in _mixed_topology_docs(dns):
        runner, captured = _live_runner(_connected(dns + "."), serve)
        application, client, config = _opened(
            git_repo_with_commit, tmp_path, runner, name=name,
        )
        try:
            view = client.get("/api/tailscale").get_json()
            assert view["serve"]["status"] == "MISMATCH", name
            assert view["enabled"] is False
            response = client.post("/api/tailscale/enable", json={})
            assert response.status_code == 409, name
            assert response.get_json()["error"]["code"] == "tailscale_serve_mismatch"
            assert load_config(path=config).tailscale.enabled is False
            assert _enable_calls(captured) == []
            assert _reset_calls(captured) == []
        finally:
            application.extensions["codeslayer"].close()


def test_disable_does_not_reset_mixed_topology(
    git_repo_with_commit, tmp_path,
):
    dns = "node.ts.net"
    for name, serve in _mixed_topology_docs(dns):
        runner, captured = _live_runner(_connected(dns + "."), serve)
        application, client, config = _opened(
            git_repo_with_commit, tmp_path, runner, name=name,
            config=CSLRConfig().with_tailscale_enabled(True),
        )
        try:
            assert load_config(path=config).tailscale.enabled is True
            response = client.post("/api/tailscale/disable", json={})
            assert response.status_code == 409, name
            assert response.get_json()["error"]["code"] == "tailscale_serve_mismatch"
            assert load_config(path=config).tailscale.enabled is True
            assert _reset_calls(captured) == []
            assert _enable_calls(captured) == []
        finally:
            application.extensions["codeslayer"].close()


def test_disable_does_not_reset_when_magicdns_host_unknown(
    git_repo_with_commit, tmp_path,
):
    dns = "node.ts.net"
    runner, captured = _live_runner(
        {"BackendState": "Running", "Self": {}}, _matching_serve(dns),
    )
    application, client, config = _opened(
        git_repo_with_commit, tmp_path, runner,
        config=CSLRConfig().with_tailscale_enabled(True),
    )
    try:
        response = client.post("/api/tailscale/disable", json={})
        assert response.status_code == 409
        assert response.get_json()["error"]["code"] == "tailscale_serve_mismatch"
        assert load_config(path=config).tailscale.enabled is True
        assert _reset_calls(captured) == []
    finally:
        application.extensions["codeslayer"].close()


def test_systemd_unit_does_not_bind_tailscale_or_wildcard_addresses():
    unit = render_user_unit(
        python_or_codeslayer=Path("/tmp/venv/bin/codeslayer"),
        checkout=Path("/tmp/code-slayer"),
        webui_dir=Path("/tmp/code-slayer/webui"),
    )
    assert "0.0.0.0" not in unit
    assert "100." not in unit
    assert "--host" not in unit


def test_update_refuses_unexpected_remote_dirty_and_divergent(tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / ".git").mkdir()

    def unexpected(argv, **kwargs):
        if "get-url" in argv:
            return _ok(argv, "https://evil.example/repo.git")
        return _ok(argv, "x")

    result = check_for_update(checkout, runner=unexpected, fetch=False)
    assert result.status == "MISMATCH"
    assert result.detail == "unexpected_origin_remote"
    with pytest.raises(ProcessError, match="unexpected_remote"):
        apply_update(checkout, runner=unexpected)

    def dirty(argv, **kwargs):
        joined = " ".join(argv)
        if "get-url" in joined:
            return _ok(argv, "https://github.com/lillakaninen23-sys/code-slayer.git")
        if "abbrev-ref" in joined:
            return _ok(argv, "main")
        if argv[-1] == "HEAD" and "rev-parse" in argv:
            return _ok(argv, "a" * 40)
        if "porcelain" in joined:
            return _ok(argv, " M file")
        if "origin/main" in argv:
            return _ok(argv, "b" * 40)
        if "is-ancestor" in joined:
            return ProcessResult(tuple(argv), 0, "", "")
        if "fetch" in argv:
            return _ok(argv)
        return _ok(argv)

    checked = check_for_update(checkout, runner=dirty, fetch=True)
    assert checked.dirty is True
    assert checked.status == "MISMATCH"
    with pytest.raises(ProcessError, match="dirty"):
        apply_update(checkout, runner=dirty)

    def divergent(argv, **kwargs):
        joined = " ".join(argv)
        if "get-url" in joined:
            return _ok(argv, "https://github.com/lillakaninen23-sys/code-slayer.git")
        if "abbrev-ref" in joined:
            return _ok(argv, "main")
        if argv[-1] == "HEAD" and "rev-parse" in argv:
            return _ok(argv, "a" * 40)
        if "porcelain" in joined:
            return _ok(argv, "")
        if "origin/main" in argv:
            return _ok(argv, "b" * 40)
        if "is-ancestor" in joined:
            return ProcessResult(tuple(argv), 1, "", "")
        if "fetch" in argv:
            return _ok(argv)
        return _ok(argv)

    checked = check_for_update(checkout, runner=divergent, fetch=True)
    assert checked.divergent is True
    with pytest.raises(ProcessError, match="divergent"):
        apply_update(checkout, runner=divergent)


def test_apply_update_is_ff_only_and_not_a_deployment(tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    merges = []

    def runner(argv, **kwargs):
        joined = " ".join(argv)
        if "get-url" in joined:
            return _ok(argv, "https://github.com/lillakaninen23-sys/code-slayer.git")
        if "abbrev-ref" in joined:
            return _ok(argv, "main")
        if argv[-1] == "HEAD" and "rev-parse" in argv:
            if merges:
                return _ok(argv, "b" * 40)
            return _ok(argv, "a" * 40)
        if "porcelain" in joined:
            return _ok(argv, "")
        if "origin/main" in argv:
            return _ok(argv, "b" * 40)
        if "is-ancestor" in joined:
            return ProcessResult(tuple(argv), 0, "", "")
        if "fetch" in argv:
            return _ok(argv)
        if "--ff-only" in argv:
            merges.append(argv)
            return _ok(argv)
        return _ok(argv)

    result = apply_update(checkout, runner=runner)
    assert merges
    assert "--ff-only" in merges[0]
    assert result.detail == "up_to_date"
    assert result.current_commit == "b" * 40


def test_cli_service_commands_exist():
    runner = CliRunner()
    help_text = runner.invoke(cli, ["--help"]).output
    for name in ("install-service", "status", "start", "stop", "restart", "serve"):
        assert name in help_text


def test_cli_status(monkeypatch):
    monkeypatch.setattr(
        "code_slayer.admin.service.run_fixed",
        lambda argv, **kwargs: _ok(argv, "active\n"),
    )
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "running: True" in result.output


def test_service_status_unverified_when_systemctl_missing():
    def missing(argv, **kwargs):
        raise ProcessError("executable_missing", "systemctl")

    status = service_status(runner=missing)
    assert status.state == "UNVERIFIED"
    assert status.active is False


def test_public_admin_path_does_not_import_fake_adapter():
    for rel in (
        "src/code_slayer/api/admin.py",
        "src/code_slayer/api/routes.py",
        "src/code_slayer/config/bindings.py",
        "src/code_slayer/security/live_certification.py",
        "src/code_slayer/admin/runtime.py",
    ):
        source = Path(rel).read_text()
        assert "FakeWorkerAdapter" not in source
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert all(alias.name != "FakeWorkerAdapter" for alias in node.names)
    sig = inspect.signature(
        __import__(
            "code_slayer.security.live_certification",
            fromlist=["certify_live_baseline_security"],
        ).certify_live_baseline_security,
    )
    assert "adapter" not in sig.parameters
    frontend = _webui_shipped_text()
    # Commit A restored the Control Room; admin identity POSTs are not
    # in the UI yet. Inspect every shipped frontend file, not the
    # previous single-file admin SPA.
    assert "192.168.32.8" not in frontend
    assert "funnel" not in frontend.lower()


def _webui_shipped_text() -> str:
    """Shipped Control Room assets only. Tests live under webui/tests/."""
    parts = [Path("webui/index.html").read_text()]
    for path in sorted(Path("webui/static").iterdir()):
        if path.is_file():
            parts.append(path.read_text())
    return "\n".join(parts)


def test_webui_does_not_post_digest():
    """Frontend must never send digest/fingerprint/outcome/evidence.

    Commit A removed the admin SPA before those POSTs are re-added.
    Scan api.js (the only HTTP module) for authority fields rather than
    deleted registerWorker/approveWorker function names.
    """
    api = Path("webui/static/api.js").read_text()
    frontend = _webui_shipped_text()
    assert "192.168.32.8" not in frontend
    for field in (
        "approved_model_digest",
        "model_digest",
        "runtime_identity_fingerprint",
        "fingerprint",
        "evidence_ref",
        "hard_disqualifiers",
        "adapter",
        "outcome",
    ):
        assert field not in api
    assert "JSON.stringify(data)" in api
    assert "/api${path}" in api


def test_cslr_wrapper_exists_and_is_executable():
    wrapper = Path("cslr")
    assert wrapper.is_file()
    assert wrapper.stat().st_mode & 0o111
    text = wrapper.read_text()
    assert "PYTHONPATH" in text
    assert "CODESLAYER_CERT" not in text or "CODESLAYER_CERT_*" in text
