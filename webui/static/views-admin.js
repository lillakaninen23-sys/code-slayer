/** Runtime presentation only. Never derives identity, never contacts Ollama. */

import { escapeHTML } from "./views.js";

const PROVENANCE_CLASS = {
  CONFIG_BOUND: "provenance-config",
  LIVE_ATTESTED: "provenance-live",
  VERIFIED: "provenance-verified",
  OBSERVED: "provenance-observed",
  MISMATCH: "provenance-mismatch",
  UNREACHABLE: "provenance-unreachable",
  UNVERIFIED: "provenance-unverified",
};

export function provenanceClass(value) {
  return PROVENANCE_CLASS[value] || "provenance-unknown";
}

export function provenanceBadge(value) {
  return `<span class="badge provenance ${provenanceClass(value)}">${escapeHTML(value)}</span>`;
}

// H.3: administrative worker lifecycle -- a SEPARATE dimension from
// availability_state/provenance above. Backend-authoritative only:
// this module never decides whether archiving/reactivating is
// available, it only renders whatever lifecycle_state/archive_available/
// reactivate_available the backend already reported.
export function lifecycleBadgeClass(state) {
  return state === "ARCHIVED" ? "lifecycle-archived" : "lifecycle-active";
}

export function lifecycleBadge(state) {
  const value = state || "ACTIVE";
  return `<span class="badge lifecycle ${lifecycleBadgeClass(value)}">${escapeHTML(value)}</span>`;
}

export function renderSourcedValue(label, field) {
  const record = field && typeof field === "object" && !Array.isArray(field)
    ? field
    : { value: field };
  const measured = Object.hasOwn(record, "measured_by_ollama")
    ? `<span class="model-meta">measured_by_ollama: ${escapeHTML(record.measured_by_ollama)}</span>`
    : "";
  return `<div class="runtime-field"><span class="runtime-field-label">${escapeHTML(label)}</span><span class="runtime-field-value">${escapeHTML(record.value)}</span>${record.source ? provenanceBadge(record.source) : ""}${measured}</div>`;
}

export function renderOllamaServerTest(result, configuredOrigin) {
  if (!result) return "";
  const models = (result.models || [])
    .map((item) => `<li><code>${escapeHTML(item.name)}</code> <span class="muted-text">${escapeHTML(item.digest)}</span></li>`)
    .join("");
  const originLine = `<div class="model-meta">observed origin ${escapeHTML(result.origin)}</div>`;
  if (result.origin !== configuredOrigin) {
    return `<div class="runtime-live"><div class="section-label">SERVER TEST (UNBOUND)</div><p>This observation is not current evidence for the configured origin.</p>${originLine}<div class="model-meta">configured origin ${escapeHTML(configuredOrigin)}</div></div>`;
  }
  return `<div class="runtime-live"><div class="section-label">SERVER TEST (OBSERVATION)</div>${provenanceBadge(result.status)}${originLine}${result.runtime_version != null ? `<div class="model-meta">runtime_version ${escapeHTML(result.runtime_version)}</div>` : ""}<p class="muted-text">A server test is observation, not model identity approval.</p>${models ? `<ul class="runtime-model-list">${models}</ul>` : ""}</div>`;
}

function renderLiveServer(live) {
  if (!live) return "";
  const models = (live.models || [])
    .map((item) => `<li><code>${escapeHTML(item.name)}</code> <span class="muted-text">${escapeHTML(item.digest)}</span></li>`)
    .join("");
  return `<div class="runtime-live"><div class="section-label">LIVE ATTESTATION</div>${provenanceBadge(live.status)}${live.reason ? `<p>${escapeHTML(live.reason)}</p>` : ""}${live.runtime_version != null ? `<div class="model-meta">runtime_version ${escapeHTML(live.runtime_version)}</div>` : ""}${models ? `<ul class="runtime-model-list">${models}</ul>` : ""}</div>`;
}

function liveForConfiguredServer(server, attestation) {
  const observed = (attestation?.ollama_servers || []).find((item) => item.id === server.id);
  if (!observed?.live) return null;
  if (observed.origin !== server.origin) return null;
  return observed.live;
}

export function attestationForWorker(worker, attestation) {
  if (!worker || !attestation) return null;
  const observed = (attestation.workers || []).find((item) => item.worker_id === worker.worker_id);
  if (!observed?.attestation) return null;
  const configuredTag = worker.model_tag?.value ?? worker.model_tag;
  const observedTag = observed.model_tag?.value ?? observed.model_tag;
  if (observed.ollama_server_id !== worker.ollama_server_id) return null;
  if (configuredTag !== observedTag) return null;
  return observed.attestation;
}

export function clearRuntimeEvidence(runtimeState) {
  runtimeState.runtimeAttestation = null;
  runtimeState.runtimeServerTests = {};
  runtimeState.runtimeIdentityResults = {};
  runtimeState.runtimeReplacePending = null;
}

export function beginRuntimeObservation(runtimeState) {
  clearRuntimeEvidence(runtimeState);
}

export function invalidateServerTest(runtimeState, serverId) {
  const current = runtimeState.runtimeServerTests || {};
  if (!Object.hasOwn(current, serverId)) {
    runtimeState.runtimeServerTests = { ...current };
    return;
  }
  const next = { ...current };
  delete next[serverId];
  runtimeState.runtimeServerTests = next;
}

export function acceptServerTest(runtimeState, result) {
  if (!result || result.id == null) return;
  runtimeState.runtimeServerTests = {
    ...(runtimeState.runtimeServerTests || {}),
    [result.id]: result,
  };
}

export function acceptRuntimeSnapshot(runtimeState, snapshot) {
  clearRuntimeEvidence(runtimeState);
  runtimeState.runtime = snapshot;
  runtimeState.runtimeUnavailable = false;
}

export function rejectRuntimeSnapshot(runtimeState) {
  clearRuntimeEvidence(runtimeState);
  runtimeState.runtime = null;
  runtimeState.runtimeUnavailable = true;
}

export function acceptRuntimeAttest(runtimeState, result) {
  clearRuntimeEvidence(runtimeState);
  runtimeState.runtime = result;
  runtimeState.runtimeAttestation = result;
  runtimeState.runtimeUnavailable = false;
}

export function identityResultBindable(snapshot, workerId, result) {
  if (!snapshot || !workerId) return false;
  const worker = (snapshot.workers || []).find((item) => item.worker_id === workerId);
  if (!worker) return false;
  if (!result || !Object.hasOwn(result, "configured_digest")) return true;
  const configured = worker.approved_model_digest && typeof worker.approved_model_digest === "object"
    ? worker.approved_model_digest.value
    : worker.approved_model_digest;
  return result.configured_digest === configured;
}

export function acceptIdentityResult(runtimeState, workerId, result, snapshot) {
  clearRuntimeEvidence(runtimeState);
  runtimeState.runtime = snapshot;
  runtimeState.runtimeUnavailable = false;
  if (!identityResultBindable(snapshot, workerId, result)) return;
  runtimeState.runtimeIdentityResults = { [workerId]: result };
}

export function renderRuntimeServers(runtime, options = {}) {
  const servers = runtime?.ollama_servers;
  if (!Array.isArray(servers) || !servers.length) {
    return '<p class="muted-text">No Ollama servers configured.</p>';
  }
  const tests = options.tests || {};
  const busy = options.busy === true;
  return servers
    .map((server) => {
      const id = server.id;
      const live = liveForConfiguredServer(server, options.attestation);
      return `<div class="runtime-item" data-runtime-server="${escapeHTML(id)}"><div class="model-card-top"><div class="model-title">${escapeHTML(id)}</div>${provenanceBadge(server.origin_source)}</div><div class="model-meta">${escapeHTML(server.origin)}</div><div class="model-meta">origin_source ${escapeHTML(server.origin_source)}</div><button type="button" class="ghost small" data-runtime-test-server="${escapeHTML(id)}" ${busy ? "disabled" : ""}>Test</button>${renderLiveServer(live)}${renderOllamaServerTest(tests[id], server.origin)}</div>`;
    })
    .join("");
}

export function renderRuntimeAttestation(attestation) {
  if (!attestation) {
    return '<p class="muted-text">No live attestation in this session. Configured runtime state is not a live probe.</p>';
  }
  const rows = (attestation.workers || [])
    .map((worker) => `<div class="runtime-attest-row"><span>${escapeHTML(worker.worker_id)}</span>${worker.attestation ? provenanceBadge(worker.attestation.status) : ""}</div>`)
    .join("");
  return `<div><div class="section-label">LIVE ATTESTATION</div>${rows || '<p class="muted-text">No runtime workers in this attestation.</p>'}</div>`;
}

export function renderReplaceIdentityConfirm(workerId) {
  return `<div class="runtime-confirm" data-runtime-replace-panel="${escapeHTML(workerId)}"><p>This replaces the currently approved runtime identity.</p><p>Certificates are NOT transferred automatically.</p><p>Production eligibility must not be assumed.</p><p class="muted-text">This confirmation authorizes the fixed action only. It is not factual proof of the identity.</p><button type="button" class="small" data-runtime-replace-confirm="${escapeHTML(workerId)}">Confirm replace identity</button> <button type="button" class="ghost small" data-runtime-replace-cancel="${escapeHTML(workerId)}">Cancel</button></div>`;
}

export function renderRuntimeIdentityResult(result) {
  if (!result) return "";
  const mismatchKept = result.status === "MISMATCH"
    ? '<p>Approved identity was not replaced.</p>'
    : "";
  const transferred = Object.hasOwn(result, "certificates_transferred")
    ? `<p>certificates_transferred: ${escapeHTML(result.certificates_transferred)}</p>`
    : "";
  return `<div class="runtime-identity-result">${provenanceBadge(result.status)}${result.reason ? `<p>${escapeHTML(result.reason)}</p>` : ""}<div class="model-meta">configured_digest ${escapeHTML(result.configured_digest)}</div><div class="model-meta">observed_digest ${escapeHTML(result.observed_digest)}</div>${mismatchKept}${transferred}</div>`;
}

function renderWorkerAttestation(attestation) {
  if (!attestation) return "";
  return `<div class="runtime-live"><div class="section-label">LIVE ATTESTATION</div>${provenanceBadge(attestation.status)}<p>${escapeHTML(attestation.reason)}</p><div class="model-meta">configured_digest ${escapeHTML(attestation.configured_digest)}</div><div class="model-meta">observed_digest ${escapeHTML(attestation.observed_digest)}</div><div class="model-meta">configured_version ${escapeHTML(attestation.configured_version)}</div><div class="model-meta">observed_version ${escapeHTML(attestation.observed_version)}</div><div class="model-meta">fingerprint_source ${escapeHTML(attestation.fingerprint_source)}</div>${attestation.fingerprint_source ? provenanceBadge(attestation.fingerprint_source) : ""}</div>`;
}

export function renderRuntimeWorkers(runtime, options = {}) {
  const workers = runtime?.workers;
  if (!Array.isArray(workers) || !workers.length) {
    return '<p class="muted-text">No configured runtime workers. This list is not the model registry above.</p>';
  }
  const busy = options.busy === true;
  const identityResults = options.identityResults || {};
  const replacePending = options.replacePending;
  const registryIds = new Set(options.registryIds || []);
  const disabled = busy ? "disabled" : "";
  return workers
    .map((worker) => {
      const id = worker.worker_id;
      const correlation = registryIds.has(id)
        ? '<p class="muted-text">Also listed in the model registry (display correlation only; not evidence).</p>'
        : "";
      const confirm = replacePending === id ? renderReplaceIdentityConfirm(id) : "";
      const archived = worker.lifecycle_state === "ARCHIVED";
      const itemClass = archived ? "runtime-item lifecycle-deemphasized" : "runtime-item";
      const lifecycleAction = worker.reactivate_available
        ? `<button type="button" class="small" data-runtime-reactivate="${escapeHTML(id)}" ${disabled}>Reactivate</button>`
        : worker.archive_available
          ? `<button type="button" class="ghost small" data-runtime-archive="${escapeHTML(id)}" ${disabled}>Archive</button>`
          : "";
      return `<div class="${itemClass}" data-runtime-worker="${escapeHTML(id)}"><div class="model-card-top"><div class="model-title">${escapeHTML(id)}</div>${lifecycleBadge(worker.lifecycle_state)}</div><div class="model-meta">${escapeHTML(worker.kind)} · ${escapeHTML(worker.network_class)} · ${escapeHTML(worker.ollama_server_id)}</div><div class="model-meta">identity_approved: ${escapeHTML(worker.identity_approved)}</div>${renderSourcedValue("model_tag", worker.model_tag)}${renderSourcedValue("approved_model_digest", worker.approved_model_digest)}${renderSourcedValue("approved_runtime_version", worker.approved_runtime_version)}${renderSourcedValue("effective_context_tokens", worker.effective_context_tokens)}${renderSourcedValue("temperature", worker.temperature)}${renderSourcedValue("normalizer_id", worker.normalizer_id)}${renderSourcedValue("normalizer_version", worker.normalizer_version)}${correlation}${renderWorkerAttestation(attestationForWorker(worker, options.attestation))}${renderRuntimeIdentityResult(identityResults[id])}<div class="runtime-actions"><button type="button" class="small" data-runtime-approve="${escapeHTML(id)}" ${disabled}>Approve live identity</button><button type="button" class="ghost small" data-runtime-replace-ask="${escapeHTML(id)}" ${disabled}>Approve new identity</button>${lifecycleAction}</div>${confirm}</div>`;
    })
    .join("");
}

export function renderRuntimeServerOptions(servers) {
  const list = Array.isArray(servers) ? servers : [];
  if (!list.length) return '<option value="">No configured servers</option>';
  return list
    .map((server) => `<option value="${escapeHTML(server.id)}">${escapeHTML(server.id)}</option>`)
    .join("");
}

export function runtimeRegistrationPayload(fields) {
  const source = fields && typeof fields === "object" ? fields : {};
  const payload = {
    worker_id: source.worker_id,
    ollama_server_id: source.ollama_server_id,
    model_tag: source.model_tag,
  };
  const optional = [
    ["kind", source.kind],
    ["network_class", source.network_class],
    ["effective_context_tokens", source.effective_context_tokens],
    ["temperature", source.temperature],
    ["normalizer_id", source.normalizer_id],
    ["normalizer_version", source.normalizer_version],
  ];
  for (const [key, raw] of optional) {
    if (raw === undefined || raw === null) continue;
    const text = String(raw).trim();
    if (!text) continue;
    if (key === "effective_context_tokens" || key === "temperature" || key === "normalizer_version") {
      const number = Number(text);
      if (!Number.isFinite(number)) continue;
      payload[key] = number;
      continue;
    }
    payload[key] = text;
  }
  return payload;
}

const CERTIFICATION_STATE_CLASS = {
  READY: "cert-ready",
  INCOMPLETE: "cert-incomplete",
  QUEUED: "cert-queued",
  RUNNING: "cert-running",
  PASS: "cert-pass",
  FAIL: "cert-fail",
  HARD_DISQUALIFIED: "cert-hard",
  CERTIFIED: "cert-certified",
  FAILED: "cert-failed",
  NOT_CERTIFIED: "cert-not-certified",
  VERIFIED: "cert-runtime-verified",
  MISMATCH: "cert-runtime-mismatch",
  UNREACHABLE: "cert-runtime-unreachable",
  UNKNOWN: "cert-runtime-unknown",
  ELIGIBLE: "cert-eligible",
  BLOCKED: "cert-blocked",
  VALIDATION: "cert-validation",
  PRODUCTION: "cert-production",
};

export function certificationStateClass(value) {
  return CERTIFICATION_STATE_CLASS[value] || "cert-unknown";
}

export function certificationStateBadge(value) {
  if (value == null || value === "") return "";
  return `<span class="badge certification ${certificationStateClass(value)}">${escapeHTML(value)}</span>`;
}

export function plannerEligibilityLabel(eligibility) {
  return eligibility?.eligible === true ? "ELIGIBLE" : "BLOCKED";
}

export function isCertificationTerminal(state) {
  return ["PASS", "FAIL", "HARD_DISQUALIFIED", "INCOMPLETE"].includes(state);
}

export function isCertificationActive(state) {
  return ["QUEUED", "RUNNING"].includes(state);
}

export function shouldContinueCertificationPoll(run) {
  return isCertificationActive(run?.state);
}

export function certificationPollDelay(hidden) {
  return hidden ? 15000 : 2000;
}

export function certificationStartEnabled(worker, options = {}) {
  if (options.busy === true) return false;
  if (isCertificationActive(options.activeRun?.state)) return false;
  return worker?.ready_for_certification === true;
}

export function certificationPreflightEnabled(worker, options = {}) {
  if (options.busy === true) return false;
  if (isCertificationActive(options.activeRun?.state)) return false;
  if (worker?.lifecycle_state === "ARCHIVED") return false;
  return true;
}

export function certificationPromoteEnabled(worker, options = {}) {
  // Backend-authoritative only: `promotion_available` is a server
  // projection of durable state (never a live probe). The frontend
  // must never infer availability from baseline_security.status, run
  // state, or any other client-side heuristic -- a VALIDATION PASS
  // certificate alone does not mean promotion is still available (e.g.
  // it may already have been promoted).
  if (options.busy === true) return false;
  if (isCertificationActive(options.activeRun?.state)) return false;
  return worker?.promotion_available === true;
}

export function certificationPlannerStartEnabled(worker, options = {}) {
  if (options.busy === true) return false;
  if (isCertificationActive(options.activePlannerRun?.state)) return false;
  return worker?.planner_ready_for_certification === true;
}

export function certificationPlannerPreflightEnabled(worker, options = {}) {
  if (options.busy === true) return false;
  if (isCertificationActive(options.activePlannerRun?.state)) return false;
  if (worker?.lifecycle_state === "ARCHIVED") return false;
  return true;
}

export function certificationWorkerSelectEnabled(options = {}) {
  return options.busy !== true;
}

export function bindCertificationEvidence(evidence, runId) {
  if (!evidence || evidence.run_id !== runId) return null;
  return evidence;
}

export function clearCertificationTransient(certState) {
  certState.selectedCertificationWorker = null;
  certState.certificationActiveRun = null;
  certState.certificationActivePlannerRun = null;
  certState.certificationHistory = null;
  certState.certificationEvidence = null;
  certState.certificationEvidenceError = null;
  certState.certificationEvidenceRequestId = null;
}

export function beginCertificationPreflight(certState) {
  certState.certificationActiveRun = null;
  certState.certificationEvidence = null;
  certState.certificationEvidenceError = null;
  certState.certificationEvidenceRequestId = null;
}

export function rejectCertificationSnapshot(certState) {
  certState.certificationWorkers = null;
  certState.certificationEnvironment = null;
  certState.certificationUnavailable = true;
  clearCertificationTransient(certState);
}

export function acceptCertificationWorkers(certState, payload) {
  certState.certificationWorkers = Array.isArray(payload?.workers) ? payload.workers : [];
  certState.certificationEnvironment = payload?.environment || null;
  certState.certificationUnavailable = false;
}

export function selectCertificationWorkerId(certState, workerId) {
  certState.selectedCertificationWorkerId = workerId;
  certState.certificationSelectionVersion = (certState.certificationSelectionVersion || 0) + 1;
  clearCertificationTransient(certState);
  return certState.certificationSelectionVersion;
}

export function certificationSelectionMatches(certState, workerId, version) {
  return certState.selectedCertificationWorkerId === workerId
    && (version == null || version === certState.certificationSelectionVersion);
}

export function acceptCertificationWorker(certState, workerId, detail, version) {
  if (!certificationSelectionMatches(certState, workerId, version)) return false;
  certState.selectedCertificationWorker = detail;
  certState.certificationHistory = detail?.history || null;
  certState.certificationUnavailable = false;
  return true;
}

export function beginCertificationRun(certState, run) {
  certState.certificationActiveRun = run && run.run_id ? run : null;
  certState.certificationEvidence = null;
  certState.certificationEvidenceError = null;
  certState.certificationEvidenceRequestId = null;
}

export function acceptCertificationStart(certState, workerId, version, run) {
  if (!certificationSelectionMatches(certState, workerId, version)) return false;
  if (!run || !run.run_id) {
    certState.certificationActiveRun = null;
    return false;
  }
  certState.certificationActiveRun = run;
  certState.certificationEvidence = null;
  certState.certificationEvidenceError = null;
  certState.certificationEvidenceRequestId = null;
  return true;
}

export function acceptCertificationPreflight(certState, workerId, version) {
  return certificationSelectionMatches(certState, workerId, version);
}

export function acceptCertificationPromotion(certState, workerId, version) {
  return certificationSelectionMatches(certState, workerId, version);
}

export function applyCertificationPoll(certState, run) {
  const expected = certState.certificationActiveRun?.run_id;
  if (!expected || !run || run.run_id !== expected) return false;
  certState.certificationActiveRun = run;
  return true;
}

// -- Planner certification run state -- kept in its own state slice so
// a Baseline Security run and a Planner run for the same worker can
// never be confused with each other, per H.2's "visually and
// semantically separate" requirement.

export function beginCertificationPlannerPreflight(certState) {
  certState.certificationActivePlannerRun = null;
}

export function acceptCertificationPlannerPreflight(certState, workerId, version) {
  return certificationSelectionMatches(certState, workerId, version);
}

export function beginCertificationPlannerRun(certState, run) {
  certState.certificationActivePlannerRun = run && run.run_id ? run : null;
}

export function acceptCertificationPlannerStart(certState, workerId, version, run) {
  if (!certificationSelectionMatches(certState, workerId, version)) return false;
  if (!run || !run.run_id) {
    certState.certificationActivePlannerRun = null;
    return false;
  }
  certState.certificationActivePlannerRun = run;
  return true;
}

export function applyCertificationPlannerPoll(certState, run) {
  const expected = certState.certificationActivePlannerRun?.run_id;
  if (!expected || !run || run.run_id !== expected) return false;
  certState.certificationActivePlannerRun = run;
  return true;
}

export function beginCertificationEvidenceRequest(certState, runId) {
  certState.certificationEvidence = null;
  certState.certificationEvidenceError = null;
  certState.certificationEvidenceRequestId = runId;
}

export function acceptCertificationEvidence(certState, evidence, runId) {
  if (certState.certificationEvidenceRequestId !== runId) return false;
  const bound = bindCertificationEvidence(evidence, runId);
  if (!bound) {
    certState.certificationEvidence = null;
    return false;
  }
  certState.certificationEvidence = bound;
  certState.certificationEvidenceError = null;
  return true;
}

export function rejectCertificationEvidence(certState, runId, message) {
  if (runId && certState.certificationEvidenceRequestId !== runId) return false;
  certState.certificationEvidence = null;
  certState.certificationEvidenceError = message || "Evidence is unavailable.";
  return true;
}

export function renderCertificationEligibility(eligibility) {
  if (!eligibility) {
    return `<div class="cert-panel"><div class="section-label">Production eligibility — Planner</div><p class="muted-text">No production eligibility result was returned.</p></div>`;
  }
  const label = plannerEligibilityLabel(eligibility);
  return `<div class="cert-panel" data-cert-eligibility><div class="section-label">Production eligibility — Planner</div>${certificationStateBadge(label)}<div class="model-meta">eligible ${escapeHTML(eligibility.eligible)}</div><div class="model-meta">reason ${escapeHTML(eligibility.reason)}</div><div class="model-meta">source ${escapeHTML(eligibility.source)}</div><div class="model-meta">security_certificate_id ${escapeHTML(eligibility.security_certificate_id)}</div><div class="model-meta">role_certificate_id ${escapeHTML(eligibility.role_certificate_id)}</div><p class="muted-text">This is the server evaluator result for Planner. It is not inferred from validation certificates or runtime status.</p></div>`;
}

export function renderCertificationRoles(roles, futureActions) {
  const entries = roles && typeof roles === "object" ? Object.entries(roles) : [];
  const rows = entries
    .map(([role, status]) => {
      const record = status && typeof status === "object" ? status : {};
      return `<div class="cert-role-row" data-cert-role="${escapeHTML(role)}"><div class="model-card-top"><div class="model-title">${escapeHTML(role)}</div>${certificationStateBadge(record.status)}</div><div class="model-meta">PRODUCTION role certificate</div><div class="model-meta">outcome ${escapeHTML(record.outcome)}</div><div class="model-meta">certificate_id ${escapeHTML(record.certificate_id)}</div></div>`;
    })
    .join("");
  const others = (futureActions || []).filter((item) => item && item.role !== "PLANNER");
  const otherNote = others.length && others.every((item) => item.available === false)
    ? '<p class="muted-text">Live role certification not available in v1 for this role.</p>'
    : "";
  return `<div class="cert-panel"><div class="section-label">Role certificates</div><p class="muted-text">Role certificates are PRODUCTION state. They are not Baseline Security VALIDATION certificates.</p>${rows || '<p class="muted-text">No role certificate states returned.</p>'}${otherNote}</div>`;
}

function renderCertificationChecks(checks) {
  if (!Array.isArray(checks) || !checks.length) return '<p class="muted-text">No checks returned.</p>';
  return checks
    .map((check) => `<div class="cert-check"><code>${escapeHTML(check.name)}</code> ${check.ok === true ? "ok" : "not ok"}<div class="model-meta">${escapeHTML(check.detail)}</div></div>`)
    .join("");
}

export function renderCertificationPreflight(preflight) {
  if (!preflight) return '<p class="muted-text">No last preflight in this projection.</p>';
  return `<div class="cert-panel"><div class="section-label">Last preflight</div><div class="model-meta">run_id ${escapeHTML(preflight.run_id)}</div>${certificationStateBadge(preflight.state)}<div class="model-meta">ready ${escapeHTML(preflight.ready)}</div>${renderCertificationChecks(preflight.checks)}<p class="muted-text">Readiness is the backend ready / ready_for_certification fields, not a browser inference from check rows.</p></div>`;
}

export function renderCertificationRun(run) {
  if (!run) {
    return '<p class="muted-text">No active certification run in this browser session. Closing this browser does not cancel a durable run.</p>';
  }
  return `<div class="cert-panel" data-cert-run="${escapeHTML(run.run_id)}"><div class="section-label">Certification run</div><p class="muted-text">A run is not a certificate.</p><div class="model-meta">run_id ${escapeHTML(run.run_id)}</div>${certificationStateBadge(run.state)}<div class="model-meta">kind ${escapeHTML(run.kind)}</div>${certificationStateBadge(run.environment)}<div class="model-meta">reason ${escapeHTML(run.reason)}</div><div class="model-meta">model_tag ${escapeHTML(run.model_tag)}</div><div class="model-meta">model_digest ${escapeHTML(run.model_digest)}</div><div class="model-meta">ollama_root ${escapeHTML(run.ollama_root)}</div><div class="model-meta">runtime_identity_fingerprint ${escapeHTML(run.runtime_identity_fingerprint)}</div><div class="model-meta">certificate_id ${escapeHTML(run.certificate_id)}</div><div class="model-meta">evidence_ref ${escapeHTML(run.evidence_ref)}</div><div class="model-meta">hard_disqualifiers ${escapeHTML(JSON.stringify(run.hard_disqualifiers))}</div><div class="model-meta">has_certificate ${escapeHTML(run.has_certificate)}</div><div class="model-meta">progress ${escapeHTML(JSON.stringify(run.progress))}</div>${run.evidence_ref ? `<button type="button" class="ghost small" data-cert-evidence="${escapeHTML(run.run_id)}">View evidence</button>` : ""}<p class="muted-text">has_certificate is the backend field. INCOMPLETE is not a certificate.</p></div>`;
}

export function renderCertificationHistory(history) {
  if (!history) return '<p class="muted-text">No certification history loaded.</p>';
  const runs = Array.isArray(history.runs) ? history.runs : [];
  const validation = Array.isArray(history.validation_certificates) ? history.validation_certificates : [];
  const production = Array.isArray(history.production_certificates) ? history.production_certificates : [];
  const runRows = runs
    .map((run) => `<div class="cert-history-row" data-cert-history-run="${escapeHTML(run.run_id)}"><div class="model-card-top"><div class="model-title">RUN ${escapeHTML(run.run_id)}</div>${certificationStateBadge(run.state)}</div><div class="model-meta">kind ${escapeHTML(run.kind)}</div><div class="model-meta">A run is not a certificate.</div><div class="model-meta">has_certificate ${escapeHTML(run.has_certificate)}</div>${run.evidence_ref ? `<button type="button" class="ghost small" data-cert-evidence="${escapeHTML(run.run_id)}">View evidence</button>` : ""}</div>`)
    .join("");
  const certRows = (items, environment) => items
    .map((item) => `<div class="cert-history-row"><div class="model-card-top"><div class="model-title">CERTIFICATE ${escapeHTML(item.certificate_id)}</div>${certificationStateBadge(environment)}</div>${certificationStateBadge(item.outcome)}<div class="model-meta">issued_at ${escapeHTML(item.issued_at)}</div><div class="model-meta">Historical evidence is not proof of current production eligibility.</div></div>`)
    .join("");
  return `<div class="cert-panel"><div class="section-label">Certification history</div><div class="cert-history-group"><div class="section-label">Certification runs</div>${runRows || '<p class="muted-text">No certification runs.</p>'}</div><div class="cert-history-group"><div class="section-label">VALIDATION certificates</div>${certRows(validation, "VALIDATION") || '<p class="muted-text">No VALIDATION certificates.</p>'}</div><div class="cert-history-group"><div class="section-label">PRODUCTION certificates</div>${certRows(production, "PRODUCTION") || '<p class="muted-text">No PRODUCTION certificates.</p>'}</div></div>`;
}

export function renderCertificationEvidence(evidence, error) {
  if (error) {
    return `<div class="cert-panel"><div class="section-label">Verified evidence</div><p class="notice error">${escapeHTML(error)}</p></div>`;
  }
  if (!evidence) return "";
  let document = "";
  try {
    document = JSON.stringify(evidence.document, null, 2);
  } catch {
    document = String(evidence.document);
  }
  return `<div class="cert-panel" data-cert-evidence-run="${escapeHTML(evidence.run_id)}"><div class="section-label">Verified evidence</div><div class="model-meta">run_id ${escapeHTML(evidence.run_id)}</div>${certificationStateBadge(evidence.environment)}<div class="model-meta">evidence_ref ${escapeHTML(evidence.evidence_ref)}</div><pre>${escapeHTML(document)}</pre><p class="muted-text">The browser does not interpret evidence into a certification verdict.</p></div>`;
}

export function renderCertificationWorkerSummary(worker, selectedId, environment, options = {}) {
  if (!worker) return "";
  const selected = worker.worker_id === selectedId ? " selected" : "";
  const eligibility = plannerEligibilityLabel(worker.production_eligibility);
  const roles = worker.roles && typeof worker.roles === "object"
    ? Object.entries(worker.roles).map(([role, status]) => `${escapeHTML(role)} ${escapeHTML(status?.status)}`).join(" · ")
    : "";
  const disabled = certificationWorkerSelectEnabled(options) ? "" : "disabled";
  return `<button type="button" class="cert-item${selected}" data-cert-worker="${escapeHTML(worker.worker_id)}" ${disabled}><div class="model-card-top"><div class="model-title">${escapeHTML(worker.worker_id)}</div>${lifecycleBadge(worker.lifecycle_state)}${certificationStateBadge(environment || worker.environment)}</div><div class="model-meta">${escapeHTML(worker.kind)} · ${escapeHTML(worker.network_class)}</div><div class="model-meta">runtime ${escapeHTML(worker.runtime?.status)} ${escapeHTML(worker.runtime?.reason)}</div>${certificationStateBadge(worker.runtime?.status)}<div class="model-meta">Baseline Security VALIDATION ${escapeHTML(worker.baseline_security?.status)} outcome ${escapeHTML(worker.baseline_security?.outcome)} environment ${escapeHTML(worker.baseline_security?.environment)}</div>${certificationStateBadge(worker.baseline_security?.status)}<div class="model-meta">roles ${roles}</div><div class="model-meta">Production eligibility — Planner</div>${certificationStateBadge(eligibility)}<div class="model-meta">${escapeHTML(worker.production_eligibility?.reason)}</div><div class="model-meta">source ${escapeHTML(worker.production_eligibility?.source)}</div><div class="model-meta">ready_for_certification ${escapeHTML(worker.ready_for_certification)}</div></button>`;
}

export function renderCertificationWorkers(payload, selectedId, options = {}) {
  const workers = payload?.workers;
  if (!Array.isArray(workers) || !workers.length) {
    return '<p class="muted-text">No certification workers in this projection.</p>';
  }
  return workers
    .map((worker) => renderCertificationWorkerSummary(worker, selectedId, payload.environment, options))
    .join("");
}

function renderCertificationIdentity(identity) {
  if (!identity) return '<p class="muted-text">No certification identity projection for this worker.</p>';
  const fields = [
    "model_tag",
    "model_digest",
    "runtime_identity_fingerprint",
    "endpoint",
    "ollama_root",
    "runtime_version",
    "normalizer_id",
    "normalizer_version",
    "effective_context_tokens",
    "temperature",
  ];
  return `<div class="cert-panel"><div class="section-label">Certification identity</div><p class="muted-text">Display only. These fields are not editable and are not a live Ollama probe from this browser.</p>${fields.map((name) => renderSourcedValue(name, identity[name])).join("")}</div>`;
}

export function renderCertificationWorkerDetail(worker, options = {}) {
  if (!worker) {
    return '<p class="muted-text">Select a certification worker.</p>';
  }
  const startEnabled = certificationStartEnabled(worker, options);
  const preflightEnabled = certificationPreflightEnabled(worker, options);
  const promoteEnabled = certificationPromoteEnabled(worker, options);
  const plannerPreflightEnabled = certificationPlannerPreflightEnabled(worker, options);
  const plannerStartEnabled = certificationPlannerStartEnabled(worker, options);
  const correlation = (options.registryIds || []).includes(worker.worker_id)
    ? '<p class="muted-text">Also listed in the model registry (display correlation only; not evidence).</p>'
    : "";
  return `<div class="cert-panel" data-cert-detail="${escapeHTML(worker.worker_id)}"><div class="model-card-top"><div class="model-title">${escapeHTML(worker.worker_id)}</div>${lifecycleBadge(worker.lifecycle_state)}${certificationStateBadge(worker.environment)}</div>${correlation}<div class="model-meta">${escapeHTML(worker.kind)} · ${escapeHTML(worker.network_class)}</div><div class="section-label">Runtime / preflight</div>${certificationStateBadge(worker.runtime?.status)}<div class="model-meta">${escapeHTML(worker.runtime?.reason)}</div><div class="section-label">Baseline Security</div><p class="muted-text">VALIDATION certificate status. This does not write production state.</p>${certificationStateBadge(worker.baseline_security?.status)}<div class="model-meta">outcome ${escapeHTML(worker.baseline_security?.outcome)}</div><div class="model-meta">environment ${escapeHTML(worker.baseline_security?.environment)}</div>${renderCertificationIdentity(worker.identity)}${renderCertificationPreflight(worker.last_preflight)}${renderCertificationRoles(worker.roles, worker.future_actions)}${renderCertificationEligibility(worker.production_eligibility)}<div class="model-meta">ready_for_certification ${escapeHTML(worker.ready_for_certification)}</div><div class="cert-actions"><button type="button" class="ghost small" data-cert-preflight="${escapeHTML(worker.worker_id)}" ${preflightEnabled ? "" : "disabled"}>Run Baseline Security preflight</button><button type="button" class="small" data-cert-start="${escapeHTML(worker.worker_id)}" ${startEnabled ? "" : "disabled"}>Start Baseline Security certification</button></div><p class="muted-text">Preflight probes runtime and writes durable READY or INCOMPLETE state. It does not start certification. Closing this browser does not cancel a durable run.</p><div class="section-label">Production Baseline Security</div><p class="muted-text">Promotion re-verifies the current VALIDATION PASS certificate against the live runtime and durable evidence, then durably records a SEPARATE PRODUCTION certificate. It never grants trust, permission, or a role certificate.</p><div class="model-meta" data-cert-promotion-available>promotion_available ${escapeHTML(worker.promotion_available)}</div><div class="model-meta" data-cert-promotion-reason>reason ${escapeHTML(worker.promotion_reason)}</div><div class="cert-actions"><button type="button" class="small" data-cert-promote="${escapeHTML(worker.worker_id)}" ${promoteEnabled ? "" : "disabled"}>Promote Baseline Security to PRODUCTION</button></div><p class="muted-text">This button reflects the backend's own promotion_available projection only. The browser never decides promotability itself.</p><div class="section-label">Planner role certification</div><p class="muted-text">A separate durable job/run track from Baseline Security. A PASS records a PRODUCTION Planner role certificate directly -- there is no promote step here. This is also separate from the "Role certificates" panel above, which shows the resulting certificate, not the run.</p>${renderCertificationPreflight(worker.planner_last_preflight)}<div class="model-meta">planner_ready_for_certification ${escapeHTML(worker.planner_ready_for_certification)}</div><div class="cert-actions"><button type="button" class="ghost small" data-cert-planner-preflight="${escapeHTML(worker.worker_id)}" ${plannerPreflightEnabled ? "" : "disabled"}>Run Planner preflight</button><button type="button" class="small" data-cert-planner-start="${escapeHTML(worker.worker_id)}" ${plannerStartEnabled ? "" : "disabled"}>Start Planner certification</button></div><p class="muted-text">Closing this browser does not cancel a durable Planner run.</p></div>`;
}


/* System / deployment and Tailscale presentation. Backend fields remain authoritative. */
function adminField(label,value){return `<div class="runtime-field"><span class="runtime-field-label">${escapeHTML(label)}</span><span class="runtime-field-value">${escapeHTML(value)}</span></div>`;}
function adminSourceField(label,value,source){return `<div class="runtime-field"><span class="runtime-field-label">${escapeHTML(label)}</span><span class="runtime-field-value">${escapeHTML(value)}</span>${source?provenanceBadge(source):""}</div>`;}
function adminJSON(value){let out;try{out=JSON.stringify(value,null,2);}catch{out=String(value);}return `<pre>${escapeHTML(out)}</pre>`;}
export function beginSystemRequest(x){x.systemRequestVersion=(x.systemRequestVersion||0)+1;return x.systemRequestVersion;}
export function acceptSystemSnapshot(x,v,n){if(n!==x.systemRequestVersion)return false;x.system=v;x.systemUnavailable=false;return true;}
export function rejectSystemSnapshot(x,n=null){if(n!=null&&n!==x.systemRequestVersion)return false;x.system=null;x.systemUnavailable=true;x.systemRequestVersion=(x.systemRequestVersion||0)+1;return true;}
export function beginTailscaleRequest(x){x.tailscaleRequestVersion=(x.tailscaleRequestVersion||0)+1;return x.tailscaleRequestVersion;}
export function acceptTailscaleSnapshot(x,v,n){if(n!==x.tailscaleRequestVersion)return false;x.tailscale=v;x.tailscaleUnavailable=false;return true;}
export function rejectTailscaleSnapshot(x,n=null){if(n!=null&&n!==x.tailscaleRequestVersion)return false;x.tailscale=null;x.tailscaleUnavailable=true;x.tailscaleRequestVersion=(x.tailscaleRequestVersion||0)+1;return true;}
function renderSystemOperation(op){if(!op)return "";const d=op.detail?`<p class="muted-text">${escapeHTML(op.detail)}</p>`:"";const r=Object.hasOwn(op,"result")?`<div class="section-label">Operation response — not current system state</div>${adminJSON(op.result)}`:"";return `<div class="runtime-live"><div class="section-label">${escapeHTML(op.kind)} operation</div><span class="badge muted">${escapeHTML(op.status)}</span>${d}${r}</div>`;}
export function renderSystemSettings(system,o={}){
 const busy=o.busy===true,cur=!!system;
 const body=system?`<div class="section-label">SERVICE</div>${adminField("state",system.service?.state)}${adminField("running",system.service?.running)}${adminField("unit",system.service?.unit)}${adminField("source",system.service?.source)}${adminField("version",system.service?.version)}<div class="section-label">PROCESS</div>${adminField("process_commit",system.service?.process_commit)}${adminField("process_commit_source",system.service?.process_commit_source)}${adminField("process_source_dirty",system.service?.process_source_dirty)}${adminField("process_source_state",system.service?.process_source_state)}${adminField("running_commit",system.service?.running_commit)}${adminField("running_commit_source",system.service?.running_commit_source)}<div class="section-label">CHECKOUT</div>${adminField("checkout_head",system.service?.checkout_head)}${adminField("checkout_head_source",system.service?.checkout_head_source)}${adminField("checkout_source_dirty",system.service?.checkout_source_dirty)}${adminField("checkout_source_state",system.service?.checkout_source_state)}${adminField("checkout_source_state_source",system.service?.checkout_source_state_source)}<div class="section-label">DEPLOYMENT — BACKEND VERDICT</div>${adminField("deployment_status",system.service?.deployment_status)}${adminField("deployment_complete",system.service?.deployment_complete)}${adminField("uptime_seconds",system.service?.uptime_seconds)}<p class="muted-text">The browser displays these backend fields verbatim and does not compare commits to derive a verdict.</p><div class="section-label">NETWORK</div>${adminField("local_url",system.network?.local_url)}${adminField("bind_host",system.network?.bind_host)}${adminField("bind_port",system.network?.bind_port)}<div class="section-label">HEALTH</div>${adminField("status",system.health?.status)}${adminField("schema_version",system.health?.schema_version)}${adminField("source",system.health?.source)}`:`<p class="notice error">${escapeHTML(o.error||"Current system state is unavailable. Prior state is not current evidence.")}</p>`;
 const check=o.updateCheck?`<div class="runtime-live"><div class="section-label">Update check result</div>${adminJSON(o.updateCheck)}<p class="muted-text">This is an explicit update-check result, not deployment proof.</p></div>`:"";
 let confirm="";if(o.confirm==="apply")confirm='<div class="runtime-confirm"><p>Apply the backend-selected update? The operation may restart the service. A returned apply response is not proof that deployment completed.</p><button type="button" class="small" id="system-update-apply-confirm">Confirm apply update</button> <button type="button" class="ghost small" id="system-confirm-cancel">Cancel</button></div>';else if(o.confirm==="restart")confirm='<div class="runtime-confirm"><p>Restart the CSLR service? The connection may disappear before a response is received.</p><button type="button" class="small" id="system-restart-confirm">Confirm restart</button> <button type="button" class="ghost small" id="system-confirm-cancel">Cancel</button></div>';
 return `${body}${check}${renderSystemOperation(o.operation)}<div class="cert-actions"><button type="button" class="ghost small" id="system-refresh" ${busy?"disabled":""}>Refresh current state</button><button type="button" class="ghost small" id="system-update-check" ${busy||!cur?"disabled":""}>Check for update</button><button type="button" class="ghost small" id="system-update-apply-ask" ${busy||!cur?"disabled":""}>Apply update…</button><button type="button" class="ghost small" id="system-restart-ask" ${busy||!cur?"disabled":""}>Restart service…</button></div>${confirm}`;
}
export function renderTailscaleSettings(t,o={}){
 const publicIngressFlag=["fun","nel_detected"].join("");
 const busy=o.busy===true,cur=!!t;
 const body=t?`<div class="section-label">NODE</div>${adminSourceField("state",t.node?.state,t.node?.source)}<div class="section-label">SERVE</div>${adminSourceField("status",t.serve?.status,t.serve?.source)}${adminField("expected_backend",t.serve?.expected_backend)}${adminField("observed_backend",t.serve?.observed_backend)}${adminField(publicIngressFlag,t.serve?.[publicIngressFlag])}<div class="runtime-field"><span class="runtime-field-label">hosts</span></div>${adminJSON(t.serve?.hosts)}${t.serve?.[publicIngressFlag]===true?'<p class="notice error">'+escapeHTML(publicIngressFlag)+' is true. Backend reports public ingress; CSLR exposes no control for this mode.</p>':""}<div class="section-label">HOST</div>${adminSourceField("name",t.host?.name,t.host?.source)}${adminSourceField("accepted",t.host?.accepted,t.host?.accepted_source)}<div class="section-label">INTENT</div>${adminSourceField("enabled",t.intent?.enabled,t.intent?.enabled_source)}${adminField("alignment",t.intent?.alignment)}<div class="section-label">REMOTE ACCESS</div>${adminField("remote_access",t.remote_access)}${adminField("url",t.url)}${adminField("backend",t.backend)}${adminSourceField("enabled",t.enabled,t.enabled_source)}${adminField("alignment",t.alignment)}${adminField("detail",t.detail)}${adminField("source",t.source)}<p class="muted-text">remote_access VERIFIED is only the backend's network/Serve/Host-path verdict. It is not proof that this browser origin or page session is verified. Intent enabled, Serve status, and remote_access are distinct fields.</p>`:`<p class="notice error">${escapeHTML(o.error||"Current Tailscale state is unavailable. Prior state is not current evidence.")}</p>`;
 const confirm=o.confirm==="disable"?'<div class="runtime-confirm"><p>Disable CSLR Tailscale Serve intent? The backend will only reset the exact CSLR topology it recognizes.</p><button type="button" class="small" id="tailscale-disable-confirm">Confirm disable</button> <button type="button" class="ghost small" id="tailscale-confirm-cancel">Cancel</button></div>':"";
 return `${body}<div class="cert-actions"><button type="button" class="ghost small" id="tailscale-refresh" ${busy?"disabled":""}>Refresh current state</button><button type="button" class="ghost small" id="tailscale-enable" ${busy||!cur?"disabled":""}>Enable Serve</button><button type="button" class="ghost small" id="tailscale-disable-ask" ${busy||!cur?"disabled":""}>Disable Serve…</button></div>${confirm}`;
}


/* Dashboard read-only summaries. These snapshots never replace admin-view state. */
export function beginDashboardSummaryRequest(x) {
  x.dashboardSummaryVersion = (x.dashboardSummaryVersion || 0) + 1;
  return x.dashboardSummaryVersion;
}

export function acceptDashboardSummary(x, version, snapshot, errors = {}) {
  if (version !== x.dashboardSummaryVersion) return false;
  x.dashboardSummary = snapshot;
  x.dashboardSummaryErrors = errors;
  x.dashboardSummaryUnavailable = false;
  return true;
}

export function rejectDashboardSummary(x, version = null, message = "Dashboard summaries unavailable.") {
  if (version != null && version !== x.dashboardSummaryVersion) return false;
  x.dashboardSummary = null;
  x.dashboardSummaryErrors = {
    system: message,
    runtime: message,
    certification: message,
    tailscale: message,
  };
  x.dashboardSummaryUnavailable = true;
  x.dashboardSummaryVersion = (x.dashboardSummaryVersion || 0) + 1;
  return true;
}

function dashboardUnavailable(error) {
  return `<div class="metric-value">UNAVAILABLE</div><div class="metric-foot">${escapeHTML(error || "No current snapshot.")}</div>`;
}

export function renderDashboardSystemSummary(system, error) {
  if (!system) return dashboardUnavailable(error);
  return `<div class="metric-value">${escapeHTML(system.service?.deployment_status)}</div><div class="metric-foot">deployment_complete ${escapeHTML(system.service?.deployment_complete)}</div><div class="metric-foot">process ${escapeHTML(system.service?.process_commit)}</div><div class="metric-foot">checkout ${escapeHTML(system.service?.checkout_head)}</div>`;
}

export function renderDashboardRuntimeSummary(runtime, error) {
  if (!runtime) return dashboardUnavailable(error);
  const workers = Array.isArray(runtime.workers) ? runtime.workers.length : 0;
  const servers = Array.isArray(runtime.ollama_servers) ? runtime.ollama_servers.length : 0;
  return `<div class="metric-value">${escapeHTML(workers)} workers</div><div class="metric-foot">${escapeHTML(servers)} Ollama servers</div><div class="metric-foot">GET /api/runtime · configuration only; not live attestation.</div>`;
}

export function renderDashboardCertificationSummary(certification, error) {
  if (!certification) return dashboardUnavailable(error);
  const workers = Array.isArray(certification.workers) ? certification.workers.length : 0;
  return `<div class="metric-value">${escapeHTML(certification.environment)}</div><div class="metric-foot">${escapeHTML(workers)} certification workers</div><div class="metric-foot">Backend projection only. Dashboard does not derive Planner eligibility.</div>`;
}

export function renderDashboardTailscaleSummary(tailscale, error) {
  if (!tailscale) return dashboardUnavailable(error);
  return `<div class="metric-value">${escapeHTML(tailscale.remote_access)}</div><div class="metric-foot">Serve ${escapeHTML(tailscale.serve?.status)}</div><div class="metric-foot">intent enabled ${escapeHTML(tailscale.intent?.enabled)} · alignment ${escapeHTML(tailscale.intent?.alignment)}</div><div class="metric-foot">Backend network/Serve/Host-path verdict only.</div>`;
}
