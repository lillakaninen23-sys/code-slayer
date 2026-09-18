const app = document.getElementById("app");

function badge(text, kind) {
  return `<span class="badge ${kind || "muted"}">${escapeHtml(text)}</span>`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => {
    if (ch === "&") return "&" + "amp;";
    if (ch === "<") return "&" + "lt;";
    if (ch === ">") return "&" + "gt;";
    if (ch === '"') return "&" + "quot;";
    return "&#39;";
  });
}

async function api(path, options) {
  const response = await fetch(path, {
    ...options,
    headers: {
      Accept: "application/json",
      ...(options && options.body ? { "Content-Type": "application/json" } : {}),
      ...(options && options.headers),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const err = new Error((data.error && data.error.code) || "request_failed");
    err.payload = data;
    err.status = response.status;
    throw err;
  }
  return data;
}

function statusKind(status) {
  if (
    status === "CERTIFIED" ||
    status === "ELIGIBLE" ||
    status === "PASS" ||
    status === "VERIFIED" ||
    status === "running" ||
    status === "Connected" ||
    status === "LIVE_ATTESTED" ||
    status === "ok"
  ) return "ok";
  if (
    status === "FAILED" ||
    status === "BLOCKED" ||
    status === "HARD_DISQUALIFIED" ||
    status === "MISMATCH" ||
    status === "UNREACHABLE" ||
    status === "ERROR" ||
    status === "failed" ||
    status === "inactive"
  ) return "bad";
  if (status === "READY" || status === "RUNNING" || status === "QUEUED" || status === "OBSERVED") return "warn";
  return "muted";
}

function setNav(name) {
  document.querySelectorAll("nav a").forEach((link) => {
    link.classList.toggle("active", link.getAttribute("data-nav") === name);
  });
}

function route() {
  const hash = location.hash.replace(/^#/, "") || "/system";
  const parts = hash.split("/").filter(Boolean);
  const section = parts[0] || "system";
  setNav(section);
  if (section === "system") return renderSystem();
  if (section === "runtime") return renderRuntime();
  if (section === "tailscale") return renderTailscale();
  if (section === "certification") {
    if (parts[1] === "runs" && parts[2]) return renderRun(parts[2]);
    if (parts[1] && parts[1] !== "runs") return renderWorker(decodeURIComponent(parts[1]));
    return renderWorkers();
  }
  app.innerHTML = `<div class="card"><p>Unknown view.</p><p><a href="#/system">Open System</a></p></div>`;
}

async function renderWorkers() {
  app.innerHTML = "<p class='muted'>Loading workers…</p>";
  const data = await api("/api/certification/workers");
  const cards = data.workers.map((worker) => {
    const production = worker.production_eligibility || {};
    const productionLabel = production.eligible ? "ELIGIBLE" : "BLOCKED";
    const roles = worker.roles || {};
    return `<article class="card">
      <div class="row">
        <strong><a href="#/certification/${encodeURIComponent(worker.worker_id)}">${escapeHtml(worker.worker_id)}</a></strong>
        ${badge(worker.environment, "warn")}
      </div>
      <p class="muted">${escapeHtml(worker.kind)} · ${escapeHtml(worker.network_class)}</p>
      <div class="row">
        ${badge("Runtime " + ((worker.runtime && worker.runtime.status) || "UNKNOWN"), statusKind(worker.runtime && worker.runtime.status))}
        ${badge("Baseline " + worker.baseline_security.status, statusKind(worker.baseline_security.status))}
        ${badge("Planner " + (roles.PLANNER && roles.PLANNER.status), statusKind(roles.PLANNER && roles.PLANNER.status))}
        ${badge("Coder " + (roles.CODER && roles.CODER.status), statusKind(roles.CODER && roles.CODER.status))}
        ${badge("Reviewer " + (roles.REVIEWER && roles.REVIEWER.status), statusKind(roles.REVIEWER && roles.REVIEWER.status))}
        ${badge("Repairer " + (roles.REPAIRER && roles.REPAIRER.status), statusKind(roles.REPAIRER && roles.REPAIRER.status))}
        ${badge("Security " + (roles.SECURITY && roles.SECURITY.status), statusKind(roles.SECURITY && roles.SECURITY.status))}
        ${badge("Production " + productionLabel, statusKind(productionLabel))}
      </div>
      <p class="muted">Eligibility: ${escapeHtml(production.reason || "unavailable")} (${escapeHtml(production.source || "")})</p>
    </article>`;
  }).join("") || "<p class='muted'>No registered workers.</p>";
  app.innerHTML = `<h1>Certification</h1>
    <p class="muted">Server-owned status. Baseline Security is never merged with a role certificate. Production eligibility is computed only by evaluate_production_eligibility.</p>
    ${cards}`;
}

async function renderWorker(workerId) {
  app.innerHTML = "<p class='muted'>Loading worker…</p>";
  const worker = await api("/api/certification/workers/" + encodeURIComponent(workerId));
  const identity = worker.identity;
  const identityRows = identity
    ? Object.entries(identity)
        .filter(([key]) => key !== "worker_id" && key !== "provider_runtime")
        .map(([key, item]) => {
          const value = item && typeof item === "object" ? item.value : item;
          const source = item && item.source ? item.source : "";
          const measured = item && item.measured_by_ollama === false ? " · not Ollama-measured" : "";
          return `<tr><td>${escapeHtml(key)}</td><td><code>${escapeHtml(value)}</code></td><td class="muted">${escapeHtml(source)}${escapeHtml(measured)}</td></tr>`;
        }).join("")
    : "<tr><td colspan='3' class='muted'>No server-owned runtime expectation is configured.</td></tr>";
  const runHistory = ((worker.history && worker.history.runs) || []).map((run) => `
    <tr>
      <td><a href="#/certification/runs/${escapeHtml(run.run_id)}">${escapeHtml(run.created_at)}</a></td>
      <td>${badge("RUN", "warn")} ${badge(run.state, statusKind(run.state))}</td>
      <td>${run.has_certificate ? "<code>" + escapeHtml(run.certificate_id) + "</code>" : "<span class='muted'>no certificate</span>"}</td>
    </tr>`).join("") || "<tr><td colspan='3' class='muted'>No validation runs yet.</td></tr>";
  const certHistory = [
    ...((worker.history && worker.history.validation_certificates) || []).map((item) => ({...item, env: "VALIDATION"})),
    ...((worker.history && worker.history.production_certificates) || []).map((item) => ({...item, env: "PRODUCTION"})),
  ].map((item) => `
    <tr>
      <td>${escapeHtml(item.issued_at)}</td>
      <td>${badge("CERTIFICATE", "ok")} ${badge(item.outcome, statusKind(item.outcome))} ${badge(item.env, item.env === "VALIDATION" ? "warn" : "muted")}</td>
      <td><code>${escapeHtml(item.certificate_id)}</code></td>
    </tr>`).join("") || "<tr><td colspan='3' class='muted'>No certificates.</td></tr>";
  const ready = Boolean(worker.ready_for_certification);
  const lastChecks = (worker.last_preflight && worker.last_preflight.checks) || [];
  const preflightHtml = lastChecks.length
    ? lastChecks.map((check) =>
        `<div class="check ${check.ok ? "ok" : "bad"}">${check.ok ? "OK" : "BLOCKED"} ${escapeHtml(check.name)}${check.detail ? " — " + escapeHtml(check.detail) : ""}</div>`
      ).join("") + `<p><strong>${ready ? "READY FOR CERTIFICATION" : "CERTIFICATION BLOCKED"}</strong></p>`
    : "<p class='muted'>No durable preflight yet. Preflight never infers.</p>";
  const future = (worker.future_actions || []).map((item) =>
    `<tr><td>${escapeHtml(item.role)}</td><td><button disabled>Run live ${escapeHtml(item.role)} certification</button></td><td class="muted">${escapeHtml(item.reason)}</td></tr>`
  ).join("");
  app.innerHTML = `
    <p><a href="#/certification">Workers</a></p>
    <h1>${escapeHtml(worker.worker_id)}</h1>
    <div class="row">${badge(worker.environment, "warn")}
      ${badge("Runtime " + worker.runtime.status, statusKind(worker.runtime.status))}
      ${badge("Baseline " + worker.baseline_security.status, statusKind(worker.baseline_security.status))}
      ${badge("Planner " + worker.roles.PLANNER.status, statusKind(worker.roles.PLANNER.status))}
      ${badge("Production " + (worker.production_eligibility.eligible ? "ELIGIBLE" : "BLOCKED"), statusKind(worker.production_eligibility.eligible ? "ELIGIBLE" : "BLOCKED"))}
    </div>
    <p class="muted">Production eligibility reason: <code>${escapeHtml(worker.production_eligibility.reason)}</code> (${escapeHtml(worker.production_eligibility.source)})</p>
    <h2>Identity</h2>
    <div class="card"><table><thead><tr><th>Field</th><th>Value</th><th>Source</th></tr></thead><tbody>${identityRows}</tbody></table></div>
    <h2>Baseline Security</h2>
    <div class="card">
      <div class="row">
        <button id="preflight">Run preflight</button>
        <button id="start" class="secondary" ${ready ? "" : "disabled"}>Run Baseline Security</button>
      </div>
      <p class="muted">Preflight never infers. Certification is one explicit attempt against VALIDATION state. There is no retry control.</p>
      <div id="preflight-result">${preflightHtml}</div>
    </div>
    <h2>Role certificates</h2>
    <div class="card"><table><tbody>
      ${Object.entries(worker.roles).map(([role, info]) => `<tr><td>${escapeHtml(role)}</td><td>${badge(info.status, statusKind(info.status))}</td><td class="muted">${escapeHtml(info.certificate_id || "")}</td></tr>`).join("")}
    </tbody></table>
    <h3>Live role certification</h3>
    <table><tbody>${future}</tbody></table></div>
    <h2>History</h2>
    <div class="card">
      <h3>Runs</h3>
      <table><thead><tr><th>When</th><th>Kind / state</th><th>Certificate</th></tr></thead><tbody>${runHistory}</tbody></table>
      <h3>Certificates</h3>
      <table><thead><tr><th>When</th><th>Kind / outcome</th><th>Id</th></tr></thead><tbody>${certHistory}</tbody></table>
    </div>
    <div id="modal"></div>`;
  document.getElementById("preflight").onclick = () => runPreflight(workerId);
  document.getElementById("start").onclick = () => confirmStart(worker);
}

async function runPreflight(workerId) {
  const box = document.getElementById("preflight-result");
  box.innerHTML = "<p class='muted'>Running preflight…</p>";
  try {
    const result = await api("/api/certification/workers/" + encodeURIComponent(workerId) + "/baseline/preflight", {
      method: "POST",
      body: "{}",
    });
    const items = (result.checks || []).map((check) =>
      `<div class="check ${check.ok ? "ok" : "bad"}">${check.ok ? "OK" : "BLOCKED"} ${escapeHtml(check.name)}${check.detail ? " — " + escapeHtml(check.detail) : ""}</div>`
    ).join("");
    box.innerHTML = `<h3>Baseline Security Preflight</h3>${items}<p><strong>${result.ready ? "READY FOR CERTIFICATION" : "CERTIFICATION BLOCKED"}</strong></p>`;
    document.getElementById("start").disabled = !result.ready;
  } catch (err) {
    box.innerHTML = `<p class="check bad">Preflight failed: ${escapeHtml(err.message)}</p>`;
    document.getElementById("start").disabled = true;
  }
}

function confirmStart(worker) {
  const identity = worker.identity || {};
  const modal = document.getElementById("modal");
  modal.innerHTML = `<div class="modal"><div class="card">
    <h2>Start Baseline Security certification?</h2>
    <p>Worker: <code>${escapeHtml(worker.worker_id)}</code></p>
    <p>Model: <code>${escapeHtml((identity.model_tag && identity.model_tag.value) || "")}</code></p>
    <p>Digest: <code>${escapeHtml((identity.model_digest && identity.model_digest.value) || "")}</code></p>
    <p>Runtime fingerprint: <code>${escapeHtml((identity.runtime_identity_fingerprint && identity.runtime_identity_fingerprint.value) || "")}</code></p>
    <p>Environment: <strong>VALIDATION</strong></p>
    <p class="muted">This runs the security evaluation against the live registered model. Closing the browser will not cancel it. There is no retry.</p>
    <div class="row">
      <button class="secondary" id="cancel">Cancel</button>
      <button id="confirm">Start certification</button>
    </div>
  </div></div>`;
  document.getElementById("cancel").onclick = () => { modal.innerHTML = ""; };
  document.getElementById("confirm").onclick = () => startRun(worker.worker_id);
}

let starting = false;
async function startRun(workerId) {
  if (starting) return;
  starting = true;
  const confirm = document.getElementById("confirm");
  if (confirm) confirm.disabled = true;
  try {
    const result = await api("/api/certification/workers/" + encodeURIComponent(workerId) + "/baseline/runs", {
      method: "POST",
      body: "{}",
    });
    location.hash = "#/certification/runs/" + result.run_id;
  } catch (err) {
    starting = false;
    const modal = document.getElementById("modal");
    if (modal) modal.innerHTML = `<div class="modal"><div class="card"><p class="check bad">${escapeHtml(err.message)}</p><button id="cancel">Close</button></div></div>`;
    const close = document.getElementById("cancel");
    if (close) close.onclick = () => { modal.innerHTML = ""; };
    return;
  }
  starting = false;
}

async function renderRun(runId) {
  app.innerHTML = "<p class='muted'>Loading run…</p>";
  const run = await api("/api/certification/runs/" + encodeURIComponent(runId));
  const progress = (run.progress || []).map((item) =>
    `<div class="check ${item.status === "done" ? "ok" : ""}">${escapeHtml(item.name)} — ${escapeHtml(item.status)}</div>`
  ).join("");
  app.innerHTML = `
    <p><a href="#/certification/${encodeURIComponent(run.worker_id)}">Worker</a></p>
    <h1>Baseline Security</h1>
    <div class="row">${badge(run.state, statusKind(run.state))} ${badge(run.environment, "warn")}</div>
    <p>Reason: <code>${escapeHtml(run.reason || "")}</code></p>
    <div class="card"><h2>Progress</h2>${progress}</div>
    <div class="card">
      <p>Certificate: ${run.has_certificate ? "<code>" + escapeHtml(run.certificate_id) + "</code>" : "<span class='muted'>none — this run is not a certificate</span>"}</p>
      <p>Evidence: ${run.evidence_ref ? "<code>" + escapeHtml(run.evidence_ref) + "</code>" : "<span class='muted'>none</span>"}</p>
      <p>Fingerprint: <code>${escapeHtml(run.runtime_identity_fingerprint || "")}</code></p>
      <p>Digest: <code>${escapeHtml(run.model_digest || "")}</code></p>
      <p class="muted">${escapeHtml(run.started_at || "")} → ${escapeHtml(run.finished_at || "")}</p>
    </div>
    <div class="card">
      <button class="secondary" id="evidence" ${run.evidence_ref ? "" : "disabled"}>Show evidence</button>
      <div id="evidence-view"></div>
    </div>`;
  document.getElementById("evidence").onclick = async () => {
    const view = document.getElementById("evidence-view");
    try {
      const data = await api("/api/certification/runs/" + encodeURIComponent(runId) + "/evidence");
      const doc = data.document || {};
      const cases = (doc.cases || []).map((item) => `
        <tr>
          <td><code>${escapeHtml(item.case_id)}</code></td>
          <td>${escapeHtml(item.category || "")}</td>
          <td>${badge(item.outcome, statusKind(item.outcome))}</td>
          <td class="muted">${escapeHtml(item.reason || "")}</td>
          <td>${escapeHtml(item.hard_disqualifier || "")}</td>
          <td>${escapeHtml(item.observed_tool || "")}</td>
          <td>${item.executed ? "yes" : "no"}</td>
        </tr>`).join("");
      view.innerHTML = `
        <p>Final outcome: ${badge(doc.final_outcome, statusKind(doc.final_outcome))} — ${escapeHtml(doc.final_reason || "")}</p>
        <p class="muted">Evidence is reread from the ContentStore. Raw model text is not shown.</p>
        <table>
          <thead><tr><th>Case</th><th>Category</th><th>Outcome</th><th>Reason</th><th>Hard</th><th>Tool</th><th>Executed</th></tr></thead>
          <tbody>${cases}</tbody>
        </table>`;
    } catch (err) {
      view.innerHTML = `<p class="check bad">${escapeHtml(err.message)}</p>`;
    }
  };
  if (run.state === "QUEUED" || run.state === "RUNNING") {
    setTimeout(() => {
      if (location.hash === "#/certification/runs/" + runId) renderRun(runId);
    }, 1000);
  }
}

function sourceBadge(item) {
  const source = item && typeof item === "object" ? item.source : "";
  const value = item && typeof item === "object" && "value" in item ? item.value : item;
  return `${value == null || value === "" ? "<span class='muted'>—</span>" : "<code>" + escapeHtml(value) + "</code>"} ${source ? badge(source, statusKind(source)) : ""}`;
}

async function renderSystem() {
  app.innerHTML = "<p class='muted'>Loading system…</p>";
  const data = await api("/api/system");
  const service = data.service || {};
  const network = data.network || {};
  const health = data.health || {};
  const env = document.getElementById("env-badge");
  if (env) env.textContent = service.running ? "RUNNING" : (service.state || "LOCAL");
  app.innerHTML = `
    <h1>System</h1>
    <div class="card">
      <div class="row">
        ${badge(service.state || "UNVERIFIED", statusKind(service.state))}
        ${badge(service.running ? "running" : "stopped", service.running ? "ok" : "bad")}
        ${badge(health.status || "unknown", statusKind(health.status))}
      </div>
      <p>Version: <code>${escapeHtml(service.version)}</code></p>
      <p>Process commit (running): <code>${escapeHtml(service.process_commit || service.running_commit || "")}</code> ${badge(service.process_commit_source || service.running_commit_source || "UNVERIFIED", statusKind(service.process_commit_source || service.running_commit_source))}</p>
      <p>Checkout HEAD: <code>${escapeHtml(service.checkout_head || "")}</code> ${badge(service.checkout_head_source || "UNVERIFIED", statusKind(service.checkout_head_source))}</p>
      <p>Deployment: ${badge(service.deployment_status || "UNVERIFIED", statusKind(service.deployment_status))} complete=<code>${escapeHtml(service.deployment_complete)}</code></p>
      <p class="note">Process commit is captured at process start and does not follow git HEAD until restart.</p>
      <p>Uptime: <code>${escapeHtml(service.uptime_seconds)}</code> seconds (${escapeHtml(service.source || "")})</p>
      <p>Health schema: <code>${escapeHtml(health.schema_version)}</code> ${badge(health.source || "", statusKind(health.source))}</p>
      <p>Local URL: <a href="${escapeHtml(network.local_url || "")}">${escapeHtml(network.local_url || "")}</a></p>
      <p class="muted">Bind: <code>${escapeHtml(network.bind_host)}</code>:<code>${escapeHtml(network.bind_port)}</code> — loopback only. Remote access is Tailscale Serve, never 0.0.0.0.</p>
      <div class="form-actions">
        <button id="restart">Restart service</button>
        <button class="secondary" id="update-check">Check for update</button>
        <button class="secondary" id="update-apply">Apply update</button>
      </div>
      <div id="system-result"></div>
    </div>`;
  document.getElementById("restart").onclick = async () => {
    const box = document.getElementById("system-result");
    box.innerHTML = "<p class='muted'>Restart requested…</p>";
    try {
      await api("/api/system/restart", { method: "POST", body: "{}" });
      box.innerHTML = "<p>Restart requested. Reload this page in a few seconds and confirm process_commit equals checkout_head.</p>";
    } catch (err) {
      box.innerHTML = `<p class="check bad">${escapeHtml(err.message)}</p>`;
    }
  };
  document.getElementById("update-check").onclick = () => runUpdate("check");
  document.getElementById("update-apply").onclick = () => {
    if (!confirm("Apply a fast-forward update? Dirty or divergent trees are refused. git merge is not a completed deployment until process_commit matches checkout_head after restart.")) return;
    runUpdate("apply");
  };
}

async function runUpdate(kind) {
  const box = document.getElementById("system-result");
  box.innerHTML = "<p class='muted'>Working…</p>";
  try {
    const path = kind === "apply" ? "/api/system/update/apply" : "/api/system/update/check";
    const result = await api(path, { method: "POST", body: "{}" });
    box.innerHTML = `
      <div class="card">
        <div class="row">${badge(result.status, statusKind(result.status))} ${badge(result.detail || "", "muted")}</div>
        <p>Current: <code>${escapeHtml(result.current_commit || "")}</code></p>
        <p>Origin: <code>${escapeHtml(result.origin_commit || "")}</code></p>
        <p>Branch: <code>${escapeHtml(result.branch || "")}</code></p>
        <p>Dirty: <code>${escapeHtml(result.dirty)}</code> Divergent: <code>${escapeHtml(result.divergent)}</code></p>
        ${result.deployment_note ? `<p class="note">${escapeHtml(result.deployment_note)}</p>` : ""}
        ${result.restart_error ? `<p class="check bad">Restart: ${escapeHtml(result.restart_error)}</p>` : ""}
      </div>`;
  } catch (err) {
    box.innerHTML = `<p class="check bad">${escapeHtml(err.message)}</p>`;
  }
}

async function renderRuntime() {
  app.innerHTML = "<p class='muted'>Loading runtime…</p>";
  const data = await api("/api/runtime");
  const servers = (data.ollama_servers || []).map((server) => `
    <tr>
      <td><code>${escapeHtml(server.id)}</code></td>
      <td><code>${escapeHtml(server.origin)}</code></td>
      <td>${badge(server.origin_source || "CONFIG_BOUND", "muted")}</td>
      <td><button class="secondary" data-test="${escapeHtml(server.id)}">Test connection</button></td>
    </tr>`).join("") || "<tr><td colspan='4' class='muted'>No Ollama servers yet.</td></tr>";
  const workers = (data.workers || []).map((worker) => {
    const digest = worker.approved_model_digest || {};
    const version = worker.approved_runtime_version || {};
    return `<article class="card">
      <div class="row">
        <strong>${escapeHtml(worker.worker_id)}</strong>
        ${badge(worker.identity_approved ? "identity approved" : "identity not approved", worker.identity_approved ? "ok" : "warn")}
      </div>
      <p>Model tag: ${sourceBadge(worker.model_tag)}</p>
      <p>Approved digest: ${sourceBadge(digest)}</p>
      <p>Approved runtime: ${sourceBadge(version)}</p>
      <p>Context: ${sourceBadge(worker.effective_context_tokens)} ${worker.effective_context_tokens && worker.effective_context_tokens.measured_by_ollama === false ? "<span class='muted'>not Ollama-measured</span>" : ""}</p>
      <p>Temperature: ${sourceBadge(worker.temperature)}</p>
      <p>Normalizer: ${sourceBadge(worker.normalizer_id)} ${sourceBadge(worker.normalizer_version)}</p>
      <p class="muted">Server: <code>${escapeHtml(worker.ollama_server_id)}</code> · ${escapeHtml(worker.kind)} · ${escapeHtml(worker.network_class)}</p>
      <div class="form-actions">
        <button class="secondary" data-approve="${escapeHtml(worker.worker_id)}">${worker.identity_approved ? "Re-attest / approve if unchanged" : "Approve live identity"}</button>
        <button class="secondary" data-replace="${escapeHtml(worker.worker_id)}">Approve new identity</button>
      </div>
    </article>`;
  }).join("") || "<p class='muted'>No workers registered in persistent config.</p>";
  app.innerHTML = `
    <h1>Runtime</h1>
    <p class="muted">Persistent config is the operator-facing identity store. The browser never sends a digest, fingerprint, outcome, or adapter. Approving a new identity does not transfer old certificates.</p>
    <div class="card">
      <h2>Ollama servers</h2>
      <table><thead><tr><th>Id</th><th>Origin</th><th>Source</th><th></th></tr></thead><tbody>${servers}</tbody></table>
      <label>Server id</label>
      <input id="ollama-id" autocomplete="off">
      <label>Origin (http(s) host:port only)</label>
      <input id="ollama-origin" placeholder="http://127.0.0.1:11434" autocomplete="off">
      <div class="form-actions">
        <button id="add-server">Add and test origin</button>
        <button class="secondary" id="attest">Attest live runtimes</button>
      </div>
      <div id="server-result"></div>
    </div>
    <div class="card">
      <h2>Register worker</h2>
      <label>Worker id</label>
      <input id="worker-id" autocomplete="off">
      <label>Ollama server id</label>
      <input id="worker-server" autocomplete="off">
      <label>Model tag</label>
      <input id="worker-tag" autocomplete="off">
      <label>Effective context tokens (config-bound, not Ollama-measured)</label>
      <input id="worker-context" type="number" min="1" value="16384">
      <label>Temperature</label>
      <input id="worker-temp" type="number" min="0" max="2" step="0.1" value="0">
      <label>Normalizer id (optional)</label>
      <input id="worker-normalizer" placeholder="leave empty unless required" autocomplete="off">
      <label>Normalizer version (optional)</label>
      <input id="worker-normalizer-version" type="number" min="1">
      <p class="note">Do not paste a digest here. Approve live identity after the origin is reachable.</p>
      <div class="form-actions"><button id="register-worker">Register worker</button></div>
      <div id="worker-result"></div>
    </div>
    <h2>Workers</h2>
    ${workers}
    <div id="runtime-result"></div>`;
  document.querySelectorAll("[data-test]").forEach((button) => {
    button.onclick = () => testServer(button.getAttribute("data-test"));
  });
  document.querySelectorAll("[data-approve]").forEach((button) => {
    button.onclick = () => approveWorker(button.getAttribute("data-approve"), false);
  });
  document.querySelectorAll("[data-replace]").forEach((button) => {
    button.onclick = () => {
      if (!confirm("Approve a NEW live identity? Existing certificates stay bound to the old fingerprint and are not transferred.")) return;
      approveWorker(button.getAttribute("data-replace"), true);
    };
  });
  document.getElementById("add-server").onclick = addServer;
  document.getElementById("attest").onclick = attestAll;
  document.getElementById("register-worker").onclick = registerWorker;
}

async function addServer() {
  const box = document.getElementById("server-result");
  const id = document.getElementById("ollama-id").value.trim();
  const origin = document.getElementById("ollama-origin").value.trim();
  box.innerHTML = "<p class='muted'>Connecting…</p>";
  try {
    await api("/api/runtime/ollama-servers", {
      method: "POST",
      body: JSON.stringify({ id, origin }),
    });
    route();
  } catch (err) {
    box.innerHTML = `<p class="check bad">${escapeHtml(err.message)}</p>`;
  }
}

async function testServer(serverId) {
  const box = document.getElementById("server-result");
  box.innerHTML = "<p class='muted'>Probing…</p>";
  try {
    const result = await api("/api/runtime/ollama-servers/" + encodeURIComponent(serverId) + "/test", {
      method: "POST",
      body: "{}",
    });
    const models = (result.models || []).map((item) =>
      `<tr><td><code>${escapeHtml(item.name)}</code></td><td><code>${escapeHtml(item.digest)}</code></td><td>${badge("LIVE_ATTESTED", "ok")}</td></tr>`
    ).join("") || "<tr><td colspan='3' class='muted'>No models reported.</td></tr>";
    box.innerHTML = `
      <p>Runtime ${badge(result.status, statusKind(result.status))} version <code>${escapeHtml(result.runtime_version)}</code></p>
      <p class="note">Digests below are live observations. They are not approved until you use Approve live identity. This page never posts a digest.</p>
      <table><thead><tr><th>Tag</th><th>Digest (observed)</th><th></th></tr></thead><tbody>${models}</tbody></table>`;
  } catch (err) {
    box.innerHTML = `<p class="check bad">${escapeHtml(err.message)}</p>`;
  }
}

async function attestAll() {
  const box = document.getElementById("runtime-result");
  box.innerHTML = "<p class='muted'>Attesting…</p>";
  try {
    const data = await api("/api/runtime/attest", { method: "POST", body: "{}" });
    const rows = (data.workers || []).map((worker) => {
      const att = worker.attestation || {};
      return `<tr>
        <td><code>${escapeHtml(worker.worker_id)}</code></td>
        <td>${badge(att.status || "UNVERIFIED", statusKind(att.status))}</td>
        <td class="muted">${escapeHtml(att.reason || "")}</td>
        <td><code>${escapeHtml(att.observed_digest || "")}</code></td>
      </tr>`;
    }).join("");
    box.innerHTML = `<div class="card"><h2>Live attestation</h2><table><tbody>${rows}</tbody></table></div>`;
  } catch (err) {
    box.innerHTML = `<p class="check bad">${escapeHtml(err.message)}</p>`;
  }
}

async function registerWorker() {
  const box = document.getElementById("worker-result");
  const payload = {
    worker_id: document.getElementById("worker-id").value.trim(),
    ollama_server_id: document.getElementById("worker-server").value.trim(),
    model_tag: document.getElementById("worker-tag").value.trim(),
  };
  const context = document.getElementById("worker-context").value;
  const temp = document.getElementById("worker-temp").value;
  const normalizer = document.getElementById("worker-normalizer").value.trim();
  const normalizerVersion = document.getElementById("worker-normalizer-version").value;
  if (context) payload.effective_context_tokens = Number(context);
  if (temp !== "") payload.temperature = Number(temp);
  if (normalizer) payload.normalizer_id = normalizer;
  if (normalizerVersion) payload.normalizer_version = Number(normalizerVersion);
  box.innerHTML = "<p class='muted'>Saving…</p>";
  try {
    await api("/api/runtime/workers", { method: "POST", body: JSON.stringify(payload) });
    route();
  } catch (err) {
    box.innerHTML = `<p class="check bad">${escapeHtml(err.message)}</p>`;
  }
}

async function approveWorker(workerId, replace) {
  const box = document.getElementById("runtime-result");
  box.innerHTML = "<p class='muted'>Approving from live attestation…</p>";
  const path = replace
    ? "/api/runtime/workers/" + encodeURIComponent(workerId) + "/approve-new-identity"
    : "/api/runtime/workers/" + encodeURIComponent(workerId) + "/approve";
  try {
    const result = await api(path, { method: "POST", body: "{}" });
    box.innerHTML = `
      <div class="card">
        ${badge(result.status, statusKind(result.status))}
        <p>Reason: <code>${escapeHtml(result.reason || "")}</code></p>
        <p>Configured digest: <code>${escapeHtml(result.configured_digest || "")}</code></p>
        <p>Observed digest: <code>${escapeHtml(result.observed_digest || "")}</code></p>
        <p>Certificates transferred: <code>${escapeHtml(result.certificates_transferred)}</code></p>
      </div>`;
    if (result.status === "VERIFIED") route();
  } catch (err) {
    box.innerHTML = `<p class="check bad">${escapeHtml(err.message)}</p>`;
  }
}

async function renderTailscale() {
  app.innerHTML = "<p class='muted'>Loading Tailscale…</p>";
  const data = await api("/api/tailscale");
  const node = data.node || {};
  const serve = data.serve || {};
  app.innerHTML = `
    <h1>Tailscale</h1>
    <div class="card">
      <div class="row">
        ${badge("node " + (node.state || "UNVERIFIED"), statusKind(node.state))}
        ${badge(node.source || "", "muted")}
        ${badge("serve " + (serve.status || "UNVERIFIED"), statusKind(serve.status))}
        ${badge("remote " + (data.remote_access || "UNVERIFIED"), statusKind(data.remote_access))}
      </div>
      <p>Expected Serve backend: <code>${escapeHtml((serve.expected_backend || data.backend) || "")}</code></p>
      <p>Observed Serve backend: <code>${escapeHtml(serve.observed_backend || "")}</code> ${badge(serve.source || "", "muted")}</p>
      <p>URL: ${data.url ? `<a href="${escapeHtml(data.url)}">${escapeHtml(data.url)}</a>` : "<span class='muted'>none</span>"}</p>
      <p>Config enabled: <code>${escapeHtml(data.enabled)}</code> ${badge(data.enabled_source || "CONFIG_BOUND", "muted")}</p>
      <p class="muted">${escapeHtml(data.detail || "")}. Funnel is never used. Node connectivity is not proof of CSLR Serve.</p>
      <div class="form-actions">
        <button id="ts-enable">Enable Serve</button>
        <button class="secondary" id="ts-disable">Disable Serve</button>
      </div>
      <div id="ts-result"></div>
    </div>`;
  document.getElementById("ts-enable").onclick = () => setTailscale(true);
  document.getElementById("ts-disable").onclick = () => setTailscale(false);
}

async function setTailscale(enabled) {
  const box = document.getElementById("ts-result");
  box.innerHTML = "<p class='muted'>Working…</p>";
  try {
    await api(enabled ? "/api/tailscale/enable" : "/api/tailscale/disable", {
      method: "POST",
      body: "{}",
    });
    route();
  } catch (err) {
    box.innerHTML = `<p class="check bad">${escapeHtml(err.message)}</p>`;
  }
}

window.addEventListener("hashchange", () => route().catch((err) => {
  app.innerHTML = `<div class="card check bad">${escapeHtml(err.message)}</div>`;
}));
route().catch((err) => {
  app.innerHTML = `<div class="card check bad">${escapeHtml(err.message)}</div>`;
});
