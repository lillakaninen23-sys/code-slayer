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

export function acceptIdentityResult(runtimeState, workerId, result, snapshot) {
  clearRuntimeEvidence(runtimeState);
  runtimeState.runtime = snapshot;
  runtimeState.runtimeIdentityResults = { [workerId]: result };
  runtimeState.runtimeUnavailable = false;
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
      return `<div class="runtime-item" data-runtime-worker="${escapeHTML(id)}"><div class="model-card-top"><div class="model-title">${escapeHTML(id)}</div></div><div class="model-meta">${escapeHTML(worker.kind)} · ${escapeHTML(worker.network_class)} · ${escapeHTML(worker.ollama_server_id)}</div><div class="model-meta">identity_approved: ${escapeHTML(worker.identity_approved)}</div>${renderSourcedValue("model_tag", worker.model_tag)}${renderSourcedValue("approved_model_digest", worker.approved_model_digest)}${renderSourcedValue("approved_runtime_version", worker.approved_runtime_version)}${renderSourcedValue("effective_context_tokens", worker.effective_context_tokens)}${renderSourcedValue("temperature", worker.temperature)}${renderSourcedValue("normalizer_id", worker.normalizer_id)}${renderSourcedValue("normalizer_version", worker.normalizer_version)}${correlation}${renderWorkerAttestation(attestationForWorker(worker, options.attestation))}${renderRuntimeIdentityResult(identityResults[id])}<div class="runtime-actions"><button type="button" class="small" data-runtime-approve="${escapeHTML(id)}" ${disabled}>Approve live identity</button><button type="button" class="ghost small" data-runtime-replace-ask="${escapeHTML(id)}" ${disabled}>Approve new identity</button></div>${confirm}</div>`;
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
