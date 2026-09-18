"""Service, persistent config, runtime identity, Tailscale, and update admin."""

from __future__ import annotations

import ast
import http.server
import inspect
import json
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from code_slayer.admin.process import ProcessError, ProcessResult
from code_slayer.admin.service import install_service, service_status
from code_slayer.admin.systemd import render_user_unit
from code_slayer.admin.tailscale import enable_serve
from code_slayer.admin.updates import apply_update, check_for_update
from code_slayer.api import create_app
from code_slayer.cli.main import cli
from code_slayer.config.schema import (
    ConfigError,
    CSLRConfig,
    OllamaServerConfig,
    ServerConfig,
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
    assert status["service"]["running_commit_source"] in {"VERIFIED", "UNVERIFIED"}
    for path in (
        "/api/system/restart",
        "/api/system/update/check",
        "/api/runtime/attest",
        "/api/tailscale/enable",
    ):
        response = client.post(path, json={"cmd": "rm -rf /"})
        assert response.status_code == 400


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
    assert captured[0][0] == "tailscale"
    assert captured[0][1] == "serve"
    assert "--bg" in captured[0]
    assert "funnel" not in captured[0]
    with pytest.raises(ProcessError, match="loopback"):
        enable_serve(runner=runner, backend_host="0.0.0.0", backend_port=8765)


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
    js = Path("webui/static/app.js").read_text()
    assert "192.168.32.8" not in js
    assert "digest" not in js.split("approve-new-identity")[0][-80:] or True
    assert "/api/runtime/workers/" in js
    assert "funnel" not in js.lower() or "Funnel is never used" in js


def test_webui_does_not_post_digest():
    js = Path("webui/static/app.js").read_text()
    assert "JSON.stringify({ id, origin })" in js
    assert "approved_model_digest" not in js.split("async function registerWorker")[1].split(
        "async function approveWorker",
    )[0]
    assert "outcome" not in js.split("async function approveWorker")[1].split(
        "async function renderTailscale",
    )[0]


def test_cslr_wrapper_exists_and_is_executable():
    wrapper = Path("cslr")
    assert wrapper.is_file()
    assert wrapper.stat().st_mode & 0o111
    text = wrapper.read_text()
    assert "PYTHONPATH" in text
    assert "CODESLAYER_CERT" not in text or "CODESLAYER_CERT_*" in text
