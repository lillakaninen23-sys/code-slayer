"""HTTP contract version 1. Routes validate inputs and call application methods."""

from dataclasses import asdict

from flask import Blueprint, current_app, jsonify, request

from code_slayer import __version__
from code_slayer.api.reads import ResourceNotFound, reason_code
from code_slayer.api.service import APIError
from code_slayer.intelligence.limits import (
    DEFAULT_CONTEXT_PACK_MAX_BYTES,
    DEFAULT_CONTEXT_PACK_MAX_FILES,
    DEFAULT_CONTEXT_PACK_PER_FILE_BYTES,
    DEFAULT_QUERY_RESULTS,
    MAX_CONTEXT_PACK_MAX_BYTES,
    MAX_CONTEXT_PACK_MAX_FILES,
    MAX_CONTEXT_PACK_PER_FILE_BYTES,
    MAX_QUERY_RESULTS,
)
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
                "planning_configured": service().bindings.planner_factory is not None,
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


# -- repository intelligence (Phase 8.1) -------------------------------------
#
# Read-only, and never accepts a filesystem root, DB path, trust, or tool
# permission from the client -- the repository is always the server's own
# configured project (`service().repo_path`); every field below is a query
# string and small, explicitly bounded integers only.

def _bounded_int(data, key, default, maximum):
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise APIError("invalid_input", f"{key} must be an integer between 1 and {maximum}.")
    return value


def _intelligence_query_body(*, int_fields):
    if not request.is_json:
        raise APIError("json_required", "Send an application/json object.", 415)
    data = request.get_json()
    allowed = {"text", *int_fields}
    if not isinstance(data, dict) or set(data) - allowed or "text" not in data:
        raise APIError("invalid_fields", "Request has missing or unsupported fields.")
    text = data["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise APIError("invalid_input", "text must be a non-empty string up to 4096 characters.")
    return data


@api.get("/intelligence/status")
def intelligence_status():
    return jsonify(service().intelligence_status())


@api.post("/intelligence/refresh")
def intelligence_refresh():
    body({})
    return jsonify(service().intelligence_refresh())


@api.post("/intelligence/query")
def intelligence_query():
    data = _intelligence_query_body(int_fields=("limit",))
    limit = _bounded_int(data, "limit", DEFAULT_QUERY_RESULTS, MAX_QUERY_RESULTS)
    return jsonify(service().intelligence_query(data["text"], limit))


@api.post("/intelligence/context-pack")
def intelligence_context_pack():
    data = _intelligence_query_body(int_fields=("max_files", "max_bytes", "per_file_bytes"))
    max_files = _bounded_int(
        data, "max_files", DEFAULT_CONTEXT_PACK_MAX_FILES, MAX_CONTEXT_PACK_MAX_FILES,
    )
    max_bytes = _bounded_int(
        data, "max_bytes", DEFAULT_CONTEXT_PACK_MAX_BYTES, MAX_CONTEXT_PACK_MAX_BYTES,
    )
    per_file_bytes = _bounded_int(
        data, "per_file_bytes", DEFAULT_CONTEXT_PACK_PER_FILE_BYTES,
        MAX_CONTEXT_PACK_PER_FILE_BYTES,
    )
    return jsonify(service().intelligence_context_pack(
        data["text"], max_files=max_files, max_bytes=max_bytes, per_file_bytes=per_file_bytes,
    ))


# -- engineering planning (Phase 8.2) -----------------------------------------
#
# Planning only: never accepts a filesystem/DB path, trust level, lease/
# fencing field, checkpoint ref, worker authority, cloud authorization, or
# raw command from the client -- every field below is a bounded string, and
# the repository/planner are always the server's own configured project and
# configured Planner, never something the client selects.

@api.get("/plans")
def plans():
    limit, offset = page_args()
    return jsonify(service().list_plans(limit, offset))


@api.get("/plans/<plan_id>")
def plan_detail(plan_id):
    return jsonify(service().get_plan(plan_id))


@api.post("/plans")
def create_plan():
    data = body({"request": 32768}, ("request",))
    response = jsonify(service().create_plan(data))
    response.status_code = 201
    response.headers["Location"] = "/api/plans/" + response.get_json()["plan_id"]
    return response


@api.post("/plans/<plan_id>/resume")
def resume_plan(plan_id):
    body({})
    return jsonify(service().resume_plan(plan_id))


@api.post("/plans/<plan_id>/replan")
def replan_plan(plan_id):
    body({})
    return jsonify(service().replan_plan(plan_id))


@api.post("/plans/<plan_id>/resolutions")
def resolve_plan(plan_id):
    data = body(
        {"ambiguity_id": 200, "answer": 16384, "resolution_kind": 32},
        ("ambiguity_id", "answer", "resolution_kind"),
    )
    if data["resolution_kind"] not in ("FACT", "AUTHORIZATION"):
        raise APIError("invalid_resolution_kind", "Choose FACT or explicit AUTHORIZATION.")
    return jsonify(service().resolve_plan(plan_id, data))
