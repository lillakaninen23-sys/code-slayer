"""Flask construction, server configuration, and uniform error handling."""

import sqlite3
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from code_slayer.admin.hosts import LOOPBACK_TRUSTED_HOSTS, LiveTrustedHosts, exact_static_hosts
from code_slayer.admin.tailscale import observe_self_dns_name
from code_slayer.api.reads import ResourceNotFound
from code_slayer.api.routes import api
from code_slayer.api.service import APIError, ApplicationService
from code_slayer.repo.git import GitError
from code_slayer.store.db import SchemaError


def create_app(
    repo_path,
    *,
    state_root=None,
    webui_dir=None,
    bindings=None,
    trusted_hosts=LOOPBACK_TRUSTED_HOSTS,
    config_path=None,
    load_persistent_config=False,
    tailscale_runner=None,
):
    app = Flask(__name__, static_folder=None)
    static = exact_static_hosts(trusted_hosts)

    def _tailscale_host():
        return observe_self_dns_name(runner=tailscale_runner)

    app.config.update(
        MAX_CONTENT_LENGTH=65536,
        TRUSTED_HOSTS=LiveTrustedHosts(static, observer=_tailscale_host),
        STATIC_TRUSTED_HOSTS=static,
        TAILSCALE_RUNNER=tailscale_runner,
    )
    app.extensions["codeslayer"] = ApplicationService(
        repo_path,
        state_root=state_root,
        bindings=bindings,
        config_path=config_path,
        load_persistent_config=load_persistent_config,
    )
    app.register_blueprint(api)

    @app.before_request
    def same_origin():
        # No permissive CORS; JSON-only mutations additionally reject cross-origin
        # browser requests. Trusted hosts protect a loopback service from rebinding.
        origin = request.headers.get("Origin")
        if origin is not None and origin != request.host_url.rstrip("/"):
            raise APIError("origin_denied", "Use the WebUI served by this backend.", 403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise APIError("origin_denied", "Cross-site requests are not supported.", 403)

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, APIError):
            code, message, status, retryable = exc.code, exc.message, exc.status, exc.retryable
        elif isinstance(exc, ResourceNotFound):
            code, message, status, retryable = "not_found", "Resource not found.", 404, False
        elif isinstance(exc, HTTPException):
            code, message, status, retryable = (
                f"http_{exc.code}",
                exc.name,
                exc.code,
                False,
            )
        elif isinstance(exc, (sqlite3.Error, SchemaError, GitError, OSError)):
            code, message, status, retryable = (
                "state_unavailable",
                "Backend state is currently unavailable.",
                503,
                True,
            )
        else:
            # Do not serialize or log prompt-bearing exception messages.
            app.logger.error("Application action failed (%s)", type(exc).__name__)
            code, message, status, retryable = (
                "application_error",
                "Action did not finish. Refresh durable run state.",
                500,
                False,
            )
        return jsonify(
            {"error": {"code": code, "message": message, "retryable": retryable}}
        ), status

    if webui_dir is not None:
        root = Path(webui_dir).resolve()
        if not (root / "index.html").is_file() or not (root / "static").is_dir():
            raise ValueError("webui_dir must contain index.html and static/")

        @app.get("/")
        def index():
            return send_from_directory(root, "index.html")

        @app.get("/static/<path:filename>")
        def assets(filename):
            return send_from_directory(root / "static", filename)

        @app.get("/manifest.json")
        def manifest():
            return send_from_directory(root, "manifest.json")

    return app
