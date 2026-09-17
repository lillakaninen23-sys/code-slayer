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
    status === "VERIFIED"
  ) return "ok";
  if (
    status === "FAILED" ||
    status === "BLOCKED" ||
    status === "HARD_DISQUALIFIED" ||
    status === "MISMATCH" ||
    status === "UNREACHABLE" ||
    status === "ERROR"
  ) return "bad";
  if (status === "READY" || status === "RUNNING" || status === "QUEUED") return "warn";
  return "muted";
}

function route() {
  const hash = location.hash.replace(/^#/, "") || "/certification";
  const parts = hash.split("/").filter(Boolean);
  if (parts[0] !== "certification") {
    app.innerHTML = `<div class="card"><p>Certification Center is the active v1 surface.</p><p><a href="#/certification">Open Certification</a></p></div>`;
    return;
  }
  if (parts[1] === "runs" && parts[2]) return renderRun(parts[2]);
  if (parts[1] && parts[1] !== "runs") return renderWorker(decodeURIComponent(parts[1]));
  return renderWorkers();
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

window.addEventListener("hashchange", () => route().catch((err) => {
  app.innerHTML = `<div class="card check bad">${escapeHtml(err.message)}</div>`;
}));
route().catch((err) => {
  app.innerHTML = `<div class="card check bad">${escapeHtml(err.message)}</div>`;
});
