"""HTTP contract version 1. Routes validate inputs and call application methods."""

from dataclasses import asdict

from flask import Blueprint, current_app, jsonify, request

from code_slayer import __version__
from code_slayer.api.reads import ResourceNotFound, reason_code
from code_slayer.api.service import APIError
from code_slayer.store.db import known_schema_version

api = Blueprint("api", __name__, url_prefix="/api")


def service():
    return current_app.extensions["codeslayer"]


def body(fields, required=()):
    if not request.is_json:
        raise APIError("json_required", "Send an application/json object.", 415)
    data = request.get_json()
    if not isinstance(data, dict) or set(data) - set(fields) or set(required) - set(data):
        raise APIError("invalid_fields", "Request has missing or unsupported fields.")
    for key, value in data.items():
        if not isinstance(value, str) or not value.strip() or len(value) > fields[key]:
            raise APIError("invalid_input", "A field is empty, invalid, or too long.")
    return data


def page_args():
    if set(request.args) - {"limit", "offset"}:
        raise APIError("invalid_query", "Unsupported query parameter.")
    try:
        limit = int(request.args.get("limit", "100"))
        offset = int(request.args.get("offset", "0"))
    except ValueError:
        raise APIError("invalid_query", "Pagination must use integers.") from None
    if not 1 <= limit <= 100 or not 0 <= offset <= 1000000:
        raise APIError("invalid_query", "Pagination is outside the allowed range.")
    return limit, offset


def result_json(result):
    data = asdict(result)
    # Output/diagnostic blobs are deliberately outside the v1 status contract.
    data.pop("final_text")
    data["reason"] = reason_code(result.reason)
    return jsonify(data)


@api.get("/health")
def health():
    with service().reads() as reads:
        project = reads.project()
    return jsonify(
        {
            "status": "ok",
            "api_version": 1,
            "source_version": __version__,
            "schema_version": project["schema_version"],
            "known_schema_version": known_schema_version(),
            "actions": {
                "start": service().bindings.analyst_factory is not None,
                "execution_configured": service().bindings.adapter_factory is not None,
            },
            "configuration_status": "ready"
            if service().bindings.analyst_factory
            else "analyst_not_configured",
            "capability_profiles": ["read_only"],
            "cloud_authorization": "not_exposed",
        }
    )


@api.get("/project")
def project():
    with service().reads() as reads:
        return jsonify(reads.project())


@api.get("/runs")
def runs():
    limit, offset = page_args()
    with service().reads() as reads:
        return jsonify(reads.list_runs(limit, offset))


@api.get("/runs/<run_id>")
def run_detail(run_id):
    with service().reads() as reads:
        data = reads.detail(run_id)
    data["execution_configured"] = service().bindings.adapter_factory is not None
    if data["next_safe_action"] == "resume" and not data["execution_configured"]:
        data["next_safe_action"] = "configure_worker_adapter"
    return jsonify(data)


@api.post("/runs")
def start():
    data = body(
        {"prompt": 32768, "worker_id": 200, "role": 100, "capability_profile": 32},
        ("prompt", "worker_id", "role"),
    )
    if data.get("capability_profile", "read_only") != "read_only":
        raise APIError("unsupported_profile", "This API slice supports read_only runs only.")
    response = result_json(service().start(data))
    response.status_code = 201
    response.headers["Location"] = "/api/runs/" + response.get_json()["run_id"]
    return response


@api.post("/runs/<run_id>/resume")
def resume(run_id):
    body({})
    return result_json(service().resume(run_id))


@api.post("/runs/<run_id>/resolutions")
def resolve(run_id):
    data = body(
        {"ambiguity_id": 200, "answer": 16384, "resolution_kind": 32},
        ("ambiguity_id", "answer", "resolution_kind"),
    )
    if data["resolution_kind"] not in ("FACT", "AUTHORIZATION"):
        raise APIError("invalid_resolution_kind", "Choose FACT or explicit AUTHORIZATION.")
    return result_json(service().resolve(run_id, data))


@api.get("/workers")
def workers():
    with service().reads() as reads:
        return jsonify({"workers": reads.workers()})


@api.get("/workers/<worker_id>/trust")
def trust(worker_id):
    with service().reads() as reads:
        if reads.worker(worker_id) is None:
            raise ResourceNotFound()
        return jsonify(reads.trust(worker_id))


@api.get("/workers/<worker_id>/conformance")
def conformance(worker_id):
    with service().reads() as reads:
        if reads.worker(worker_id) is None:
            raise ResourceNotFound()
        return jsonify(reads.conformance(worker_id))


@api.get("/runs/<run_id>/audit")
def audit(run_id):
    limit, _ = page_args()
    with service().reads() as reads:
        return jsonify(reads.audit(run_id, limit))
