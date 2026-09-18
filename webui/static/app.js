import { createAPI } from "./api.js";
import { workerAlias, saveWorkerAlias } from "./aliases.js";
import {
  escapeHTML as esc,
  badge,
  connectionText,
  pollDelay,
  statusClass,
  renderRuns,
  renderRun,
  renderQuestions,
  renderTrust,
  renderAudit,
  renderConformance,
  intelStatusClass,
  intelStatusLabel,
  renderIntelStatus,
  renderIntelProjects,
  renderIntelCommands,
  renderIntelResults,
  planBadge,
  renderPlanList,
  renderPlanDetail,
  renderPlanQuestions,
  jobBadge,
  renderPendingPermissionRequests,
  renderActivePermissionGrants,
  renderPermissionHistory,
} from "./views.js";
import {
  renderRuntimeServers,
  renderRuntimeWorkers,
  renderRuntimeAttestation,
  renderRuntimeServerOptions,
  runtimeRegistrationPayload,
  acceptRuntimeSnapshot,
  rejectRuntimeSnapshot,
  acceptRuntimeAttest,
  acceptIdentityResult,
  beginRuntimeObservation,
  clearRuntimeEvidence,
  invalidateServerTest,
  acceptServerTest,
  certificationPollDelay,
  shouldContinueCertificationPoll,
  isCertificationTerminal,
  rejectCertificationSnapshot,
  acceptCertificationWorkers,
  selectCertificationWorkerId,
  acceptCertificationWorker,
  beginCertificationRun,
  applyCertificationPoll,
  beginCertificationEvidenceRequest,
  acceptCertificationEvidence,
  rejectCertificationEvidence,
  renderCertificationWorkers,
  renderCertificationWorkerDetail,
  renderCertificationRun,
  renderCertificationHistory,
  renderCertificationEvidence,
} from "./views-admin.js";

const api = createAPI();
const $ = (id) => document.getElementById(id);
const state = {
  connected: false,
  busy: false,
  refreshing: false,
  selected: null,
  run: null,
  runs: [],
  workers: [],
  health: null,
  questionKey: null,
  nextOffset: null,
  workerId: null,
  intelBusy: false,
  planBusy: false,
  plans: [],
  selectedPlanId: null,
  selectedPlan: null,
  activeJob: null,
  privacyBusy: false,
  permissionRequests: [],
  permissionGrants: [],
  runtime: null,
  runtimeBusy: false,
  runtimeUnavailable: false,
  runtimeAttestation: null,
  runtimeServerTests: {},
  runtimeIdentityResults: {},
  runtimeReplacePending: null,
  certificationWorkers: null,
  certificationEnvironment: null,
  certificationUnavailable: false,
  selectedCertificationWorkerId: null,
  selectedCertificationWorker: null,
  certificationBusy: false,
  certificationActiveRun: null,
  certificationHistory: null,
  certificationEvidence: null,
  certificationEvidenceError: null,
  certificationEvidenceRequestId: null,
  certificationSelectionVersion: 0,
};
let timer;
let activeJobTimer;
let certificationTimer;
let selectionVersion = 0;

function view(name) {
  document
    .querySelectorAll(".nav-item")
    .forEach((button) =>
      button.classList.toggle("active", button.dataset.view === name),
    );
  document
    .querySelectorAll(".view")
    .forEach((panel) => panel.classList.toggle("active", panel.id === name));
  if (name === "intelligence") loadIntelligence();
  if (name === "planning") loadPlanning();
  if (name === "privacy") loadPrivacy();
  if (name === "models") loadRuntime();
}
document
  .querySelectorAll(".nav-item")
  .forEach((button) =>
    button.addEventListener("click", () => view(button.dataset.view)),
  );
function notice(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
}
function controls() {
  const canStart =
    state.connected &&
    !state.busy &&
    !state.refreshing &&
    state.health?.actions.start &&
    state.workers.length > 0;
  $("new-run").disabled = !canStart;
  $("start-submit").disabled = !canStart;
  $("resume").disabled =
    !state.connected ||
    state.busy ||
    state.refreshing ||
    !["resume", "answer_then_resume"].includes(state.run?.next_safe_action);
  $("questions")
    .querySelectorAll("button")
    .forEach((button) => {
      button.disabled =
        !state.connected ||
        state.busy ||
        state.refreshing ||
        state.run?.run_id !== state.selected;
    });
  $("refresh").disabled = state.busy || state.refreshing;
}
function connected(value) {
  const wasConnected = state.connected;
  state.connected = value;
  $("connection-dot").classList.toggle("offline", !value);
  $("connection-label").textContent = value
    ? "Backend connected"
    : "Disconnected";
  $("backend-status").textContent = value ? "CONNECTED" : "DISCONNECTED";
  controls();
  runtimeControls();
  if (!value) {
    rejectRuntimeSnapshot(state);
    renderRuntimeView();
    rejectCertificationSnapshot(state);
    renderCertificationView();
  } else if (!wasConnected) {
    if (document.getElementById("models")?.classList.contains("active"))
      loadRuntime();
    if (document.getElementById("privacy")?.classList.contains("active"))
      loadPrivacy();
  }
}
async function workerDetail(workerId) {
  state.workerId = workerId;

  $("workers-list")
    .querySelectorAll(".model-card")
    .forEach((card) => {
      card.classList.toggle(
        "selected",
        card.dataset.worker === workerId,
      );
    });

  const [trust, conformance] = await Promise.all([
    api.trust(workerId),
    api.conformance(workerId),
  ]);
  if (state.workerId !== workerId) return;
  const expanded = new Set(
    [...$("worker-detail").querySelectorAll("details[open] summary")].map(
      (el) => el.textContent,
    ),
  );
  $("worker-detail").innerHTML = `
    <div class="worker-identity">

      <label>
        Display name

        <input
          id="worker-alias"
          value="${esc(workerAlias(workerId))}"
          placeholder="${esc(workerId)}"
        >
      </label>

      <button
        id="save-worker-alias"
        class="ghost small"
      >
        Save name
      </button>

      <div class="model-meta">
        Backend ID: ${esc(workerId)}
      </div>

    </div>

    ${renderTrust(trust)}

    <h3>
      Conformance
    </h3>

    ${renderConformance(conformance)}

    <details>
      <summary>
        Exact trust history
      </summary>

      <pre>${esc(JSON.stringify(trust, null, 2))}</pre>
    </details>
  `;
  $("save-worker-alias").addEventListener("click", () => {
    saveWorkerAlias(workerId, $("worker-alias").value);

    document
      .querySelectorAll("#workers-list [data-worker]")
      .forEach((card) => {
        const id = card.dataset.worker;

        card.querySelector(".model-title").textContent =
          workerAlias(id);
      });
  });
    $("worker-detail")
    .querySelectorAll("details")
    .forEach((el) => {
      el.open = expanded.has(el.querySelector("summary").textContent);
    });
}
function runtimeControls() {
  const busy = state.runtimeBusy || !state.connected;
  const attest = $("runtime-attest");
  if (attest) attest.disabled = busy;
  for (const id of [
    "ollama-server-id",
    "ollama-server-origin",
    "ollama-server-submit",
    "runtime-worker-id",
    "runtime-worker-server",
    "runtime-worker-model",
    "runtime-worker-submit",
    "runtime-worker-kind",
    "runtime-worker-network",
    "runtime-worker-context",
    "runtime-worker-temperature",
    "runtime-worker-normalizer-id",
    "runtime-worker-normalizer-version",
  ]) {
    const el = $(id);
    if (el) el.disabled = busy;
  }
  document
    .querySelectorAll(
      "[data-runtime-test-server], [data-runtime-approve], [data-runtime-replace-ask], [data-runtime-replace-confirm], [data-runtime-replace-cancel]",
    )
    .forEach((el) => {
      el.disabled = busy;
    });
}
function renderRuntimeView() {
  const servers = $("runtime-servers");
  const workers = $("runtime-workers");
  const attestation = $("runtime-attestation");
  const select = $("runtime-worker-server");
  if (!servers || !workers || !attestation) return;
  if (!state.connected || state.runtimeUnavailable || !state.runtime) {
    const message = !state.connected
      ? '<p class="notice error">Backend disconnected. Runtime configuration is unavailable until the connection returns.</p>'
      : '<p class="notice error">Runtime configuration is unavailable. Prior live observations are not current evidence.</p>';
    servers.innerHTML = message;
    workers.innerHTML = message;
    attestation.innerHTML = message;
    runtimeControls();
    return;
  }
  servers.innerHTML = renderRuntimeServers(state.runtime, {
    tests: state.runtimeServerTests,
    attestation: state.runtimeAttestation,
    busy: state.runtimeBusy,
  });
  workers.innerHTML = renderRuntimeWorkers(state.runtime, {
    attestation: state.runtimeAttestation,
    identityResults: state.runtimeIdentityResults,
    replacePending: state.runtimeReplacePending,
    registryIds: state.workers.map((worker) => worker.worker_id),
    busy: state.runtimeBusy,
  });
  attestation.innerHTML = renderRuntimeAttestation(state.runtimeAttestation);
  if (select) {
    const previous = select.value;
    select.innerHTML = renderRuntimeServerOptions(state.runtime?.ollama_servers);
    if ([...select.options].some((option) => option.value === previous))
      select.value = previous;
  }
  runtimeControls();
}
async function loadRuntime() {
  if (!state.connected) {
    rejectRuntimeSnapshot(state);
    renderRuntimeView();
    return;
  }
  try {
    const snapshot = await api.runtime();
    acceptRuntimeSnapshot(state, snapshot);
    renderRuntimeView();
  } catch (error) {
    rejectRuntimeSnapshot(state);
    renderRuntimeView();
    $("runtime-servers").innerHTML =
      `<p class="notice error">${esc(error.message)}</p>`;
    $("runtime-workers").innerHTML =
      `<p class="notice error">${esc(error.message)}</p>`;
  }
}
async function runtimeAction(work, success) {
  if (state.runtimeBusy) return;
  state.runtimeBusy = true;
  runtimeControls();
  try {
    await work();
    if (success) notice(success);
  } catch (error) {
    notice(error.message, true);
  } finally {
    state.runtimeBusy = false;
    renderRuntimeView();
  }
}
async function mutateRuntime(work) {
  try {
    await work();
  } catch (error) {
    rejectRuntimeSnapshot(state);
    throw error;
  }
}
async function observeRuntimeAttest() {
  beginRuntimeObservation(state);
  try {
    const result = await api.runtimeAttest();
    acceptRuntimeAttest(state, result);
  } catch (error) {
    clearRuntimeEvidence(state);
    throw error;
  }
}
async function observeServerTest(serverId) {
  invalidateServerTest(state, serverId);
  try {
    const result = await api.testOllamaServer(serverId);
    acceptServerTest(state, result);
  } catch (error) {
    invalidateServerTest(state, serverId);
    throw error;
  }
}
async function loadIntelligence() {
  if (!state.connected) {
    $("intel-badge").textContent = "DISCONNECTED";
    $("intel-badge").className = "badge muted";
    $("intel-status").innerHTML =
      '<p class="notice error">Backend disconnected. Repository intelligence is unavailable until the connection returns.</p>';
    return;
  }
  try {
    const status = await api.intelligenceStatus();
    $("intel-badge").textContent = intelStatusLabel(status);
    $("intel-badge").className = `badge ${intelStatusClass(status)}`;
    $("intel-status").innerHTML = renderIntelStatus(status);
    $("intel-projects").innerHTML = renderIntelProjects(status.projects);
    $("intel-commands").innerHTML = renderIntelCommands(status.commands);
  } catch (error) {
    $("intel-status").innerHTML =
      `<p class="notice error">${esc(error.message)}</p>`;
  }
}
function planControls() {
  const planningReady =
    state.connected && state.health?.actions.planning_configured;
  $("plan-submit").disabled = !planningReady || state.planBusy;
  const plan = state.selectedPlan;
  $("plan-resume").disabled =
    !planningReady || state.planBusy || !plan || plan.state !== "NEEDS_INPUT";
  $("plan-replan").disabled =
    !planningReady || state.planBusy || !plan || plan.state === "SUPERSEDED";
  $("plan-help").textContent = planningReady
    ? ""
    : "Configure a server-side Planner before creating plans.";
}
async function selectPlan(planId) {
  state.selectedPlanId = planId;
  $("plan-heading").textContent = planId;
  $("plan-state-badge").innerHTML = "";
  $("plan-detail").textContent = "Loading plan…";
  $("plan-questions").innerHTML = "";
  try {
    const plan = await api.plan(planId);
    if (state.selectedPlanId !== planId) return;
    state.selectedPlan = plan;
    const job =
      state.activeJob && state.activeJob.plan_id === planId
        ? state.activeJob
        : null;
    $("plan-heading").textContent =
      (plan.content && plan.content.goal) || plan.plan_id;
    $("plan-state-badge").innerHTML = planBadge(plan.effective_state);
    $("plan-detail").innerHTML = renderPlanDetail(plan, job);
    $("plan-questions").innerHTML = renderPlanQuestions(plan);
    $("plan-list").innerHTML = renderPlanList(
      state.plans,
      state.selectedPlanId,
    );
  } catch (error) {
    $("plan-detail").innerHTML =
      `<p class="notice error">${esc(error.message)}</p>`;
  } finally {
    planControls();
  }
}
// Phase 8.2d: a background planning job outlives any one HTTP request or
// browser tab. This poll is purely a UI convenience for showing progress
// promptly while the tab is open -- backend execution never depends on
// it running. It pauses (a longer interval) while hidden rather than
// stopping outright, and `visibilitychange`/`pageshow` below always
// force one immediate refresh the moment the tab is foregrounded again,
// so a returning user never has to wait out a stale interval.
async function pollActiveJob() {
  if (!state.activeJob) return;
  const jobId = state.activeJob.job_id;
  try {
    const job = await api.planningJob(jobId);
    if (!state.activeJob || state.activeJob.job_id !== jobId) return;
    state.activeJob = job;
    if (state.selectedPlanId === job.plan_id) {
      $("plan-state-badge").innerHTML =
        jobBadge(job.state) +
        " " +
        planBadge(state.selectedPlan?.effective_state || "DRAFT");
    }
    if (job.state === "SUCCEEDED" || job.state === "FAILED") {
      notice(
        job.state === "SUCCEEDED"
          ? "Planning attempt finished."
          : "Planning attempt failed.",
        job.state === "FAILED",
      );
      await loadPlanning();
      if (state.selectedPlanId === job.plan_id) await selectPlan(job.plan_id);
      state.activeJob = null;
      return;
    }
  } catch (error) {
    notice(error.message, true);
  }
  if (state.activeJob) {
    clearTimeout(activeJobTimer);
    activeJobTimer = setTimeout(pollActiveJob, document.hidden ? 15000 : 2000);
  }
}
function trackJob(job) {
  state.activeJob = job;
  clearTimeout(activeJobTimer);
  pollActiveJob();
}
async function loadPlanning() {
  if (!state.connected) {
    $("planning-badge").textContent = "DISCONNECTED";
    $("planning-badge").className = "badge muted";
    $("plan-list").innerHTML =
      '<p class="notice error">Backend disconnected. Planning is unavailable until the connection returns.</p>';
    return;
  }
  const planningReady = state.health?.actions.planning_configured;
  $("planning-badge").textContent = planningReady ? "READY" : "NOT CONFIGURED";
  $("planning-badge").className = `badge ${planningReady ? "ok" : "muted"}`;
  try {
    const data = await api.plans();
    state.plans = data.plans;
    $("plan-list").innerHTML = renderPlanList(
      state.plans,
      state.selectedPlanId,
    );
    if (state.selectedPlanId) await selectPlan(state.selectedPlanId);
  } catch (error) {
    $("plan-list").innerHTML =
      `<p class="notice error">${esc(error.message)}</p>`;
  } finally {
    planControls();
  }
}
async function loadPrivacy() {
  if (!state.connected) {
    $("privacy-badge").textContent = "DISCONNECTED";
    $("privacy-badge").className = "badge muted";
    $("privacy-pending").innerHTML =
      '<p class="notice error">Backend disconnected. Permission state is unavailable until the connection returns.</p>';
    rejectCertificationSnapshot(state);
    renderCertificationView();
    return;
  }
  try {
    const [requests, grants] = await Promise.all([
      api.permissionRequests(),
      api.permissionGrants(),
    ]);
    state.permissionRequests = requests.requests;
    state.permissionGrants = grants.grants;
    const pendingCount = state.permissionRequests.filter(
      (r) => r.state === "PENDING",
    ).length;
    $("privacy-badge").textContent = pendingCount
      ? `${pendingCount} PENDING`
      : "NO PENDING REQUESTS";
    $("privacy-badge").className = `badge ${pendingCount ? "warning" : "ok"}`;
    $("privacy-pending").innerHTML = renderPendingPermissionRequests(
      state.permissionRequests,
    );
    $("privacy-active").innerHTML = renderActivePermissionGrants(
      state.permissionGrants,
    );
    $("privacy-history").innerHTML = renderPermissionHistory(
      state.permissionRequests,
      state.permissionGrants,
    );
  } catch (error) {
    $("privacy-pending").innerHTML =
      `<p class="notice error">${esc(error.message)}</p>`;
  }
  await loadCertification();
}
function certificationControls() {
  const busy = state.certificationBusy || !state.connected;
  document
    .querySelectorAll("[data-cert-preflight], [data-cert-start], [data-cert-evidence]")
    .forEach((el) => {
      if (el.hasAttribute("data-cert-start")) {
        el.disabled = busy || state.selectedCertificationWorker?.ready_for_certification !== true;
        return;
      }
      el.disabled = busy;
    });
}
function renderCertificationView() {
  const workers = $("cert-workers");
  const detail = $("cert-detail");
  const run = $("cert-run");
  const evidence = $("cert-evidence");
  const badgeEl = $("cert-badge");
  if (!workers || !detail || !run || !evidence) return;
  if (!state.connected || state.certificationUnavailable) {
    const message = !state.connected
      ? '<p class="notice error">Backend disconnected. Certification state is unavailable until the connection returns.</p>'
      : '<p class="notice error">Certification projection is unavailable. Prior certification observations are not current evidence.</p>';
    workers.innerHTML = message;
    detail.innerHTML = message;
    run.innerHTML = "";
    evidence.innerHTML = "";
    if (badgeEl) {
      badgeEl.textContent = "UNAVAILABLE";
      badgeEl.className = "badge muted";
    }
    return;
  }
  workers.innerHTML = renderCertificationWorkers(
    {
      environment: state.certificationEnvironment,
      workers: state.certificationWorkers,
    },
    state.selectedCertificationWorkerId,
  );
  detail.innerHTML = renderCertificationWorkerDetail(state.selectedCertificationWorker, {
    busy: state.certificationBusy,
    registryIds: state.workers.map((worker) => worker.worker_id),
  });
  if (state.selectedCertificationWorker) {
    detail.innerHTML += renderCertificationHistory(state.certificationHistory);
  }
  run.innerHTML = renderCertificationRun(state.certificationActiveRun);
  evidence.innerHTML = renderCertificationEvidence(
    state.certificationEvidence,
    state.certificationEvidenceError,
  );
  if (badgeEl) {
    const count = (state.certificationWorkers || []).length;
    badgeEl.textContent = `${count} WORKERS`;
    badgeEl.className = "badge";
  }
  certificationControls();
}
async function loadCertification() {
  if (!state.connected) {
    rejectCertificationSnapshot(state);
    renderCertificationView();
    return;
  }
  try {
    const payload = await api.certificationWorkers();
    acceptCertificationWorkers(state, payload);
    renderCertificationView();
    if (state.selectedCertificationWorkerId) {
      await loadCertificationWorker(
        state.selectedCertificationWorkerId,
        state.certificationSelectionVersion,
      );
    }
  } catch (error) {
    rejectCertificationSnapshot(state);
    renderCertificationView();
    if ($("cert-workers")) {
      $("cert-workers").innerHTML =
        `<p class="notice error">${esc(error.message)}</p>`;
    }
  }
}
async function loadCertificationWorker(workerId, version) {
  try {
    const detail = await api.certificationWorker(workerId);
    if (!acceptCertificationWorker(state, workerId, detail, version)) return;
    renderCertificationView();
  } catch (error) {
    if (state.selectedCertificationWorkerId !== workerId) return;
    if (version != null && version !== state.certificationSelectionVersion) return;
    state.selectedCertificationWorker = null;
    state.certificationHistory = null;
    renderCertificationView();
    if ($("cert-detail")) {
      $("cert-detail").innerHTML =
        `<p class="notice error">${esc(error.message)}</p>`;
    }
  }
}
function scheduleCertificationPoll() {
  clearTimeout(certificationTimer);
  if (!shouldContinueCertificationPoll(state.certificationActiveRun)) return;
  certificationTimer = setTimeout(() => {
    pollCertificationRun();
  }, certificationPollDelay(document.hidden));
}
async function pollCertificationRun() {
  const runId = state.certificationActiveRun?.run_id;
  const workerId = state.selectedCertificationWorkerId;
  const version = state.certificationSelectionVersion;
  if (!runId) return;
  try {
    const run = await api.certificationRun(runId);
    if (!applyCertificationPoll(state, run)) return;
    renderCertificationView();
    if (isCertificationTerminal(run.state)) {
      if (workerId) await loadCertificationWorker(workerId, version);
      return;
    }
  } catch (error) {
    notice(error.message, true);
  }
  scheduleCertificationPoll();
}
async function certificationAction(work, success) {
  if (state.certificationBusy) return;
  state.certificationBusy = true;
  certificationControls();
  try {
    await work();
    if (success) notice(success);
  } catch (error) {
    notice(error.message, true);
  } finally {
    state.certificationBusy = false;
    renderCertificationView();
  }
}
async function selectedDetail() {
  if (!state.selected) return;
  const id = state.selected,
    version = selectionVersion;
  const [run, audit] = await Promise.all([api.run(id), api.audit(id)]);
  if (id !== state.selected || version !== selectionVersion) return;
  state.run = run;
  $("run-heading").textContent = run.run_id;
  $("run-badge").textContent = run.status;
  $("run-badge").className = `badge ${statusClass(run.status)}`;
  $("run-detail").innerHTML = renderRun(run);
  $("run-advanced").textContent = JSON.stringify(run, null, 2);
  // Preserve a draft answer across polls while its durable question is unchanged.
  const key = JSON.stringify([id, run.questions]);
  if (key !== state.questionKey) {
    $("questions").innerHTML = renderQuestions(run);
    state.questionKey = key;
  }
  $("current-worker").textContent = run.worker_id;
  $("worker-network").textContent =
    state.workers.find((w) => w.worker_id === run.worker_id)?.network_class ||
    "Unknown";
  $("audit-preview").innerHTML = renderAudit(audit.events.slice(-5));
  $("audit-timeline").innerHTML = renderAudit(audit.events);
  await workerDetail(state.workerId || run.worker_id);
  controls();
}
function showRuns() {
  $("runs-list").innerHTML = renderRuns(state.runs, state.selected);
  $("run-count").textContent = state.runs.length;
  $("load-more").hidden = state.nextOffset === null;
}
async function refresh() {
  if (state.refreshing || state.busy) return;
  state.refreshing = true;
  controls();
  try {
    const [health, project, runs, workers] = await Promise.all([
      api.health(),
      api.project(),
      api.runs(),
      api.workers(),
    ]);
    if (
      health.api_version !== 1 ||
      !Array.isArray(runs.runs) ||
      !Array.isArray(workers.workers)
    )
      throw new Error("Unsupported backend API contract.");
    state.health = health;
    state.workers = workers.workers;
    // Keep explicitly loaded older runs visible, updating the newest page in place.
    const latest = new Set(runs.runs.map((r) => r.run_id));
    state.runs = [
      ...runs.runs,
      ...state.runs.filter((r) => !latest.has(r.run_id)),
    ];
    state.nextOffset = runs.next_offset === null ? null : state.runs.length;
    if (!state.selected && state.runs.length)
      state.selected = state.runs[0].run_id;
    $("project-name").textContent = project.display_name;
    $("project-head").textContent = project.head?.slice(0, 10) || "Unborn";
    $("project-branch").textContent =
      project.branch || (project.detached ? "Detached HEAD" : "No branch yet");
    $("project-detail").innerHTML =
      `<div class="project-row"><div><strong>${esc(project.display_name)}</strong><div class="muted-text">${esc(project.repository_path)}</div></div>${badge("LOCAL")}</div><details><summary>Repository identity</summary><pre>${esc(JSON.stringify(project, null, 2))}</pre></details>`;
    $("backend-version").textContent =
      `v${health.source_version} · schema ${health.schema_version}`;
    const selectedWorker = $("worker").value;
    $("worker").innerHTML = state.workers
      .map(
        (worker) =>
          `<option value="${esc(worker.worker_id)}">${esc(worker.worker_id)} (${esc(worker.network_class)})</option>`,
      )
      .join("");
    if (state.workers.some((w) => w.worker_id === selectedWorker))
      $("worker").value = selectedWorker;
    $("workers-list").innerHTML = state.workers.length
      ? state.workers
          .map(
            (w) => `
              <div
                class="model-card ${w.worker_id === state.workerId ? "selected" : ""}"
                data-worker="${esc(w.worker_id)}"
              >

                <div class="model-card-top">
                  <div class="model-title">
                    ${esc(workerAlias(w.worker_id))}
                  </div>

                  ${badge(w.availability_state)}
                </div>

                <div class="model-meta">
                  ${esc(w.kind)} · ${esc(w.network_class)}
                </div>

                <div class="model-role">
                  coder
                </div>

              </div>
            `,
          )
          .join("")
      : '<p class="muted-text">No registered workers.</p>';
    $("start-help").textContent = !health.actions.start
      ? "A server-side PromptAnalyst must be configured before starting runs."
      : !state.workers.length
        ? "Register a worker through the backend before starting."
        : !health.actions.execution_configured
          ? "Runs can be analyzed; configure a server-side worker adapter to execute."
          : "Read-only profile. Cloud workers require separate explicit backend authorization, unavailable in this UI.";
    showRuns();
    if (state.selected) await selectedDetail();
    else {
      $("run-heading").textContent = "No run selected";
      $("run-detail").innerHTML = renderRun(null);
    }
    connected(true);
    notice(connectionText("connected"));
  } catch (error) {
    connected(false);
    notice(`${connectionText("disconnected")} ${error.message}`, true);
  } finally {
    state.refreshing = false;
    controls();
  }
}
async function action(work, success) {
  if (state.busy) return;
  state.busy = true;
  controls();
  notice("Waiting for backend action…");
  let message,
    failed = false;
  try {
    await work();
    message = success;
  } catch (error) {
    message = error.message;
    failed = true;
  } finally {
    state.busy = false;
    await refresh();
    notice(message, failed);
    controls();
  }
}
$("new-run").addEventListener("click", () => {
  view("tasks");
  $("prompt").focus();
});
$("refresh").addEventListener("click", refresh);
$("start-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if ($("start-submit").disabled) return;
  action(async () => {
    const run = await api.start(
      $("prompt").value,
      $("worker").value,
      $("role").value,
    );
    state.selected = run.run_id;
    selectionVersion++;
    $("prompt").value = "";
    view("dashboard");
  }, "Run recorded. Status comes from the backend.");
});
$("resume").addEventListener("click", () => {
  const id = state.selected;
  action(
    () => api.resume(id),
    "Resume request finished; current backend status is shown.",
  );
});
$("questions").addEventListener("submit", (event) => {
  const form = event.target.closest("form[data-question]");
  if (!form) return;
  event.preventDefault();
  const id = state.selected;
  const data = new FormData(form);
  action(
    () =>
      api.resolve(
        id,
        form.dataset.question,
        data.get("answer"),
        data.get("resolution_kind"),
      ),
    "Answer recorded. Resume to let QuestionGate re-evaluate.",
  );
});
$("runs-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-run]");
  if (!button || state.busy || state.refreshing) return;
  state.selected = button.dataset.run;
  state.run = null;
  selectionVersion++;
  $("questions").innerHTML = "";
  state.questionKey = null;
  controls();
  $("run-detail").textContent = "Loading run…";
  $("run-heading").textContent = state.selected;
  $("run-badge").textContent = "LOADING";
  showRuns();
  try {
    await selectedDetail();
  } catch (error) {
    notice(error.message, true);
  }
});
$("workers-list").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-worker]");
  if (button)
    try {
      await workerDetail(button.dataset.worker);
    } catch (error) {
      notice(error.message, true);
    }
});
$("runtime-attest").addEventListener("click", () => {
  runtimeAction(observeRuntimeAttest, "Live runtime attestation recorded from the backend.");
});
$("ollama-server-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const serverId = $("ollama-server-id").value.trim();
  const origin = $("ollama-server-origin").value.trim();
  if (!serverId || !origin || state.runtimeBusy) return;
  runtimeAction(() => mutateRuntime(async () => {
    const snapshot = await api.addOllamaServer(serverId, origin);
    acceptRuntimeSnapshot(state, snapshot);
    $("ollama-server-form").reset();
  }), "Ollama server saved from the backend.");
});
$("runtime-worker-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.runtimeBusy) return;
  const payload = runtimeRegistrationPayload({
    worker_id: $("runtime-worker-id").value,
    ollama_server_id: $("runtime-worker-server").value,
    model_tag: $("runtime-worker-model").value,
    kind: $("runtime-worker-kind").value,
    network_class: $("runtime-worker-network").value,
    effective_context_tokens: $("runtime-worker-context").value,
    temperature: $("runtime-worker-temperature").value,
    normalizer_id: $("runtime-worker-normalizer-id").value,
    normalizer_version: $("runtime-worker-normalizer-version").value,
  });
  if (!payload.worker_id || !payload.ollama_server_id || !payload.model_tag)
    return;
  runtimeAction(() => mutateRuntime(async () => {
    const snapshot = await api.registerRuntimeWorker(payload);
    acceptRuntimeSnapshot(state, snapshot);
    $("runtime-worker-form").reset();
  }), "Runtime worker saved from the backend.");
});
$("runtime-stack").addEventListener("click", (event) => {
  const testButton = event.target.closest("[data-runtime-test-server]");
  const approveButton = event.target.closest("[data-runtime-approve]");
  const replaceAsk = event.target.closest("[data-runtime-replace-ask]");
  const replaceConfirm = event.target.closest("[data-runtime-replace-confirm]");
  const replaceCancel = event.target.closest("[data-runtime-replace-cancel]");
  if (testButton) {
    runtimeAction(() => observeServerTest(testButton.dataset.runtimeTestServer));
    return;
  }
  if (approveButton) {
    const workerId = approveButton.dataset.runtimeApprove;
    runtimeAction(() => mutateRuntime(async () => {
      const result = await api.approveRuntimeWorker(workerId);
      const snapshot = await api.runtime();
      acceptIdentityResult(state, workerId, result, snapshot);
    }));
    return;
  }
  if (replaceAsk) {
    if (state.runtimeBusy) return;
    state.runtimeReplacePending = replaceAsk.dataset.runtimeReplaceAsk;
    renderRuntimeView();
    return;
  }
  if (replaceCancel) {
    state.runtimeReplacePending = null;
    renderRuntimeView();
    return;
  }
  if (replaceConfirm) {
    const workerId = replaceConfirm.dataset.runtimeReplaceConfirm;
    runtimeAction(() => mutateRuntime(async () => {
      const result = await api.approveNewRuntimeIdentity(workerId);
      const snapshot = await api.runtime();
      acceptIdentityResult(state, workerId, result, snapshot);
    }));
  }
});
$("intel-refresh").addEventListener("click", async () => {
  if (state.intelBusy || !state.connected) return;
  state.intelBusy = true;
  $("intel-refresh").disabled = true;
  try {
    await api.intelligenceRefresh();
    await loadIntelligence();
  } catch (error) {
    notice(error.message, true);
  } finally {
    state.intelBusy = false;
    $("intel-refresh").disabled = false;
  }
});
$("intel-query-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.intelBusy || !state.connected) return;
  const text = $("intel-query-text").value;
  if (!text.trim()) return;
  state.intelBusy = true;
  $("intel-results").textContent = "Searching…";
  try {
    $("intel-results").innerHTML = renderIntelResults(
      await api.intelligenceQuery(text),
    );
  } catch (error) {
    $("intel-results").innerHTML =
      `<p class="notice error">${esc(error.message)}</p>`;
  } finally {
    state.intelBusy = false;
  }
});
$("plan-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.planBusy || $("plan-submit").disabled) return;
  const text = $("plan-request").value;
  if (!text.trim()) return;
  state.planBusy = true;
  planControls();
  notice(
    "Planning job accepted; the server will keep working even if you leave this page.",
  );
  try {
    const job = await api.createPlan(text);
    $("plan-request").value = "";
    await loadPlanning();
    await selectPlan(job.plan_id);
    trackJob(job);
  } catch (error) {
    notice(error.message, true);
  } finally {
    state.planBusy = false;
    planControls();
  }
});
$("plan-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-plan]");
  if (!button || state.planBusy) return;
  selectPlan(button.dataset.plan);
});
$("plan-resume").addEventListener("click", async () => {
  if (state.planBusy || !state.selectedPlanId) return;
  state.planBusy = true;
  planControls();
  try {
    await api.resumePlan(state.selectedPlanId);
    await selectPlan(state.selectedPlanId);
    notice("Plan resumed; Question Gate re-evaluated.");
  } catch (error) {
    notice(error.message, true);
  } finally {
    state.planBusy = false;
    planControls();
  }
});
$("plan-replan").addEventListener("click", async () => {
  if (state.planBusy || !state.selectedPlanId) return;
  state.planBusy = true;
  planControls();
  try {
    const job = await api.replanPlan(state.selectedPlanId);
    await loadPlanning();
    await selectPlan(job.plan_id);
    notice(
      "New plan revision accepted; the prior revision is marked superseded, never deleted.",
    );
    trackJob(job);
  } catch (error) {
    notice(error.message, true);
  } finally {
    state.planBusy = false;
    planControls();
  }
});
$("plan-questions").addEventListener("submit", async (event) => {
  const form = event.target.closest("form[data-plan-question]");
  if (!form || state.planBusy || !state.selectedPlanId) return;
  event.preventDefault();
  const data = new FormData(form);
  state.planBusy = true;
  planControls();
  try {
    await api.resolvePlan(
      state.selectedPlanId,
      form.dataset.planQuestion,
      data.get("answer"),
      data.get("resolution_kind"),
    );
    await selectPlan(state.selectedPlanId);
    notice("Answer recorded. Resume to let the Question Gate re-evaluate.");
  } catch (error) {
    notice(error.message, true);
  } finally {
    state.planBusy = false;
    planControls();
  }
});
async function decidePendingPermission(requestId, decision) {
  if (state.privacyBusy) return;
  state.privacyBusy = true;
  try {
    await api.decidePermission(requestId, decision);
    notice(
      decision === "ALLOW"
        ? "Permission allowed."
        : "Permission request declined.",
    );
    await loadPrivacy();
  } catch (error) {
    notice(error.message, true);
  } finally {
    state.privacyBusy = false;
  }
}
$("privacy-pending").addEventListener("click", (event) => {
  const allowButton = event.target.closest("[data-permission-allow]");
  const denyButton = event.target.closest("[data-permission-deny]");
  if (allowButton)
    decidePendingPermission(allowButton.dataset.permissionAllow, "ALLOW");
  else if (denyButton)
    decidePendingPermission(denyButton.dataset.permissionDeny, "DENY");
});
$("privacy-active").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-permission-revoke]");
  if (!button || state.privacyBusy) return;
  state.privacyBusy = true;
  try {
    await api.revokePermission(button.dataset.permissionRevoke);
    notice("Permission revoked.");
    await loadPrivacy();
  } catch (error) {
    notice(error.message, true);
  } finally {
    state.privacyBusy = false;
  }
});
$("cert-stack").addEventListener("click", (event) => {
  const workerButton = event.target.closest("[data-cert-worker]");
  const preflightButton = event.target.closest("[data-cert-preflight]");
  const startButton = event.target.closest("[data-cert-start]");
  const evidenceButton = event.target.closest("[data-cert-evidence]");
  if (workerButton) {
    const workerId = workerButton.dataset.certWorker;
    const version = selectCertificationWorkerId(state, workerId);
    renderCertificationView();
    loadCertificationWorker(workerId, version);
    return;
  }
  if (preflightButton) {
    const workerId = preflightButton.dataset.certPreflight;
    certificationAction(async () => {
      beginCertificationEvidenceRequest(state, null);
      try {
        await api.certificationPreflight(workerId);
        await loadCertificationWorker(workerId, state.certificationSelectionVersion);
      } catch (error) {
        await loadCertificationWorker(workerId, state.certificationSelectionVersion);
        throw error;
      }
    }, "Baseline Security preflight recorded.");
    return;
  }
  if (startButton) {
    const workerId = startButton.dataset.certStart;
    if (state.selectedCertificationWorker?.ready_for_certification !== true) return;
    certificationAction(async () => {
      beginCertificationRun(state, null);
      try {
        const run = await api.startBaselineCertification(workerId);
        beginCertificationRun(state, run);
        renderCertificationView();
        scheduleCertificationPoll();
      } catch (error) {
        beginCertificationRun(state, null);
        throw error;
      }
    }, "Baseline Security certification accepted. Closing this browser does not cancel the run.");
    return;
  }
  if (evidenceButton) {
    const runId = evidenceButton.dataset.certEvidence;
    certificationAction(async () => {
      beginCertificationEvidenceRequest(state, runId);
      renderCertificationView();
      try {
        const evidence = await api.certificationEvidence(runId);
        if (!acceptCertificationEvidence(state, evidence, runId)) {
          rejectCertificationEvidence(state, runId, "Evidence is not bound to the requested run.");
        }
      } catch (error) {
        rejectCertificationEvidence(state, runId, error.message);
        throw error;
      }
    });
  }
});
$("load-more").addEventListener("click", async () => {
  $("load-more").disabled = true;
  try {
    const data = await api.runs(state.nextOffset);
    const known = new Set(state.runs.map((run) => run.run_id));
    state.runs.push(...data.runs.filter((run) => !known.has(run.run_id)));
    state.nextOffset = data.next_offset;
    showRuns();
  } catch (error) {
    notice(error.message, true);
  } finally {
    $("load-more").disabled = false;
  }
});
async function poll() {
  await refresh();
  timer = setTimeout(
    poll,
    pollDelay(state.run?.status, state.connected, document.hidden),
  );
}
window.addEventListener("pagehide", () => {
  clearTimeout(timer);
  clearTimeout(certificationTimer);
});
// Phase 8.2d: backend planning execution is fully independent of this
// tab's lifetime, so a paused/backgrounded/suspended timer never risks
// losing anything -- but the moment the user comes back, refresh
// immediately rather than waiting out whatever interval was in flight.
function refreshOnReturn() {
  if (document.hidden) return;
  if (state.activeJob) pollActiveJob();
  else if (document.getElementById("planning")?.classList.contains("active"))
    loadPlanning();
  if (document.getElementById("privacy")?.classList.contains("active"))
    loadPrivacy();
  if (document.getElementById("models")?.classList.contains("active"))
    loadRuntime();
  if (shouldContinueCertificationPoll(state.certificationActiveRun))
    scheduleCertificationPoll();
}
document.addEventListener("visibilitychange", refreshOnReturn);
window.addEventListener("pageshow", refreshOnReturn);
poll();
