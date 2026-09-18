import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createAPI, APIError } from "../static/api.js";
import { workerAlias, saveWorkerAlias } from "../static/aliases.js";
import { provenanceClass, provenanceBadge, renderRuntimeServers, renderRuntimeWorkers, renderRuntimeAttestation, renderOllamaServerTest, renderRuntimeIdentityResult, renderReplaceIdentityConfirm, renderRuntimeServerOptions, runtimeRegistrationPayload, clearRuntimeEvidence, acceptRuntimeSnapshot, rejectRuntimeSnapshot, acceptRuntimeAttest, acceptIdentityResult, attestationForWorker, beginRuntimeObservation, invalidateServerTest, acceptServerTest, identityResultBindable, certificationStateClass, certificationStateBadge, plannerEligibilityLabel, isCertificationTerminal, isCertificationActive, shouldContinueCertificationPoll, certificationPollDelay, certificationStartEnabled, certificationPreflightEnabled, certificationPromoteEnabled, certificationPlannerStartEnabled, certificationPlannerPreflightEnabled, bindCertificationEvidence, selectCertificationWorkerId, acceptCertificationWorker, beginCertificationRun, applyCertificationPoll, beginCertificationEvidenceRequest, acceptCertificationEvidence, rejectCertificationEvidence, beginCertificationPreflight, certificationSelectionMatches, acceptCertificationStart, acceptCertificationPreflight, acceptCertificationPromotion, beginCertificationPlannerPreflight, acceptCertificationPlannerPreflight, beginCertificationPlannerRun, acceptCertificationPlannerStart, applyCertificationPlannerPoll, clearCertificationTransient, renderCertificationWorkers, renderCertificationWorkerSummary, renderCertificationWorkerDetail, renderCertificationEligibility, renderCertificationRoles, renderCertificationPreflight, renderCertificationRun, renderCertificationHistory, renderCertificationEvidence, beginSystemRequest, acceptSystemSnapshot, rejectSystemSnapshot, beginTailscaleRequest, acceptTailscaleSnapshot, rejectTailscaleSnapshot, renderSystemSettings, renderTailscaleSettings, beginDashboardSummaryRequest, acceptDashboardSummary, rejectDashboardSummary, renderDashboardSystemSummary, renderDashboardRuntimeSummary, renderDashboardCertificationSummary, renderDashboardTailscaleSummary } from "../static/views-admin.js";
import { badge, connectionText, pollDelay, renderRun, renderRuns, renderQuestions, renderTrust, renderAudit, renderConformance, intelStatusClass, intelStatusLabel, renderIntelStatus, renderIntelProjects, renderIntelCommands, renderIntelResults, planStateClass, planBadge, renderPlanList, renderPlanAffectedFiles, renderPlanCommands, renderPlanQuestions, renderPlanDetail, jobStateClass, jobBadge, renderJobStatus, permissionSensitivityBadge, renderPermissionTechnicalDetails, renderPermissionExplanation, renderPendingPermissionRequest, renderPendingPermissionRequests, permissionGrantStateClass, permissionGrantBadge, renderActivePermissionGrants, renderPermissionHistory } from "../static/views.js";

const run = { run_id: "real-run-id", worker_id: "local-worker", role: "coder", status: "RUNNING", task_status: "IMPLEMENTING", reason: null, next_safe_action: "wait", execution_state_available: true };
const reply = (data, ok = true) => ({ ok, json: async () => data });

test("API client parses application JSON and encodes identifiers", async () => {
  const calls = [];
  const api = createAPI(async (...args) => { calls.push(args); return reply(run); });
  assert.deepEqual(await api.run("id/with space"), run);
  assert.equal(calls[0][0], "/api/runs/id%2Fwith%20space");
  assert.equal(calls[0][1].method, "GET");
});

test("API failure, invalid JSON and backend-down state are explicit", async () => {
  await assert.rejects(createAPI(async () => reply({error:{code:"not_found",message:"Missing",retryable:false}}, false)).health(), { code: "not_found", message: "Missing" });
  await assert.rejects(createAPI(async () => { throw new TypeError("Network error"); }).health(), { code: "disconnected" });
  await assert.rejects(createAPI(async () => ({ok:true,json:async()=>{throw new Error();}})).health(), { code: "invalid_response" });
  assert.match(connectionText("loading"), /Connecting/);
  assert.match(connectionText("disconnected"), /stale/);
});

test("start, human answer and resume use only application choices", async () => {
  const calls = [];
  const api = createAPI(async (url, options) => { calls.push([url, options.method, JSON.parse(options.body)]); return reply(run); });
  await api.start("Read README", "worker", "coder");
  await api.resolve("run", "ambiguity", "README only", "FACT");
  await api.resume("run");
  assert.deepEqual(calls, [
    ["/api/runs", "POST", {prompt:"Read README",worker_id:"worker",role:"coder",capability_profile:"read_only"}],
    ["/api/runs/run/resolutions", "POST", {ambiguity_id:"ambiguity",answer:"README only",resolution_kind:"FACT"}],
    ["/api/runs/run/resume", "POST", {}],
  ]);
});

test("failed mutation is not automatically retried", async () => {
  let calls = 0;
  const api = createAPI(async () => { calls++; throw new Error("lost response"); });
  await assert.rejects(api.resume("run"), (e) => e instanceof APIError && /Refresh durable/.test(e.message));
  assert.equal(calls, 1);
});

test("active, completed, failed and interrupted runs render authoritative status", () => {
  assert.match(renderRuns([run], run.run_id), /RUNNING/);
  assert.match(renderRun(run), /IMPLEMENTING/);
  for (const status of ["COMPLETED", "FAILED", "INTERRUPTED_RESUMABLE", "DENIED_TRUST"]) {
    assert.match(renderRuns([{...run,status}], null), new RegExp(status));
    assert.match(renderRun({...run,task_status:status}), new RegExp(status));
  }
  assert.match(renderRuns([], null), /No durable runs/);
  assert.match(renderRun(null), /Select a run/);
});

test("blocked question form carries exact ambiguity and explicit answer kinds", () => {
  const html = renderQuestions({...run,status:"BLOCKED_ON_QUESTIONS",questions:[{ambiguity_id:"scope",question:"Which files?",risk_class:"DESTRUCTIVE",answer_recorded:true}]});
  assert.match(html, /data-question="scope"/);
  assert.match(html, /Which files\?/);
  assert.match(html, /value="FACT"/);
  assert.match(html, /value="AUTHORIZATION"/);
  assert.match(html, /resume to re-evaluate/);
  assert.doesNotMatch(html, /SAFE_DEFAULT|cloud|checked/);
});

test("trust badges display exact scopes without granting broader trust", () => {
  const html = renderTrust({scopes:[{role:"coder",capability:"read_file",level:"GUARDED"},{role:"coder",capability:"write_file",level:"LOCKED"}]});
  assert.match(html, /read_file.*GUARDED/s);
  assert.match(html, /write_file.*LOCKED/s);
  assert.match(badge("LOCKED"), /muted/);
});

test("audit and conformance render recorded results and historical suite labels", () => {
  assert.match(renderAudit([{occurred_at:"now",event_type:"QUESTION_GATE_DECISION",details:{decision:"ASK"},association:"shared_prompt_identity"}]), /Shared prompt evidence/);
  assert.match(renderConformance({current_suite_version:"v2",runs:[{role:"coder",suite_version:"v1",status:"PASSED",results:[{case_name:"read",reason:"passed",passed:true}]}]}), /historical suite/);
});

test("untrusted values are escaped everywhere", () => {
  const attack = '<img src=x onerror="alert(1)">';
  assert.doesNotMatch(renderRun({...run, reason:attack}), /<img/);
  assert.doesNotMatch(badge(attack), /<img/);
  assert.doesNotMatch(renderQuestions({questions:[{ambiguity_id:attack,question:attack,risk_class:attack}]}), /<img/);
});

test("polling slows for terminal, disconnected and hidden views", () => {
  assert.equal(pollDelay("RUNNING", true, false), 4000);
  for (const status of ["COMPLETED", "FAILED", "INTERRUPTED_RESUMABLE", "BLOCKED_ON_QUESTIONS"]) assert.equal(pollDelay(status,true,false),15000);
  assert.equal(pollDelay("RUNNING",false,false),30000);
  assert.equal(pollDelay("RUNNING",true,true),30000);
});

test("production HTML has no mock metrics or machine-specific metadata", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  const js = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  assert.doesNotMatch(html, /63 \/ 63|Foundation tagged|37fa23b|\/home\/onos|codeslayer-v0.1-foundation/);
  assert.match(html, /Loading project/);
  assert.match(html, /start-form/);
  for (const action of ["api.start(", "api.resolve(", "api.resume(", "api.project(", "api.trust(", "api.conformance(", "api.audit(", "api.intelligenceStatus(", "api.intelligenceQuery(", "api.createPlan(", "api.resumePlan(", "api.replanPlan(", "api.resolvePlan(", "api.planningJob("]) assert.ok(js.includes(action));
  assert.doesNotMatch(js, /sqlite|localStorage|indexedDB|ToolExecutor|fetch\(/);
});

// --- Phase 8.1: repository intelligence ------------------------------------

test("intelligence API client requests only text/limit fields, never a filesystem or DB path", async () => {
  const calls = [];
  const api = createAPI(async (url, options) => { calls.push([url, options.method, options.body ? JSON.parse(options.body) : undefined]); return reply({}); });
  await api.intelligenceStatus();
  await api.intelligenceRefresh();
  await api.intelligenceQuery("find the billing module");
  await api.intelligenceContextPack("find the billing module");
  assert.deepEqual(calls, [
    ["/api/intelligence/status", "GET", undefined],
    ["/api/intelligence/refresh", "POST", {}],
    ["/api/intelligence/query", "POST", {text: "find the billing module"}],
    ["/api/intelligence/context-pack", "POST", {text: "find the billing module"}],
  ]);
  for (const [, , body] of calls) {
    if (!body) continue;
    for (const key of Object.keys(body)) assert.ok(["text"].includes(key), `unexpected field: ${key}`);
  }
});

test("intelligence status renders indexed/current/stale states without a live model probe", () => {
  const notIndexed = {indexed: false, current: false};
  assert.equal(intelStatusLabel(notIndexed), "NOT INDEXED");
  assert.equal(intelStatusClass(notIndexed), "muted");
  assert.match(renderIntelStatus(notIndexed), /not been indexed/);

  const current = {indexed: true, current: true, head_sha: "deadbeef00", working_tree_dirty: false, file_count: 42, inventory_truncated: false, created_at: "now"};
  assert.equal(intelStatusLabel(current), "CURRENT");
  assert.equal(intelStatusClass(current), "ok");
  assert.match(renderIntelStatus(current), /42/);
  assert.doesNotMatch(renderIntelStatus(current), /changed since/);

  const stale = {...current, current: false};
  assert.equal(intelStatusLabel(stale), "STALE");
  assert.equal(intelStatusClass(stale), "warning");
  assert.match(renderIntelStatus(stale), /changed since/);
});

test("detected projects and discovered commands render with their evidence, never asserting execution", () => {
  const html = renderIntelProjects([{kind: "python", evidence_paths: ["pyproject.toml"], facts: {name: "demo"}}]);
  assert.match(html, /python/);
  assert.match(html, /pyproject\.toml/);
  assert.match(html, /demo/);
  assert.match(renderIntelProjects([]), /No project\/language evidence/);

  const commands = renderIntelCommands([{command: "pytest", purpose: "test", evidence_source: "pyproject.toml:[tool.pytest]", confidence: "high"}]);
  assert.match(commands, /pytest/);
  assert.match(commands, /pyproject\.toml/);
  assert.match(renderIntelCommands([]), /No test\/lint\/build commands/);
});

test("query results show ranked candidates with explicit, non-empty reasons", () => {
  const result = {stale: false, candidates: [{path: "src/billing.py", score: 100, reasons: ["path_mention:billing.py"]}]};
  const html = renderIntelResults(result);
  assert.match(html, /src\/billing\.py/);
  assert.match(html, /path_mention:billing\.py/);
  assert.doesNotMatch(renderIntelResults(null), /candidates/);
  assert.match(renderIntelResults({stale: false, candidates: []}), /No relevant files/);
  assert.match(renderIntelResults({stale: true, candidates: []}), /stale snapshot|refresh the index/i);
});

test("intelligence rendering escapes untrusted evidence values everywhere", () => {
  const attack = '<img src=x onerror="alert(1)">';
  assert.doesNotMatch(renderIntelProjects([{kind: attack, evidence_paths: [attack], facts: {}}]), /<img/);
  assert.doesNotMatch(renderIntelCommands([{command: attack, purpose: attack, evidence_source: attack, confidence: "high"}]), /<img/);
  assert.doesNotMatch(renderIntelResults({stale: false, candidates: [{path: attack, score: 1, reasons: [attack]}]}), /<img/);
});

// --- Phase 8.2: engineering planning ----------------------------------------

test("planning API client sends only the request text, never a filesystem/DB path or authority field", async () => {
  const calls = [];
  const api = createAPI(async (url, options) => { calls.push([url, options.method, options.body ? JSON.parse(options.body) : undefined]); return reply({plans: []}); });
  await api.plans();
  await api.plan("plan-1");
  await api.createPlan("Add a read-only endpoint");
  await api.resumePlan("plan-1");
  await api.replanPlan("plan-1");
  await api.resolvePlan("plan-1", "scope", "JSON", "FACT");
  await api.planningJobs();
  await api.planningJob("job-1");
  assert.deepEqual(calls, [
    ["/api/plans?limit=100&offset=0", "GET", undefined],
    ["/api/plans/plan-1", "GET", undefined],
    ["/api/plans", "POST", {request: "Add a read-only endpoint"}],
    ["/api/plans/plan-1/resume", "POST", {}],
    ["/api/plans/plan-1/replan", "POST", {}],
    ["/api/plans/plan-1/resolutions", "POST", {ambiguity_id: "scope", answer: "JSON", resolution_kind: "FACT"}],
    ["/api/planning-jobs?limit=100&offset=0", "GET", undefined],
    ["/api/planning-jobs/job-1", "GET", undefined],
  ]);
  for (const [, , body] of calls) {
    if (!body) continue;
    for (const key of Object.keys(body)) {
      assert.ok(["request", "ambiguity_id", "answer", "resolution_kind"].includes(key), `unexpected field: ${key}`);
    }
  }
});

test("plan list and state badges render effective (possibly stale) state, never only the durable state", () => {
  assert.equal(planStateClass("READY"), "ok");
  assert.equal(planStateClass("STALE"), "warning");
  assert.equal(planStateClass("NEEDS_INPUT"), "warning");
  assert.equal(planStateClass("SUPERSEDED"), "muted");
  assert.match(planBadge("STALE"), /warning/);

  const plans = [
    {plan_id: "p1", revision: 1, created_at: "now", effective_state: "READY", content: {goal: "Add endpoint"}},
    {plan_id: "p2", revision: 2, created_at: "now", effective_state: "STALE", content: {goal: "Old plan"}},
  ];
  const html = renderPlanList(plans, "p1");
  assert.match(html, /Add endpoint/);
  assert.match(html, /data-plan="p1"/);
  assert.match(html, /STALE/);
  assert.match(renderPlanList([], null), /No plans yet/);
});

test("affected files show evidence reasons and distinguish existing facts from new-file proposals", () => {
  const files = [
    {path: "README.md", action: "modify", reason: "document the endpoint", exists_in_repository: true,
      evidence: [{kind: "file_exists", key: "README.md", snapshot_id: "snap1"}]},
    {path: "new_module.py", action: "create", reason: "new module", exists_in_repository: false,
      evidence: [{kind: "file_absent", key: "new_module.py", snapshot_id: "snap1"}]},
  ];
  const html = renderPlanAffectedFiles(files);
  assert.match(html, /README\.md/);
  assert.match(html, /document the endpoint/);
  assert.match(html, /file_exists/);
  assert.match(html, /EXISTING/);
  assert.match(html, /new_module\.py/);
  assert.match(html, /file_absent/);
  assert.match(html, /PROPOSED/);
  assert.match(renderPlanAffectedFiles([]), /No affected files proposed/);
});

test("discovered commands render as evidence only, never as something executed", () => {
  const html = renderPlanCommands([{command: "pytest", purpose: "test", evidence_source: "pyproject.toml:[tool.pytest]"}]);
  assert.match(html, /pytest/);
  assert.match(html, /pyproject\.toml/);
  assert.match(renderPlanCommands([]), /never executed/i);
});

test("unresolved plan questions render a form; resolved questions do not", () => {
  const html = renderPlanQuestions({questions: [
    {ambiguity_id: "scope", question: "Which format?", risk_class: "MATERIAL", resolved: false, answer_recorded: false},
    {ambiguity_id: "done", question: "Already answered", risk_class: "ROUTINE", resolved: true, answer_recorded: true},
  ]});
  assert.match(html, /data-plan-question="scope"/);
  assert.match(html, /Which format\?/);
  assert.doesNotMatch(html, /Already answered/);
  assert.equal(renderPlanQuestions({questions: []}), "");
});

test("plan detail shows evidence reasons and repository binding, not just model prose", () => {
  const record = {
    effective_state: "READY", head_sha: "deadbeef00", working_tree_dirty: false,
    intelligence_snapshot_id: "snap1",
    content: {
      goal: "Add a read-only endpoint", requirements: ["must not mutate files"], assumptions: [],
      affected_files: [{path: "README.md", action: "modify", reason: "doc", exists_in_repository: true, evidence: []}],
      planned_changes: [{description: "edit readme", paths: ["README.md"]}],
      risks: [], verification_steps: [], discovered_commands: [], validation_issues: [],
    },
  };
  const html = renderPlanDetail(record);
  assert.match(html, /Add a read-only endpoint/);
  assert.match(html, /must not mutate files/);
  assert.match(html, /README\.md/);
  assert.doesNotMatch(html, /changed since|Replan for current/);

  const stale = {...record, effective_state: "STALE"};
  assert.match(renderPlanDetail(stale), /Replan for current evidence/);

  assert.match(renderPlanDetail(null), /Select a plan above/);
  assert.match(renderPlanDetail({effective_state: "DRAFT", reason: "evidence_validation_failed", content: null}), /evidence_validation_failed/);
});

test("planning rendering escapes untrusted evidence values everywhere", () => {
  const attack = '<img src=x onerror="alert(1)">';
  assert.doesNotMatch(renderPlanList([{plan_id: attack, revision: 1, created_at: "now", effective_state: "READY", content: {goal: attack}}], null), /<img/);
  assert.doesNotMatch(renderPlanAffectedFiles([{path: attack, action: attack, reason: attack, exists_in_repository: true, evidence: [{kind: attack, key: attack, snapshot_id: attack}]}]), /<img/);
  assert.doesNotMatch(renderPlanCommands([{command: attack, purpose: attack, evidence_source: attack}]), /<img/);
  assert.doesNotMatch(renderPlanQuestions({questions: [{ambiguity_id: attack, question: attack, risk_class: attack, resolved: false}]}), /<img/);
});

test("planning HTML/JS never wires trust, execution, or arbitrary path fields", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  const js = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  assert.match(html, /plan-form/);
  assert.match(html, /planning only/i);
  assert.doesNotMatch(js, /trust_level|lease_generation|checkpoint_id|repo_path|db_path/);
});

// --- Phase 8.2d: durable background planning jobs ---------------------------

test("job state and plan state render as distinct, never-conflated badges", () => {
  assert.equal(jobStateClass("SUCCEEDED"), "ok");
  assert.equal(jobStateClass("FAILED"), "error");
  assert.equal(jobStateClass("RUNNING"), "warning");
  assert.equal(jobStateClass("QUEUED"), "muted");
  assert.match(jobBadge("QUEUED"), /muted/);
});

test("QUEUED and RUNNING jobs render a background-execution notice, not an error", () => {
  const queued = renderJobStatus({state: "QUEUED"});
  assert.match(queued, /QUEUED/);
  assert.match(queued, /background/i);
  assert.doesNotMatch(queued, /notice error/);
  const running = renderJobStatus({state: "RUNNING"});
  assert.match(running, /RUNNING/);
  assert.doesNotMatch(running, /notice error/);
});

test("SUCCEEDED job with a READY plan and SUCCEEDED job with a NEEDS_INPUT plan render distinctly", () => {
  const readyPlan = {effective_state: "READY", content: {
    goal: "Add endpoint", requirements: [], assumptions: [], affected_files: [],
    planned_changes: [], risks: [], verification_steps: [], discovered_commands: [],
    validation_issues: [],
  }};
  const needsInputPlan = {...readyPlan, effective_state: "NEEDS_INPUT"};
  const succeededJob = {state: "SUCCEEDED", failure_category: null};

  const readyHtml = renderPlanDetail(readyPlan, succeededJob) + planBadge(readyPlan.effective_state);
  const needsInputHtml = renderPlanDetail(needsInputPlan, succeededJob) + planBadge(needsInputPlan.effective_state);
  assert.match(readyHtml, /READY/);
  assert.match(needsInputHtml, /NEEDS_INPUT/);
  // Both share the same SUCCEEDED job framing -- job success is never
  // itself asserted to mean the plan is READY.
  assert.match(renderJobStatus(succeededJob), /SUCCEEDED/);
  assert.doesNotMatch(renderJobStatus(succeededJob), /READY|NEEDS_INPUT/);
});

test("FAILED job renders its safe failure category using error-notice styling", () => {
  // `failure_category` is always one of a small, fixed, backend-owned
  // set (`PlannerFailureCategory`) -- raw model prose never reaches
  // this field at all (proven at the backend, `test_raw_planner_output_
  // never_appears_on_the_job_record`); this only checks the rendering
  // itself, which is HTML-escaping (checked separately below), not
  // content filtering.
  const html = renderJobStatus({state: "FAILED", failure_category: "non_tool_response"});
  assert.match(html, /FAILED/);
  assert.match(html, /non_tool_response/);
  assert.match(html, /notice error/);
});

test("a plan still awaiting its background job shows a waiting state, not a bare error", () => {
  const draftNoContent = {effective_state: "DRAFT", reason: null, content: null};
  const html = renderPlanDetail(draftNoContent, {state: "QUEUED"});
  assert.match(html, /Waiting for the background planning job/);
  assert.doesNotMatch(html, /notice error/);
});

test("planning API client can poll job status independent of any plan fetch", async () => {
  const calls = [];
  const api = createAPI(async (url) => { calls.push(url); return reply({job_id: "job-1", state: "RUNNING"}); });
  const job = await api.planningJob("job-1");
  assert.equal(job.state, "RUNNING");
  assert.deepEqual(calls, ["/api/planning-jobs/job-1"]);
});

test("app.js refreshes immediately on visibilitychange/pageshow rather than relying only on a foreground timer", async () => {
  const js = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  assert.match(js, /visibilitychange/);
  assert.match(js, /pageshow/);
  assert.match(js, /document\.hidden/);
});

test("app.js tracks and polls a created/replanned job rather than assuming immediate completion", async () => {
  const js = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  assert.match(js, /trackJob/);
  assert.match(js, /pollActiveJob/);
  assert.doesNotMatch(js, /FakePlanner/i);
});

test("job rendering escapes untrusted failure category values", () => {
  const attack = '<img src=x onerror="alert(1)">';
  assert.doesNotMatch(renderJobStatus({state: "FAILED", failure_category: attack}), /<img/);
});

// --- CSLR Governance Foundation, slice G2: Permission Engine consent UX -----

const definition = {
  permission_key: "network.discovery.local", semantic_version: "1", action: "discover", resource_type: "none",
  sensitivity: "HIGH", user_title: "Look for compatible devices on your local network",
  user_summary: "CSLR may search the local network for compatible devices/services.",
  what_it_does: ["Searches the local network for devices CSLR might connect to in the future."],
  what_it_does_not_do: ["Does not connect to anything.", "Does not authenticate to anything.", "Does not read files.",
    "Does not write files.", "Does not configure anything.", "Does not send data to an AI model.",
    "Does not download a model.", "Does not grant internet access."],
  data_observed: ["Advertised device/service metadata only."],
  data_retained: ["A local list of what was found and when."],
  data_transmitted: ["Nothing leaves this machine as a result of this permission alone."],
  revocable: true,
  technical_details: ["No discovery mechanism is implemented yet; this will describe the actual protocol once built."],
  implementation_reference: "src/code_slayer/permissions/definitions.py", user_selectable_scope: false,
};
const pendingRequest = {
  request_id: "req-1", created_at: "now", permission_key: "network.discovery.local", semantic_version: "1",
  resource: null, purpose: "Find compatible devices before offering to connect.", requesting_subsystem: "device-setup",
  state: "PENDING", decision: null, decided_at: null, grant_id: null, definition,
};
const activeGrant = {
  grant_id: "grant-1", request_id: "req-1", permission_key: "network.discovery.local", semantic_version: "1",
  resource: null, authority_origin: "USER_EXPLICIT", granted_at: "now", expiry: null, revoked_at: null,
  state: "ACTIVE", definition,
};

test("Privacy & Security navigation and view exist", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  assert.match(html, /data-view="privacy"/);
  assert.match(html, /id="privacy"/);
  assert.match(html, /Privacy/);
  assert.match(html, /id="privacy-pending"/);
  assert.match(html, /id="privacy-active"/);
  assert.match(html, /id="privacy-history"/);
});

test("empty permission state renders honestly, never a fabricated request/grant", () => {
  assert.match(renderPendingPermissionRequests([]), /No pending permission requests/);
  assert.match(renderActivePermissionGrants([]), /No active permissions/);
  assert.match(renderPermissionHistory([], []), /No denied or revoked permissions/);
});

test("a pending request renders its title, purpose and requesting subsystem from trusted definition metadata", () => {
  const html = renderPendingPermissionRequest(pendingRequest);
  assert.match(html, /Look for compatible devices on your local network/);
  assert.match(html, /Find compatible devices before offering to connect\./);
  assert.match(html, /device-setup/);
  assert.match(html, /data-permission-allow="req-1"/);
  assert.match(html, /data-permission-deny="req-1"/);
  const withScope = renderPendingPermissionRequest({...pendingRequest, resource: "wifi-lan"});
  assert.match(withScope, /wifi-lan/);
});

test("Allow decides ALLOW against exactly the request_id, no other field", async () => {
  const calls = [];
  const api = createAPI(async (url, options) => { calls.push([url, options.method, JSON.parse(options.body)]); return reply({request_id: "req-1", state: "ALLOWED"}); });
  await api.decidePermission("req-1", "ALLOW");
  assert.deepEqual(calls, [["/api/permissions/requests/req-1/decision", "POST", {decision: "ALLOW"}]]);
});

test("Not now/Deny decides DENY against exactly the request_id", async () => {
  const calls = [];
  const api = createAPI(async (url, options) => { calls.push([url, options.method, JSON.parse(options.body)]); return reply({request_id: "req-1", state: "DENIED"}); });
  await api.decidePermission("req-1", "DENY");
  assert.deepEqual(calls, [["/api/permissions/requests/req-1/decision", "POST", {decision: "DENY"}]]);
});

test("What will CSLR do? reveals a trusted-definition-driven explanation, not model prose", () => {
  const html = renderPendingPermissionRequest(pendingRequest);
  assert.match(html, /What will CSLR do\?/);
  assert.match(html, /Searches the local network for devices CSLR might connect to in the future\./);
  assert.match(html, /Does not connect to anything\./);
  assert.match(html, /Does not send data to an AI model\./);
  assert.match(html, /Advertised device\/service metadata only\./);
  assert.match(html, /Nothing leaves this machine as a result of this permission alone\./);
  assert.match(html, /can be revoked at any time/);
  assert.match(renderPermissionExplanation(null), /notice error/);
});

test("technical details show the exact permission key, semantic version and source reference", () => {
  const html = renderPermissionTechnicalDetails(definition);
  assert.match(html, /Technical details/);
  assert.match(html, /network\.discovery\.local/);
  assert.match(html, />1</);
  assert.match(html, /src\/code_slayer\/permissions\/definitions\.py/);
  assert.equal(renderPermissionTechnicalDetails(null), "");
});

test("an active permission renders with its grant date and a working revoke control", () => {
  const html = renderActivePermissionGrants([activeGrant]);
  assert.match(html, /Look for compatible devices on your local network/);
  assert.match(html, /Granted now/);
  assert.match(html, /data-permission-revoke="grant-1"/);
  assert.match(permissionGrantBadge("ACTIVE"), /ACTIVE/);
});

test("revoke sends only the grant_id, no permission fields", async () => {
  const calls = [];
  const api = createAPI(async (url, options) => { calls.push([url, options.method, JSON.parse(options.body)]); return reply({grant_id: "grant-1", state: "REVOKED"}); });
  await api.revokePermission("grant-1");
  assert.deepEqual(calls, [["/api/permissions/grants/grant-1/revoke", "POST", {}]]);
});

test("a revoked permission no longer renders as active and instead appears in history", () => {
  const revoked = {...activeGrant, state: "REVOKED", revoked_at: "later"};
  assert.doesNotMatch(renderActivePermissionGrants([revoked]), /data-permission-revoke/);
  assert.match(renderActivePermissionGrants([revoked]), /No active permissions/);
  const history = renderPermissionHistory([], [revoked]);
  assert.match(history, /Look for compatible devices on your local network/);
  assert.match(history, /Revoked later/);
});

test("the WebUI cannot alter permission key, semantic version, resource or authority origin when deciding or revoking", async () => {
  const apiSource = await readFile(new URL("../static/api.js", import.meta.url), "utf8");
  const decideLine = apiSource.match(/decidePermission:[^\n]+/)[0];
  const revokeLine = apiSource.match(/revokePermission:[^\n]+/)[0];
  for (const forbidden of ["permission_key", "semantic_version", "resource", "authority_origin", "scope"]) {
    assert.doesNotMatch(decideLine, new RegExp(forbidden));
    assert.doesNotMatch(revokeLine, new RegExp(forbidden));
  }
  assert.match(decideLine, /decision/);
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  assert.doesNotMatch(appSource, /api\.decidePermission\([^)]*permission_key/);
  assert.doesNotMatch(appSource, /api\.revokePermission\([^)]*permission_key/);
});

test("disconnected state is clearly shown for the Privacy & Security view, never stale data presented as current", async () => {
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  assert.match(appSource, /function loadPrivacy/);
  assert.match(appSource, /Backend disconnected\. Permission state is unavailable until the connection returns\./);
  assert.match(appSource, /getElementById\("privacy"\)\?\.classList\.contains\("active"\)/);
  assert.match(appSource, /loadPrivacy\(\)/);
});

test("no fabricated production permission data: the UI only ever renders what the backend returned", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  // The static markup carries no pre-populated request/grant rows -- only
  // empty containers that `loadPrivacy()` fills from a live API response.
  assert.doesNotMatch(html, /network\.discovery\.local/);
  assert.doesNotMatch(html, /PENDING|ALLOWED|REVOKED/);
  assert.doesNotMatch(appSource, /FakePermission|demo-grant|demo-request/i);
  assert.match(appSource, /api\.permissionRequests\(\)/);
  assert.match(appSource, /api\.permissionGrants\(\)/);
});

test("permission rendering escapes untrusted values everywhere", () => {
  const attack = '<img src=x onerror="alert(1)">';
  const attackDefinition = {...definition, user_title: attack, user_summary: attack, what_it_does: [attack], implementation_reference: attack};
  assert.doesNotMatch(renderPendingPermissionRequest({...pendingRequest, purpose: attack, requesting_subsystem: attack, definition: attackDefinition}), /<img/);
  assert.doesNotMatch(renderActivePermissionGrants([{...activeGrant, resource: attack, definition: attackDefinition}]), /<img/);
  assert.doesNotMatch(renderPermissionHistory([{...pendingRequest, state: "DENIED", definition: attackDefinition}], []), /<img/);
});

function fakeStorage() {
  const mem = {};
  return {
    mem,
    getItem: (key) => (Object.hasOwn(mem, key) ? mem[key] : null),
    setItem: (key, value) => {
      mem[key] = String(value);
    },
    removeItem: (key) => {
      delete mem[key];
    },
  };
}

test("worker aliases are in-memory presentation names and never persist", () => {
  const local = fakeStorage();
  const session = fakeStorage();
  globalThis.localStorage = local;
  globalThis.sessionStorage = session;
  saveWorkerAlias("local-worker", "Studio coder");
  assert.equal(workerAlias("local-worker"), "Studio coder");
  assert.equal(workerAlias("other-worker"), "other-worker");
  assert.deepEqual(Object.keys(local.mem), []);
  assert.deepEqual(Object.keys(session.mem), []);
  saveWorkerAlias("local-worker", "sha256:abcdef");
  saveWorkerAlias("local-worker", "http://127.0.0.1:11434");
  saveWorkerAlias("local-worker", "/var/lib/codeslayer/state.db");
  saveWorkerAlias("local-worker", "digest:abc");
  saveWorkerAlias("local-worker", "evidence_ref-1");
  assert.equal(workerAlias("local-worker"), "Studio coder");
  saveWorkerAlias("local-worker", "127.0.0.1:11434");
  saveWorkerAlias("other-worker", "sk-proj-abcdef");
  saveWorkerAlias("third-worker", "api-key-123");
  assert.deepEqual(Object.keys(local.mem), []);
  assert.deepEqual(Object.keys(session.mem), []);
  saveWorkerAlias("local-worker", "");
  saveWorkerAlias("other-worker", "");
  saveWorkerAlias("third-worker", "");
  assert.equal(workerAlias("local-worker"), "local-worker");
  assert.equal(workerAlias("other-worker"), "other-worker");
  assert.equal(workerAlias("third-worker"), "third-worker");
});

test("shipped modules keep same-origin transport and never use web storage", async () => {
  const files = [
    "../index.html",
    "../static/app.js",
    "../static/api.js",
    "../static/views.js",
    "../static/aliases.js",
    "../static/app.css",
    "../static/views-admin.js",
  ];
  for (const rel of files) {
    const source = await readFile(new URL(rel, import.meta.url), "utf8");
    assert.doesNotMatch(source, /localStorage/);
    assert.doesNotMatch(source, /sessionStorage/);
    assert.doesNotMatch(source, /indexedDB/i);
  }
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  const apiSource = await readFile(new URL("../static/api.js", import.meta.url), "utf8");
  const aliasSource = await readFile(new URL("../static/aliases.js", import.meta.url), "utf8");
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  assert.match(html, /href="\/manifest\.json"/);
  assert.match(html, /href="\/static\/app\.css"/);
  assert.match(html, /src="\/static\/app\.js"/);
  assert.match(html, /id="plan-detail"/);
  assert.doesNotMatch(appSource, /fetch\(/);
  assert.match(appSource, /from "\.\/aliases\.js"/);
  assert.match(apiSource, /credentials: "same-origin"/);
  assert.match(apiSource, /cache: "no-store"/);
  assert.match(apiSource, /fetcher\(`\/api\$\{path\}`/);
  assert.match(apiSource, /Refresh durable run state/);
  assert.match(aliasSource, /new Map\(/);
  assert.doesNotMatch(aliasSource, /fetch\(/);
});

function captured(url, options) {
  return [url, options.method, options.body === undefined ? undefined : JSON.parse(options.body)];
}

test("system API client uses exact GET/POST routes and empty mutation bodies", async () => {
  const calls = [];
  const api = createAPI(async (url, options) => { calls.push(captured(url, options)); return reply({}); });
  await api.system();
  await api.systemRestart();
  await api.systemUpdateCheck();
  await api.systemUpdateApply();
  assert.deepEqual(calls, [
    ["/api/system", "GET", undefined],
    ["/api/system/restart", "POST", {}],
    ["/api/system/update/check", "POST", {}],
    ["/api/system/update/apply", "POST", {}],
  ]);
});

test("runtime overview, attest and Ollama routes use exact paths and bodies", async () => {
  const calls = [];
  const api = createAPI(async (url, options) => { calls.push(captured(url, options)); return reply({}); });
  await api.runtime();
  await api.runtimeAttest();
  await api.addOllamaServer("local/ollama", "http://127.0.0.1:12345");
  await api.testOllamaServer("local/ollama");
  assert.deepEqual(calls, [
    ["/api/runtime", "GET", undefined],
    ["/api/runtime/attest", "POST", {}],
    ["/api/runtime/ollama-servers", "POST", { id: "local/ollama", origin: "http://127.0.0.1:12345" }],
    ["/api/runtime/ollama-servers/local%2Follama/test", "POST", {}],
  ]);
});

test("registerRuntimeWorker sends only the HTTP-route allowlist", async () => {
  const calls = [];
  const api = createAPI(async (url, options) => { calls.push(captured(url, options)); return reply({}); });
  await api.registerRuntimeWorker({
    worker_id: "w1",
    ollama_server_id: "local",
    model_tag: "qwen2.5-coder",
  });
  await api.registerRuntimeWorker({
    worker_id: "w1",
    ollama_server_id: "local",
    model_tag: "qwen2.5-coder",
    kind: "openai_compatible",
    network_class: "local",
    effective_context_tokens: 16384,
    temperature: 0,
    normalizer_id: "norm",
    normalizer_version: 1,
    digest: "abc",
    model_digest: "abc",
    approved_model_digest: "abc",
    fingerprint: "fp",
    runtime_identity_fingerprint: "fp",
    outcome: "pass",
    evidence_ref: "ev",
    hard_disqualifiers: ["x"],
    adapter: "fake",
    certificates_transferred: true,
    authority: "x",
    trust: "y",
    permission: "z",
    endpoint: "http://evil.example",
    repo_path: "/tmp",
    db_path: "x.db",
    output_token_budget: 128,
    tool_choice_enforcement: true,
    planner_policy_version: 2,
  });
  assert.deepEqual(calls, [
    ["/api/runtime/workers", "POST", { worker_id: "w1", ollama_server_id: "local", model_tag: "qwen2.5-coder" }],
    ["/api/runtime/workers", "POST", {
      worker_id: "w1",
      ollama_server_id: "local",
      model_tag: "qwen2.5-coder",
      kind: "openai_compatible",
      network_class: "local",
      effective_context_tokens: 16384,
      temperature: 0,
      normalizer_id: "norm",
      normalizer_version: 1,
    }],
  ]);
});

test("runtime approval routes use encoded worker ids and exact empty bodies", async () => {
  const calls = [];
  const api = createAPI(async (url, options) => { calls.push(captured(url, options)); return reply({}); });
  await api.approveRuntimeWorker("w/1");
  await api.approveNewRuntimeIdentity("w/1");
  assert.deepEqual(calls, [
    ["/api/runtime/workers/w%2F1/approve", "POST", {}],
    ["/api/runtime/workers/w%2F1/approve-new-identity", "POST", {}],
  ]);
});

test("certification GETs stay GET; preflight and start POST exact empty bodies", async () => {
  const calls = [];
  const api = createAPI(async (url, options) => { calls.push(captured(url, options)); return reply({}); });
  await api.certificationWorkers();
  await api.certificationWorker("w/1");
  await api.certificationRun("run/1");
  await api.certificationEvidence("run/1");
  await api.certificationHistory("w/1");
  await api.certificationPreflight("w/1", { outcome: "pass", evidence_ref: "x", digest: "d", fingerprint: "f" });
  await api.startBaselineCertification("w/1", { outcome: "pass", adapter: "fake", hard_disqualifiers: [] });
  await api.promoteBaselineCertification("w/1", { outcome: "pass", evidence_ref: "x", certificate_id: "c" });
  assert.deepEqual(calls, [
    ["/api/certification/workers", "GET", undefined],
    ["/api/certification/workers/w%2F1", "GET", undefined],
    ["/api/certification/runs/run%2F1", "GET", undefined],
    ["/api/certification/runs/run%2F1/evidence", "GET", undefined],
    ["/api/certification/workers/w%2F1/history", "GET", undefined],
    ["/api/certification/workers/w%2F1/baseline/preflight", "POST", {}],
    ["/api/certification/workers/w%2F1/baseline/runs", "POST", {}],
    ["/api/certification/workers/w%2F1/baseline/promote", "POST", {}],
  ]);
});

test("tailscale GET/enable/disable are exact and Funnel is not a client operation", async () => {
  const calls = [];
  const api = createAPI(async (url, options) => { calls.push(captured(url, options)); return reply({}); });
  await api.tailscale();
  await api.tailscaleEnable();
  await api.tailscaleDisable();
  assert.deepEqual(calls, [
    ["/api/tailscale", "GET", undefined],
    ["/api/tailscale/enable", "POST", {}],
    ["/api/tailscale/disable", "POST", {}],
  ]);
  for (const name of Object.keys(api)) assert.doesNotMatch(name, /funnel/i);
  assert.equal(api.tailscaleFunnel, undefined);
  assert.equal(api.tailscaleEnable.length, 0);
  assert.equal(api.tailscaleDisable.length, 0);
});

test("administration mutations are not automatically retried", async () => {
  let calls = 0;
  const api = createAPI(async () => { calls++; throw new Error("lost response"); });
  await assert.rejects(api.systemRestart(), (e) => e instanceof APIError && /Refresh durable/.test(e.message));
  assert.equal(calls, 1);
  await assert.rejects(api.tailscaleEnable(), (e) => e instanceof APIError && /Refresh durable/.test(e.message));
  assert.equal(calls, 2);
});

test("administration transport keeps same-origin /api, credentials, cache and timeouts", async () => {
  const delays = [];
  const originalSet = globalThis.setTimeout;
  const originalClear = globalThis.clearTimeout;
  globalThis.setTimeout = (_fn, ms) => {
    delays.push(ms);
    return 0;
  };
  globalThis.clearTimeout = () => {};
  try {
    const calls = [];
    const api = createAPI(async (url, options) => { calls.push([url, options]); return reply({}); });
    await api.system();
    await api.tailscaleEnable();
    assert.deepEqual(delays, [10000, 120000]);
    assert.equal(calls[0][0], "/api/system");
    assert.equal(calls[0][1].method, "GET");
    assert.equal(calls[0][1].credentials, "same-origin");
    assert.equal(calls[0][1].cache, "no-store");
    assert.equal(calls[0][1].headers.Accept, "application/json");
    assert.equal(calls[0][1].body, undefined);
    assert.equal(calls[1][0], "/api/tailscale/enable");
    assert.equal(calls[1][1].method, "POST");
    assert.equal(calls[1][1].credentials, "same-origin");
    assert.equal(calls[1][1].cache, "no-store");
    assert.equal(calls[1][1].body, "{}");
  } finally {
    globalThis.setTimeout = originalSet;
    globalThis.clearTimeout = originalClear;
  }
});

test("api.js never constructs client-authority request fields", async () => {
  const apiSource = await readFile(new URL("../static/api.js", import.meta.url), "utf8");
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  for (const field of ["approved_model_digest", "runtime_identity_fingerprint", "hard_disqualifiers", "evidence_ref"]) {
    assert.doesNotMatch(apiSource, new RegExp(field));
  }
  assert.doesNotMatch(apiSource, /funnel/i);
  assert.doesNotMatch(appSource, /fetch\(/);
  assert.match(apiSource, /credentials: "same-origin"/);
  assert.match(apiSource, /cache: "no-store"/);
  assert.match(apiSource, /data === undefined \? 10000 : 120000/);
  assert.match(apiSource, /Refresh durable run state before trying again/);
});

const runtimeConfig = {
  ollama_servers: [
    { id: "local", origin: "http://127.0.0.1:9", origin_source: "CONFIG_BOUND" },
  ],
  workers: [
    {
      worker_id: "w1",
      kind: "openai_compatible",
      network_class: "local",
      ollama_server_id: "local",
      model_tag: { value: "qwen", source: "CONFIG_BOUND" },
      approved_model_digest: { value: "sha256:abc", source: "CONFIG_BOUND" },
      approved_runtime_version: { value: "0.11.0", source: "CONFIG_BOUND" },
      effective_context_tokens: { value: 16384, source: "CONFIG_BOUND", measured_by_ollama: false },
      temperature: { value: 0, source: "CONFIG_BOUND" },
      normalizer_id: { value: null, source: "CONFIG_BOUND" },
      normalizer_version: { value: null, source: "CONFIG_BOUND" },
      identity_approved: true,
      attestation: null,
    },
  ],
};

test("Models view keeps the registry and a separate Runtime configuration area", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  assert.match(html, /id="workers-list"/);
  assert.match(html, /id="worker-detail"/);
  assert.match(html, /MODEL REGISTRY/);
  assert.match(html, /Runtime configuration/);
  assert.match(html, /Ollama servers/);
  assert.match(html, /Runtime workers/);
  assert.match(html, /id="runtime-attest"/);
  assert.match(html, /Live attest runtime/);
  assert.match(html, /Configured runtime state is not a live probe/);
  assert.match(html, /GET \/api\/runtime is CONFIG_BOUND \/ UNVERIFIED/);
  assert.match(html, /id="ollama-server-form"/);
  assert.match(html, /id="runtime-worker-form"/);
  assert.match(html, /id="runtime-worker-advanced"/);
  assert.doesNotMatch(html, /<details[^>]*\sopen/);
  assert.doesNotMatch(html, /192\.168\.32\.8/);
  assert.doesNotMatch(html, /name="digest"|name="approved_model_digest"|name="fingerprint"|name="outcome"|name="evidence_ref"|name="adapter"/);
});

test("GET runtime load never auto-attests; live attest is an explicit control", async () => {
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  const loadStart = appSource.indexOf("async function loadRuntime");
  const loadEnd = appSource.indexOf("async function runtimeAction");
  const loadFn = appSource.slice(loadStart, loadEnd);
  assert.match(loadFn, /api\.runtime\(\)/);
  assert.doesNotMatch(loadFn, /runtimeAttest/);
  const refreshStart = appSource.indexOf("async function refresh");
  const refreshEnd = appSource.indexOf("async function action");
  assert.doesNotMatch(appSource.slice(refreshStart, refreshEnd), /runtimeAttest/);
  assert.equal((appSource.match(/api\.runtimeAttest\(/g) || []).length, 1);
  assert.match(appSource, /\$\("runtime-attest"\)\.addEventListener\("click"/);
  assert.match(appSource, /if \(state\.runtimeBusy\) return/);
});

test("config-bound servers and live tests keep distinct provenance", () => {
  assert.equal(provenanceClass("CONFIG_BOUND"), "provenance-config");
  assert.equal(provenanceClass("LIVE_ATTESTED"), "provenance-live");
  assert.equal(provenanceClass("VERIFIED"), "provenance-verified");
  assert.equal(provenanceClass("MISMATCH"), "provenance-mismatch");
  assert.equal(provenanceClass("UNREACHABLE"), "provenance-unreachable");
  assert.equal(provenanceClass("READY"), "provenance-unknown");
  assert.notEqual(provenanceClass("VERIFIED"), provenanceClass("READY"));
  assert.notEqual(provenanceClass("MISMATCH"), provenanceClass("VERIFIED"));
  assert.notEqual(provenanceClass("UNREACHABLE"), provenanceClass("VERIFIED"));
  assert.notEqual(provenanceClass("LIVE_ATTESTED"), provenanceClass("CONFIG_BOUND"));
  const html = renderRuntimeServers(runtimeConfig);
  assert.match(html, /CONFIG_BOUND/);
  assert.match(html, /provenance-config/);
  assert.doesNotMatch(html, /LIVE_ATTESTED|provenance-live|provenance-verified/);
  assert.match(html, /data-runtime-test-server="local"/);
  const live = renderRuntimeServers(runtimeConfig, {
    attestation: {
      ollama_servers: [{
        id: "local",
        origin: "http://127.0.0.1:9",
        origin_source: "CONFIG_BOUND",
        live: { status: "UNREACHABLE", reason: "runtime_probe_unavailable" },
      }],
    },
    tests: {
      local: {
        id: "local",
        origin: "http://127.0.0.1:9",
        status: "LIVE_ATTESTED",
        runtime_version: "0.11.0",
        models: [{ name: "qwen", digest: "sha256:abc" }],
      },
    },
  });
  assert.match(live, /provenance-config/);
  assert.match(live, /provenance-live/);
  assert.match(live, /LIVE ATTESTATION/);
  assert.match(live, /SERVER TEST \(OBSERVATION\)/);
  assert.match(live, /UNREACHABLE/);
  assert.match(live, /provenance-unreachable/);
  assert.doesNotMatch(live, /class="badge provenance provenance-verified">UNREACHABLE/);
  assert.doesNotMatch(live, /class="badge provenance provenance-verified">MISMATCH/);
  const mismatch = renderRuntimeAttestation({
    workers: [{ worker_id: "w1", attestation: { status: "MISMATCH", reason: "runtime_identity_mismatch" } }],
  });
  assert.match(mismatch, /MISMATCH/);
  assert.match(mismatch, /provenance-mismatch/);
  assert.doesNotMatch(mismatch, /provenance-verified">MISMATCH/);
});

test("runtime server and worker renderers escape untrusted values", () => {
  const attack = '<img src=x onerror="alert(1)">';
  assert.doesNotMatch(renderRuntimeServers({ ollama_servers: [{ id: attack, origin: attack, origin_source: attack }] }), /<img/);
  assert.doesNotMatch(renderOllamaServerTest({ status: attack, origin: attack, runtime_version: attack, models: [{ name: attack, digest: attack }] }, attack), /<img/);
  assert.doesNotMatch(renderRuntimeWorkers({ workers: [{
    worker_id: attack, kind: attack, network_class: attack, ollama_server_id: attack,
    model_tag: { value: attack, source: attack },
    approved_model_digest: { value: attack, source: attack },
    approved_runtime_version: { value: attack, source: attack },
    effective_context_tokens: { value: attack, source: attack, measured_by_ollama: attack },
    temperature: { value: attack, source: attack },
    normalizer_id: { value: attack, source: attack },
    normalizer_version: { value: attack, source: attack },
    identity_approved: attack,
  }] }), /<img/);
  assert.doesNotMatch(renderRuntimeIdentityResult({ status: attack, reason: attack, configured_digest: attack, observed_digest: attack }), /<img/);
  assert.doesNotMatch(renderReplaceIdentityConfirm(attack), /<img/);
  assert.doesNotMatch(provenanceBadge(attack), /<img/);
});

test("runtime workers display digest and version and do not post them", () => {
  const html = renderRuntimeWorkers(runtimeConfig);
  assert.match(html, /approved_model_digest/);
  assert.match(html, /sha256:abc/);
  assert.match(html, /0\.11\.0/);
  assert.match(html, /measured_by_ollama: false/);
  assert.match(html, /identity_approved: true/);
  assert.doesNotMatch(html, /<input[^>]+name="digest"/);
  assert.doesNotMatch(html, /<input[^>]+name="approved_model_digest"/);
  assert.doesNotMatch(html, /<input[^>]+name="fingerprint"/);
  assert.match(html, /data-runtime-approve="w1"/);
  assert.match(html, /data-runtime-replace-ask="w1"/);
  assert.doesNotMatch(html, /data-runtime-replace-confirm/);
});

test("registerRuntimeWorker payload copies only supplied allowlisted fields", () => {
  assert.deepEqual(runtimeRegistrationPayload({
    worker_id: "w1",
    ollama_server_id: "local",
    model_tag: "qwen",
    kind: "",
    digest: "abc",
    approved_model_digest: "abc",
    fingerprint: "fp",
    outcome: "pass",
  }), { worker_id: "w1", ollama_server_id: "local", model_tag: "qwen" });
  assert.deepEqual(runtimeRegistrationPayload({
    worker_id: "w1",
    ollama_server_id: "local",
    model_tag: "qwen",
    kind: "openai_compatible",
    network_class: "local",
    effective_context_tokens: "16384",
    temperature: "0",
    normalizer_id: "norm",
    normalizer_version: "1",
  }), {
    worker_id: "w1",
    ollama_server_id: "local",
    model_tag: "qwen",
    kind: "openai_compatible",
    network_class: "local",
    effective_context_tokens: 16384,
    temperature: 0,
    normalizer_id: "norm",
    normalizer_version: 1,
  });
  const options = renderRuntimeServerOptions(runtimeConfig.ollama_servers);
  assert.match(options, /value="local"/);
});

test("identity approval is explicit and MISMATCH does not replace", async () => {
  const mismatch = renderRuntimeIdentityResult({
    status: "MISMATCH",
    reason: "runtime_identity_mismatch",
    configured_digest: "sha256:old",
    observed_digest: "sha256:new",
    replaced: false,
  });
  assert.match(mismatch, /MISMATCH/);
  assert.match(mismatch, /Approved identity was not replaced/);
  assert.doesNotMatch(mismatch, /data-runtime-replace-confirm/);
  const confirm = renderReplaceIdentityConfirm("w1");
  assert.match(confirm, /replaces the currently approved runtime identity/);
  assert.match(confirm, /Certificates are NOT transferred automatically/);
  assert.match(confirm, /Production eligibility must not be assumed/);
  assert.match(confirm, /data-runtime-replace-confirm="w1"/);
  const withConfirm = renderRuntimeWorkers(runtimeConfig, { replacePending: "w1" });
  assert.match(withConfirm, /data-runtime-replace-confirm="w1"/);
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  assert.match(appSource, /api\.approveRuntimeWorker\(workerId\)/);
  assert.match(appSource, /api\.approveNewRuntimeIdentity\(workerId\)/);
  assert.match(appSource, /const snapshot = await api\.runtime\(\)/);
  assert.doesNotMatch(appSource, /result\.status === "MISMATCH" \? state\.runtime/);
  assert.match(appSource, /acceptIdentityResult\(state, workerId, result, snapshot\)/);
  assert.match(appSource, /api\.addOllamaServer\(serverId, origin\)/);
  assert.equal((appSource.match(/api\.addOllamaServer\(/g) || []).length, 1);
  const approveBlock = appSource.slice(appSource.indexOf("if (approveButton)"), appSource.indexOf("if (replaceAsk)"));
  assert.doesNotMatch(approveBlock, /approveNewRuntimeIdentity/);
});

test("Models runtime UI keeps fetch out of app and admin views", async () => {
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  const adminSource = await readFile(new URL("../static/views-admin.js", import.meta.url), "utf8");
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  assert.doesNotMatch(appSource, /fetch\(/);
  assert.doesNotMatch(adminSource, /fetch\(/);
  assert.doesNotMatch(adminSource, /localStorage|sessionStorage|indexedDB/i);
  assert.match(appSource, /from "\.\/views-admin\.js"/);
  assert.match(html, /name="kind"/);
  assert.match(html, /name="network_class"/);
  assert.match(html, /name="effective_context_tokens"/);
  assert.match(html, /name="temperature"/);
  assert.match(html, /name="normalizer_id"/);
  assert.match(html, /name="normalizer_version"/);
  assert.doesNotMatch(html, /name="output_token_budget"|name="tool_choice_enforcement"|name="planner_policy_version"/);
  const modelsStart = html.indexOf('id="models"');
  const modelsEnd = html.indexOf('id="intelligence"', modelsStart);
  assert.ok(modelsStart >= 0 && modelsEnd > modelsStart);
  const modelsHtml = html.slice(modelsStart, modelsEnd);
  assert.doesNotMatch(modelsHtml, /Funnel/i);
  assert.doesNotMatch(adminSource, /statusClass/);
});

function evidenceState(extra = {}) {
  return {
    runtime: runtimeConfig,
    runtimeUnavailable: false,
    runtimeAttestation: {
      ollama_servers: [{
        id: "local",
        origin: "http://127.0.0.1:9",
        live: { status: "LIVE_ATTESTED", runtime_version: "old" },
      }],
      workers: [{
        worker_id: "w1",
        ollama_server_id: "local",
        model_tag: { value: "qwen" },
        attestation: { status: "VERIFIED", reason: "approved_identity_live_attested" },
      }],
    },
    runtimeServerTests: {
      local: { id: "local", origin: "http://127.0.0.1:9", status: "LIVE_ATTESTED", runtime_version: "old" },
    },
    runtimeIdentityResults: { w1: { status: "MISMATCH", reason: "runtime_identity_mismatch" } },
    runtimeReplacePending: "w1",
    ...extra,
  };
}

test("a new runtime snapshot invalidates prior transient evidence", () => {
  const runtimeState = evidenceState();
  const next = {
    ollama_servers: [{ id: "local", origin: "http://127.0.0.1:1", origin_source: "CONFIG_BOUND" }],
    workers: [],
  };
  acceptRuntimeSnapshot(runtimeState, next);
  assert.equal(runtimeState.runtime, next);
  assert.equal(runtimeState.runtimeAttestation, null);
  assert.deepEqual(runtimeState.runtimeServerTests, {});
  assert.deepEqual(runtimeState.runtimeIdentityResults, {});
  assert.equal(runtimeState.runtimeReplacePending, null);
  assert.equal(runtimeState.runtimeUnavailable, false);
  const html = renderRuntimeServers(runtimeState.runtime, {
    tests: runtimeState.runtimeServerTests,
    attestation: runtimeState.runtimeAttestation,
  });
  assert.match(html, /http:\/\/127\.0\.0\.1:1/);
  assert.doesNotMatch(html, /LIVE_ATTESTED|VERIFIED|MISMATCH|UNBOUND/);
  assert.doesNotMatch(renderRuntimeWorkers(runtimeState.runtime, {
    identityResults: runtimeState.runtimeIdentityResults,
    replacePending: runtimeState.runtimeReplacePending,
    attestation: runtimeState.runtimeAttestation,
  }), /data-runtime-replace-confirm/);
});

test("live attest installs config and live state from the same response", () => {
  const runtimeState = evidenceState();
  const probed = {
    ollama_servers: [{
      id: "local",
      origin: "http://127.0.0.1:9",
      origin_source: "CONFIG_BOUND",
      live: { status: "LIVE_ATTESTED", runtime_version: "0.11.0" },
    }],
    workers: [{
      ...runtimeConfig.workers[0],
      attestation: {
        status: "VERIFIED",
        reason: "approved_identity_live_attested",
        configured_digest: "sha256:abc",
        observed_digest: "sha256:abc",
        configured_version: "0.11.0",
        observed_version: "0.11.0",
        fingerprint_source: "LIVE_ATTESTED",
      },
    }],
  };
  acceptRuntimeAttest(runtimeState, probed);
  assert.equal(runtimeState.runtime, probed);
  assert.equal(runtimeState.runtimeAttestation, probed);
  assert.deepEqual(runtimeState.runtimeServerTests, {});
  assert.deepEqual(runtimeState.runtimeIdentityResults, {});
  const html = renderRuntimeServers(runtimeState.runtime, { attestation: runtimeState.runtimeAttestation });
  assert.match(html, /LIVE_ATTESTED/);
  assert.match(html, /http:\/\/127\.0\.0\.1:9/);
  const workers = renderRuntimeWorkers(runtimeState.runtime, { attestation: runtimeState.runtimeAttestation });
  assert.match(workers, /provenance-verified/);
  assert.equal(attestationForWorker(runtimeState.runtime.workers[0], runtimeState.runtimeAttestation).status, "VERIFIED");
});

test("replacing server id local cannot retain the old server test", () => {
  const runtimeState = evidenceState();
  const replaced = {
    ollama_servers: [{ id: "local", origin: "http://127.0.0.1:1", origin_source: "CONFIG_BOUND" }],
    workers: runtimeConfig.workers,
  };
  acceptRuntimeSnapshot(runtimeState, replaced);
  const html = renderRuntimeServers(replaced, {
    tests: runtimeState.runtimeServerTests,
    attestation: runtimeState.runtimeAttestation,
  });
  assert.doesNotMatch(html, /LIVE_ATTESTED/);
  assert.doesNotMatch(html, /SERVER TEST \(OBSERVATION\)/);
  assert.doesNotMatch(html, /http:\/\/127\.0\.0\.1:9/);
  const leftover = renderRuntimeServers(replaced, {
    tests: { local: { id: "local", origin: "http://127.0.0.1:9", status: "LIVE_ATTESTED", runtime_version: "old" } },
  });
  assert.match(leftover, /SERVER TEST \(UNBOUND\)/);
  assert.match(leftover, /not current evidence for the configured origin/);
  assert.doesNotMatch(leftover, /provenance-live/);
});

test("re-registering worker id w1 cannot retain old identity result", () => {
  const runtimeState = evidenceState();
  const reregistered = {
    ollama_servers: runtimeConfig.ollama_servers,
    workers: [{
      ...runtimeConfig.workers[0],
      model_tag: { value: "other", source: "CONFIG_BOUND" },
    }],
  };
  acceptRuntimeSnapshot(runtimeState, reregistered);
  assert.deepEqual(runtimeState.runtimeIdentityResults, {});
  assert.equal(runtimeState.runtimeReplacePending, null);
  const html = renderRuntimeWorkers(reregistered, {
    identityResults: runtimeState.runtimeIdentityResults,
    replacePending: runtimeState.runtimeReplacePending,
    attestation: runtimeState.runtimeAttestation,
  });
  assert.doesNotMatch(html, /runtime_identity_mismatch/);
  assert.doesNotMatch(html, /data-runtime-replace-confirm/);
  assert.doesNotMatch(html, /LIVE ATTESTATION/);
  assert.equal(attestationForWorker(reregistered.workers[0], evidenceState().runtimeAttestation), null);
});

test("replacePending is cleared when runtime config changes", () => {
  const runtimeState = evidenceState({ runtimeReplacePending: "w1" });
  clearRuntimeEvidence(runtimeState);
  assert.equal(runtimeState.runtimeReplacePending, null);
  acceptRuntimeSnapshot(runtimeState, runtimeConfig);
  assert.equal(runtimeState.runtimeReplacePending, null);
});

test("failed runtime reload cannot leave old live evidence as current", () => {
  const runtimeState = evidenceState();
  rejectRuntimeSnapshot(runtimeState);
  assert.equal(runtimeState.runtime, null);
  assert.equal(runtimeState.runtimeUnavailable, true);
  assert.equal(runtimeState.runtimeAttestation, null);
  assert.deepEqual(runtimeState.runtimeServerTests, {});
  assert.deepEqual(runtimeState.runtimeIdentityResults, {});
  assert.equal(runtimeState.runtimeReplacePending, null);
  assert.doesNotMatch(renderRuntimeServers(runtimeState.runtime, {
    tests: runtimeState.runtimeServerTests,
    attestation: runtimeState.runtimeAttestation,
  }), /LIVE_ATTESTED|VERIFIED|MISMATCH/);
});

test("server-test rendering shows its own returned origin and will not bind a different origin", () => {
  const bound = renderOllamaServerTest({
    id: "local",
    origin: "http://127.0.0.1:9",
    status: "LIVE_ATTESTED",
    runtime_version: "0.11.0",
  }, "http://127.0.0.1:9");
  assert.match(bound, /observed origin http:\/\/127\.0\.0\.1:9/);
  assert.match(bound, /LIVE_ATTESTED/);
  assert.match(bound, /SERVER TEST \(OBSERVATION\)/);
  const unbound = renderOllamaServerTest({
    id: "local",
    origin: "http://127.0.0.1:9",
    status: "LIVE_ATTESTED",
    runtime_version: "0.11.0",
  }, "http://127.0.0.1:1");
  assert.match(unbound, /observed origin http:\/\/127\.0\.0\.1:9/);
  assert.match(unbound, /configured origin http:\/\/127\.0\.0\.1:1/);
  assert.match(unbound, /SERVER TEST \(UNBOUND\)/);
  assert.doesNotMatch(unbound, /provenance-live/);
  assert.doesNotMatch(unbound, /SERVER TEST \(OBSERVATION\)/);
  const html = renderRuntimeServers({
    ollama_servers: [{ id: "local", origin: "http://127.0.0.1:1", origin_source: "CONFIG_BOUND" }],
  }, {
    tests: { local: { id: "local", origin: "http://127.0.0.1:9", status: "LIVE_ATTESTED" } },
  });
  assert.match(html, /http:\/\/127\.0\.0\.1:1/);
  assert.doesNotMatch(html, /class="badge provenance provenance-live"/);
});

test("identity result after approval is not mixed with prior attestation", () => {
  const runtimeState = evidenceState();
  const result = { status: "VERIFIED", reason: "approved_from_live_attestation", certificates_transferred: false };
  acceptIdentityResult(runtimeState, "w1", result, runtimeConfig);
  assert.equal(runtimeState.runtimeAttestation, null);
  assert.deepEqual(runtimeState.runtimeServerTests, {});
  assert.equal(runtimeState.runtimeReplacePending, null);
  assert.equal(runtimeState.runtimeIdentityResults.w1, result);
  const later = { ollama_servers: runtimeConfig.ollama_servers, workers: runtimeConfig.workers };
  acceptRuntimeSnapshot(runtimeState, later);
  assert.deepEqual(runtimeState.runtimeIdentityResults, {});
});

test("app.js fail-closes runtime evidence on GET, attest, add, register and approve", async () => {
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  const loadStart = appSource.indexOf("async function loadRuntime");
  const loadEnd = appSource.indexOf("async function runtimeAction");
  const loadFn = appSource.slice(loadStart, loadEnd);
  assert.match(loadFn, /acceptRuntimeSnapshot\(state, snapshot\)/);
  assert.match(loadFn, /rejectRuntimeSnapshot\(state\)/);
  assert.doesNotMatch(loadFn, /runtimeAttest/);
  assert.match(appSource, /acceptRuntimeAttest\(state, result\)/);
  assert.match(appSource, /const snapshot = await api\.addOllamaServer/);
  assert.match(appSource, /const snapshot = await api\.registerRuntimeWorker/);
  assert.equal((appSource.match(/acceptRuntimeSnapshot\(state, snapshot\)/g) || []).length, 3);
  assert.match(appSource, /acceptIdentityResult\(state, workerId, result, snapshot\)/);
});

test("failed server test cannot leave a prior LIVE_ATTESTED result visible", () => {
  const runtimeState = evidenceState();
  acceptServerTest(runtimeState, {
    id: "other",
    origin: "http://127.0.0.1:8",
    status: "LIVE_ATTESTED",
    runtime_version: "keep",
  });
  assert.equal(runtimeState.runtimeServerTests.local.status, "LIVE_ATTESTED");
  invalidateServerTest(runtimeState, "local");
  assert.equal(runtimeState.runtimeServerTests.local, undefined);
  assert.equal(runtimeState.runtimeServerTests.other.status, "LIVE_ATTESTED");
  const html = renderRuntimeServers(runtimeState.runtime, { tests: runtimeState.runtimeServerTests });
  assert.doesNotMatch(html, /SERVER TEST \(OBSERVATION\)/);
  assert.doesNotMatch(html, /provenance-live/);
});

test("failed live attest leaves no previous live attestation visible", () => {
  const runtimeState = evidenceState();
  const probed = {
    ollama_servers: runtimeConfig.ollama_servers,
    workers: runtimeConfig.workers,
  };
  acceptRuntimeAttest(runtimeState, {
    ...probed,
    ollama_servers: [{
      id: "local",
      origin: "http://127.0.0.1:9",
      origin_source: "CONFIG_BOUND",
      live: { status: "LIVE_ATTESTED", runtime_version: "0.11.0" },
    }],
  });
  const snapshot = runtimeState.runtime;
  beginRuntimeObservation(runtimeState);
  assert.equal(runtimeState.runtime, snapshot);
  assert.equal(runtimeState.runtimeAttestation, null);
  assert.deepEqual(runtimeState.runtimeServerTests, {});
  const html = renderRuntimeServers(runtimeState.runtime, { attestation: runtimeState.runtimeAttestation });
  assert.doesNotMatch(html, /LIVE ATTESTATION/);
  assert.doesNotMatch(html, /provenance-live/);
  assert.match(html, /CONFIG_BOUND/);
});

test("failed add/register/approve mutations invalidate the runtime snapshot", async () => {
  const runtimeState = evidenceState();
  rejectRuntimeSnapshot(runtimeState);
  assert.equal(runtimeState.runtime, null);
  assert.equal(runtimeState.runtimeUnavailable, true);
  assert.equal(runtimeState.runtimeAttestation, null);
  assert.deepEqual(runtimeState.runtimeServerTests, {});
  assert.deepEqual(runtimeState.runtimeIdentityResults, {});
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  const mutateStart = appSource.indexOf("async function mutateRuntime");
  const mutateEnd = appSource.indexOf("async function observeRuntimeAttest");
  const mutateFn = appSource.slice(mutateStart, mutateEnd);
  assert.match(mutateFn, /rejectRuntimeSnapshot\(state\)/);
  assert.equal((mutateFn.match(/work\(\)/g) || []).length, 1);
  assert.equal((appSource.match(/mutateRuntime\(/g) || []).length, 5);
  assert.match(appSource, /mutateRuntime\(async \(\) => \{\n    const snapshot = await api\.addOllamaServer/);
  assert.match(appSource, /mutateRuntime\(async \(\) => \{\n    const snapshot = await api\.registerRuntimeWorker/);
  assert.match(appSource, /mutateRuntime\(async \(\) => \{\n      const result = await api\.approveRuntimeWorker/);
  assert.match(appSource, /mutateRuntime\(async \(\) => \{\n      const result = await api\.approveNewRuntimeIdentity/);
  const attestFn = appSource.slice(
    appSource.indexOf("async function observeRuntimeAttest"),
    appSource.indexOf("async function observeServerTest"),
  );
  assert.match(attestFn, /beginRuntimeObservation\(state\)/);
  assert.match(attestFn, /clearRuntimeEvidence\(state\)/);
  assert.doesNotMatch(attestFn, /rejectRuntimeSnapshot/);
  const testFn = appSource.slice(appSource.indexOf("async function observeServerTest"));
  assert.match(testFn, /invalidateServerTest\(state, serverId\)/);
  assert.match(testFn, /acceptServerTest\(state, result\)/);
});

test("approveRuntimeWorker MISMATCH refreshes GET /runtime and will not bind old config", () => {
  const oldSnapshot = runtimeConfig;
  const fresh = {
    ollama_servers: runtimeConfig.ollama_servers,
    workers: [{
      ...runtimeConfig.workers[0],
      approved_model_digest: { value: "sha256:current", source: "CONFIG_BOUND" },
    }],
  };
  const mismatch = {
    status: "MISMATCH",
    reason: "runtime_identity_mismatch",
    configured_digest: "sha256:current",
    observed_digest: "sha256:new",
    replaced: false,
  };
  assert.equal(identityResultBindable(oldSnapshot, "w1", mismatch), false);
  const runtimeState = evidenceState({ runtime: oldSnapshot });
  acceptIdentityResult(runtimeState, "w1", mismatch, oldSnapshot);
  assert.equal(runtimeState.runtimeIdentityResults.w1, undefined);
  acceptIdentityResult(runtimeState, "w1", mismatch, fresh);
  assert.equal(runtimeState.runtimeIdentityResults.w1, mismatch);
  assert.equal(runtimeState.runtime, fresh);
  assert.doesNotMatch(JSON.stringify(runtimeState.runtime.workers[0].approved_model_digest), /sha256:abc/);
});

test("runtime mutations are not retried after a lost response", async () => {
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  const actionFn = appSource.slice(
    appSource.indexOf("async function runtimeAction"),
    appSource.indexOf("async function mutateRuntime"),
  );
  assert.match(actionFn, /notice\(error\.message, true\)/);
  assert.doesNotMatch(actionFn, /await work\(\);\s*await work\(\)/);
  assert.doesNotMatch(actionFn, /for \(.*work\(\)/);
  const apiSource = await readFile(new URL("../static/api.js", import.meta.url), "utf8");
  assert.match(apiSource, /Refresh durable run state before trying again/);
});

const certWorker = {
  worker_id: "cw1",
  kind: "openai_compatible",
  network_class: "local",
  environment: "VALIDATION",
  runtime: { status: "UNKNOWN", reason: "not_probed" },
  baseline_security: {
    status: "CERTIFIED",
    outcome: "PASS",
    environment: "VALIDATION",
    certificate_id: "cert-val",
  },
  roles: {
    planner: { status: "NOT_CERTIFIED", certificate_id: null, outcome: null },
  },
  production_eligibility: {
    eligible: false,
    reason: "missing_role_certificate",
    source: "evaluate_production_eligibility",
    security_certificate_id: null,
    role_certificate_id: null,
  },
  ready_for_certification: false,
  promotion_available: true,
  promotion_reason: "promotable",
  planner_ready_for_certification: false,
  future_actions: [
    { role: "planner", available: false, reason: "live_role_certification_unavailable" },
  ],
  identity: {
    model_tag: { value: "qwen", source: "CONFIG_BOUND" },
    model_digest: { value: "sha256:abc", source: "CONFIG_BOUND" },
    runtime_identity_fingerprint: { value: "fp", source: "CONFIG_BOUND" },
    endpoint: { value: "http://127.0.0.1:9", source: "CONFIG_BOUND" },
    ollama_root: { value: "/models", source: "CONFIG_BOUND" },
    runtime_version: { value: "0.11.0", source: "CONFIG_BOUND" },
    normalizer_id: { value: null, source: "CONFIG_BOUND" },
    normalizer_version: { value: null, source: "CONFIG_BOUND" },
    effective_context_tokens: { value: 16384, source: "CONFIG_BOUND", measured_by_ollama: false },
    temperature: { value: 0, source: "CONFIG_BOUND" },
  },
  last_preflight: {
    run_id: "pre-1",
    state: "INCOMPLETE",
    ready: false,
    checks: [{ name: "digest_matches", ok: false, detail: "runtime_model_digest_mismatch" }],
  },
  history: {
    runs: [{ run_id: "run-inc", state: "INCOMPLETE", has_certificate: false }],
    validation_certificates: [{ certificate_id: "cert-val", outcome: "PASS", environment: "VALIDATION", issued_at: "now" }],
    production_certificates: [],
  },
};

test("Privacy permission UI remains and Certification Center is a separate section", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  assert.match(html, /id="privacy-pending"/);
  assert.match(html, /id="privacy-active"/);
  assert.match(html, /id="privacy-history"/);
  assert.match(html, /id="privacy-badge"/);
  assert.match(html, /id="cert-stack"/);
  assert.match(html, /id="cert-badge"/);
  assert.match(html, /CERTIFICATION CENTER/);
  assert.match(html, /isolated VALIDATION state/);
  assert.match(html, /They do not grant trust, permissions, or production eligibility/);
  assert.ok(html.indexOf("id=\"privacy-history\"") < html.indexOf("id=\"cert-stack\""));
});

test("certification state namespace is distinct from run status and runtime provenance", () => {
  assert.equal(certificationStateClass("READY"), "cert-ready");
  assert.equal(certificationStateClass("INCOMPLETE"), "cert-incomplete");
  assert.equal(certificationStateClass("QUEUED"), "cert-queued");
  assert.equal(certificationStateClass("RUNNING"), "cert-running");
  assert.equal(certificationStateClass("PASS"), "cert-pass");
  assert.equal(certificationStateClass("FAIL"), "cert-fail");
  assert.equal(certificationStateClass("HARD_DISQUALIFIED"), "cert-hard");
  assert.equal(certificationStateClass("CERTIFIED"), "cert-certified");
  assert.equal(certificationStateClass("FAILED"), "cert-failed");
  assert.equal(certificationStateClass("NOT_CERTIFIED"), "cert-not-certified");
  assert.equal(certificationStateClass("VERIFIED"), "cert-runtime-verified");
  assert.equal(certificationStateClass("MISMATCH"), "cert-runtime-mismatch");
  assert.equal(certificationStateClass("UNREACHABLE"), "cert-runtime-unreachable");
  assert.equal(certificationStateClass("UNKNOWN"), "cert-runtime-unknown");
  assert.notEqual(certificationStateClass("VERIFIED"), provenanceClass("VERIFIED"));
  assert.notEqual(certificationStateClass("MISMATCH"), provenanceClass("MISMATCH"));
  assert.notEqual(certificationStateClass("UNKNOWN"), certificationStateClass("VERIFIED"));
  assert.notEqual(certificationStateClass("UNREACHABLE"), certificationStateClass("VERIFIED"));
  assert.notEqual(certificationStateClass("MISMATCH"), certificationStateClass("VERIFIED"));
  assert.notEqual(certificationStateClass("PASS"), certificationStateClass("FAIL"));
  assert.notEqual(certificationStateClass("INCOMPLETE"), certificationStateClass("PASS"));
  assert.notEqual(certificationStateClass("HARD_DISQUALIFIED"), certificationStateClass("PASS"));
  assert.notEqual(certificationStateClass("CERTIFIED"), certificationStateClass("ELIGIBLE"));
  assert.match(certificationStateBadge("UNKNOWN"), /cert-runtime-unknown/);
  assert.doesNotMatch(certificationStateBadge("UNKNOWN"), /provenance-verified|cert-runtime-verified/);
  assert.doesNotMatch(certificationStateBadge("MISMATCH"), /cert-runtime-verified|provenance-verified/);
});

test("certification summaries keep VALIDATION, runtime unknowns, and Planner eligibility distinct", () => {
  const html = renderCertificationWorkers({ environment: "VALIDATION", workers: [certWorker] }, "cw1");
  assert.match(html, /VALIDATION/);
  assert.match(html, /cert-validation/);
  assert.match(html, /UNKNOWN/);
  assert.match(html, /cert-runtime-unknown/);
  assert.doesNotMatch(html, /cert-runtime-verified">UNKNOWN/);
  assert.match(html, /Production eligibility — Planner/);
  assert.match(html, /BLOCKED/);
  assert.match(html, /missing_role_certificate/);
  assert.doesNotMatch(html, />ELIGIBLE</);
  assert.match(html, /CERTIFIED/);
  const mismatch = renderCertificationWorkerSummary({
    ...certWorker,
    runtime: { status: "MISMATCH", reason: "runtime_model_digest_mismatch" },
    production_eligibility: { eligible: true, reason: "eligible", source: "evaluate_production_eligibility" },
  }, null, "VALIDATION");
  assert.match(mismatch, /MISMATCH/);
  assert.match(mismatch, /cert-runtime-mismatch/);
  assert.doesNotMatch(mismatch, /cert-runtime-verified">MISMATCH/);
  assert.match(mismatch, />ELIGIBLE</);
  assert.equal(plannerEligibilityLabel({ eligible: true }), "ELIGIBLE");
  assert.equal(plannerEligibilityLabel({ eligible: false, reason: "x" }), "BLOCKED");
});

test("validation Baseline Security CERTIFIED does not imply Planner eligibility", () => {
  const html = renderCertificationWorkerDetail(certWorker);
  assert.match(html, /Baseline Security/);
  assert.match(html, /VALIDATION certificate status/);
  assert.match(html, /CERTIFIED/);
  assert.match(html, /Production eligibility — Planner/);
  assert.match(html, /BLOCKED/);
  assert.match(html, /missing_role_certificate/);
  assert.doesNotMatch(html, />ELIGIBLE</);
  assert.match(html, /Role certificates/);
  assert.match(html, /PRODUCTION role certificate/);
  assert.match(html, /Live role certification not available in v1/);
  assert.doesNotMatch(html, /data-cert-role-start|data-cert-certify-role/);
  assert.match(html, /measured_by_ollama: false/);
  assert.match(html, /CONFIG_BOUND/);
});

test("certification history keeps runs, VALIDATION certificates and PRODUCTION certificates separate", () => {
  const html = renderCertificationHistory({
    runs: [{ run_id: "run-inc", state: "INCOMPLETE", has_certificate: false }],
    validation_certificates: [{ certificate_id: "v1", outcome: "PASS", issued_at: "t" }],
    production_certificates: [{ certificate_id: "p1", outcome: "PASS", issued_at: "t" }],
  });
  assert.match(html, /Certification runs/);
  assert.match(html, /RUN run-inc/);
  assert.match(html, /A run is not a certificate/);
  assert.match(html, /has_certificate false/);
  assert.match(html, /VALIDATION certificates/);
  assert.match(html, /PRODUCTION certificates/);
  assert.match(html, /CERTIFICATE v1/);
  assert.match(html, /CERTIFICATE p1/);
  assert.match(html, /Historical evidence is not proof of current production eligibility/);
  const incomplete = renderCertificationRun({
    run_id: "run-inc",
    state: "INCOMPLETE",
    kind: "baseline_security",
    environment: "VALIDATION",
    has_certificate: false,
    certificate_id: null,
  });
  assert.match(incomplete, /INCOMPLETE/);
  assert.match(incomplete, /has_certificate false/);
  assert.match(incomplete, /A run is not a certificate/);
  const passNoCert = renderCertificationRun({
    run_id: "run-pass",
    state: "PASS",
    has_certificate: false,
    certificate_id: null,
  });
  assert.match(passNoCert, /has_certificate false/);
  assert.doesNotMatch(passNoCert, /has_certificate true/);
});

test("preflight is explicit, does not auto-start, and start is gated by ready_for_certification", async () => {
  const blocked = renderCertificationWorkerDetail(certWorker);
  assert.match(blocked, /data-cert-preflight="cw1"/);
  assert.match(blocked, /data-cert-start="cw1"/);
  assert.match(blocked, /data-cert-start="cw1" disabled/);
  assert.equal(certificationStartEnabled(certWorker), false);
  const ready = renderCertificationWorkerDetail({ ...certWorker, ready_for_certification: true });
  assert.doesNotMatch(ready, /data-cert-start="cw1" disabled/);
  assert.equal(certificationStartEnabled({ ready_for_certification: true }), true);
  const checksReady = renderCertificationWorkerDetail({
    ...certWorker,
    last_preflight: { run_id: "pre-2", state: "READY", ready: true, checks: [{ name: "digest_matches", ok: true, detail: "ok" }] },
    ready_for_certification: false,
  });
  assert.match(checksReady, /data-cert-start="cw1" disabled/);
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  const preflightBlock = appSource.slice(appSource.indexOf("if (preflightButton)"), appSource.indexOf("if (startButton)"));
  assert.match(preflightBlock, /api\.certificationPreflight\(workerId\)/);
  assert.doesNotMatch(preflightBlock, /startBaselineCertification/);
  const loadStart = appSource.indexOf("async function loadCertification()");
  const loadEnd = appSource.indexOf("async function loadCertificationWorker");
  assert.doesNotMatch(appSource.slice(loadStart, loadEnd), /startBaselineCertification|certificationPreflight/);
  assert.equal((appSource.match(/startBaselineCertification/g) || []).length, 1);
  assert.match(appSource, /api\.startBaselineCertification\(workerId\)/);
});

test("H.1: promotion availability is backend-authoritative, never a frontend inference", async () => {
  // Backend says available: enabled.
  const html = renderCertificationWorkerDetail(certWorker);
  assert.match(html, /data-cert-promote="cw1"/);
  assert.doesNotMatch(html, /data-cert-promote="cw1" disabled/);
  assert.equal(certificationPromoteEnabled(certWorker), true);
  assert.match(html, /Promote Baseline Security to PRODUCTION/);
  assert.match(html, /never grants trust, permission, or a role certificate/);
  assert.match(html, /promotion_available true/);
  assert.match(html, /reason promotable/);
  assert.match(html, /backend's own promotion_available projection only/);

  // The exact case a naive "VALIDATION PASS implies available" heuristic
  // would get wrong: baseline_security is still CERTIFIED/PASS, but the
  // backend reports it was already promoted. The button must stay
  // disabled purely because promotion_available is false -- not because
  // of anything derived from baseline_security here.
  const alreadyPromoted = {
    ...certWorker,
    promotion_available: false,
    promotion_reason: "already_promoted_to_production",
  };
  assert.equal(alreadyPromoted.baseline_security.status, "CERTIFIED");
  assert.equal(certificationPromoteEnabled(alreadyPromoted), false);
  const alreadyPromotedHtml = renderCertificationWorkerDetail(alreadyPromoted);
  assert.match(alreadyPromotedHtml, /data-cert-promote="cw1" disabled/);
  assert.match(alreadyPromotedHtml, /reason already_promoted_to_production/);

  // Every other backend-reported non-promotable reason also keeps the
  // button disabled, regardless of baseline_security/runtime display
  // fields -- the frontend performs no eligibility computation of its
  // own.
  for (const reason of [
    "no_validation_certificate",
    "validation_certificate_not_pass",
    "validation_certificate_profile_mismatch",
    "runtime_identity_fingerprint_mismatch",
    "runtime_profile_not_configured",
  ]) {
    const denied = { ...certWorker, promotion_available: false, promotion_reason: reason };
    assert.equal(certificationPromoteEnabled(denied), false, reason);
    assert.match(renderCertificationWorkerDetail(denied), /data-cert-promote="cw1" disabled/);
  }

  // A worker payload that omits the field entirely (e.g. an older/
  // malformed projection) fails closed too -- never defaults to enabled.
  const { promotion_available: _omitted, ...withoutField } = certWorker;
  assert.equal(certificationPromoteEnabled(withoutField), false);

  // busy/active-run still gate on top of a backend-available projection.
  assert.equal(certificationPromoteEnabled(certWorker, { busy: true }), false);
  assert.equal(certificationPromoteEnabled(certWorker, { activeRun: { state: "RUNNING" } }), false);
  assert.equal(certificationPromoteEnabled(certWorker, { activeRun: { state: "QUEUED" } }), false);

  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  const promoteBlock = appSource.slice(
    appSource.indexOf("if (promoteButton)"),
    appSource.indexOf("if (evidenceButton)"),
  );
  assert.match(promoteBlock, /api\.promoteBaselineCertification\(workerId\)/);
  assert.match(promoteBlock, /acceptCertificationPromotion\(state, workerId, version\)/);
  assert.match(promoteBlock, /refreshCertificationProjection\(workerId, version\)/);
  assert.equal((appSource.match(/promoteBaselineCertification/g) || []).length, 1);

  // The gating function itself must read promotion_available, never any
  // baseline_security/ready_for_certification/run-derived heuristic.
  const viewsSource = await readFile(new URL("../static/views-admin.js", import.meta.url), "utf8");
  const gateStart = viewsSource.indexOf("export function certificationPromoteEnabled");
  const gateEnd = viewsSource.indexOf("export function certificationWorkerSelectEnabled");
  const gateFn = viewsSource.slice(gateStart, gateEnd);
  assert.match(gateFn, /return worker\?\.promotion_available === true/);
});

test("H.1: a lost promotion response is not automatically retried", async () => {
  let calls = 0;
  const api = createAPI(async () => { calls++; throw new Error("lost response"); });
  await assert.rejects(
    api.promoteBaselineCertification("cw1"),
    (e) => e instanceof APIError && /Refresh durable/.test(e.message),
  );
  assert.equal(calls, 1);
  await assert.rejects(api.promoteBaselineCertification("cw1"), () => true);
  assert.equal(calls, 2);
});

test("H.2: certification GETs stay GET; planner preflight and start POST exact empty bodies", async () => {
  const calls = [];
  const api = createAPI(async (url, options) => { calls.push(captured(url, options)); return reply({}); });
  await api.certificationPlannerPreflight("w/1", { outcome: "pass" });
  await api.startPlannerCertification("w/1", { score: 1.0 });
  assert.deepEqual(calls, [
    ["/api/certification/workers/w%2F1/planner/preflight", "POST", {}],
    ["/api/certification/workers/w%2F1/planner/runs", "POST", {}],
  ]);
});

test("H.2: a lost planner certification response is not automatically retried", async () => {
  let calls = 0;
  const api = createAPI(async () => { calls++; throw new Error("lost response"); });
  await assert.rejects(
    api.startPlannerCertification("cw1"),
    (e) => e instanceof APIError && /Refresh durable/.test(e.message),
  );
  assert.equal(calls, 1);
  await assert.rejects(api.certificationPlannerPreflight("cw1"), () => true);
  assert.equal(calls, 2);
});

test("H.2: Planner certification action is a separate track from Baseline Security and the role certificate panel", () => {
  const ready = { ...certWorker, planner_ready_for_certification: true };
  const html = renderCertificationWorkerDetail(ready);
  assert.match(html, /Planner role certification/);
  assert.match(html, /data-cert-planner-preflight="cw1"/);
  assert.match(html, /data-cert-planner-start="cw1"/);
  assert.doesNotMatch(html, /data-cert-planner-start="cw1" disabled/);
  assert.match(html, /separate durable job\/run track from Baseline Security/);
  assert.match(html, /separate from the "Role certificates" panel/);
  assert.match(html, /there is no promote step here/);

  // Backend-authoritative, same discipline as H.1's promotion gate:
  // never inferred from baseline_security/ready_for_certification.
  assert.equal(certificationPlannerStartEnabled(ready), true);
  assert.equal(certificationPlannerStartEnabled(certWorker), false);
  assert.equal(
    certificationPlannerStartEnabled({ ...ready, planner_ready_for_certification: false }),
    false,
  );
  assert.equal(certificationPlannerStartEnabled(ready, { busy: true }), false);
  assert.equal(
    certificationPlannerStartEnabled(ready, { activePlannerRun: { state: "RUNNING" } }),
    false,
  );
  // A concurrent/active BASELINE run must not disable the Planner
  // action -- the two tracks are independent.
  assert.equal(
    certificationPlannerStartEnabled(ready, { activeRun: { state: "RUNNING" } }),
    true,
  );
  assert.equal(certificationPlannerPreflightEnabled(), true);
  assert.equal(certificationPlannerPreflightEnabled({ busy: true }), false);
  assert.equal(
    certificationPlannerPreflightEnabled({ activePlannerRun: { state: "QUEUED" } }),
    false,
  );

  const notReady = renderCertificationWorkerDetail(certWorker);
  assert.match(notReady, /data-cert-planner-start="cw1" disabled/);
});

test("H.2: Planner run wiring calls the right API functions and refreshes durable state", async () => {
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  const preflightBlock = appSource.slice(
    appSource.indexOf("if (plannerPreflightButton)"),
    appSource.indexOf("if (plannerStartButton)"),
  );
  assert.match(preflightBlock, /api\.certificationPlannerPreflight\(workerId\)/);
  assert.match(preflightBlock, /acceptCertificationPlannerPreflight\(state, workerId, version\)/);
  assert.match(preflightBlock, /refreshCertificationProjection\(workerId, version\)/);
  const startBlock = appSource.slice(
    appSource.indexOf("if (plannerStartButton)"),
    appSource.indexOf("if (evidenceButton)"),
  );
  assert.match(startBlock, /api\.startPlannerCertification\(workerId\)/);
  assert.match(startBlock, /acceptCertificationPlannerStart\(state, workerId, version, run\)/);
  assert.match(startBlock, /scheduleCertificationPlannerPoll\(\)/);
  assert.equal((appSource.match(/startPlannerCertification/g) || []).length, 1);
  assert.equal((appSource.match(/certificationPlannerPreflight\(/g) || []).length, 1);
});

test("H.2: worker reselection cannot bind a Planner run/preflight response to the wrong worker", () => {
  const certState = {};
  const versionA = selectCertificationWorkerId(certState, "a");
  selectCertificationWorkerId(certState, "b");
  assert.equal(certificationSelectionMatches(certState, "a", versionA), false);
  assert.equal(acceptCertificationPlannerPreflight(certState, "a", versionA), false);
  assert.equal(acceptCertificationPlannerStart(certState, "a", versionA, {
    run_id: "planner-run-a",
    worker_id: "a",
    state: "QUEUED",
  }), false);
  assert.notEqual(certState.certificationActivePlannerRun?.run_id, "planner-run-a");
  assert.equal(certState.selectedCertificationWorkerId, "b");
  const versionB = certState.certificationSelectionVersion;
  assert.equal(acceptCertificationPlannerStart(certState, "b", versionB, {
    run_id: "planner-run-b",
    worker_id: "b",
    state: "QUEUED",
  }), true);
  assert.equal(certState.certificationActivePlannerRun.run_id, "planner-run-b");
});

test("H.2: reselecting a worker clears the prior Planner run display without dropping the Baseline one's own guard", () => {
  const certState = {
    selectedCertificationWorkerId: "cw1",
    certificationActiveRun: { run_id: "baseline-run-1", state: "RUNNING" },
    certificationActivePlannerRun: { run_id: "planner-run-1", state: "RUNNING" },
  };
  selectCertificationWorkerId(certState, "cw2");
  assert.equal(certState.certificationActiveRun, null);
  assert.equal(certState.certificationActivePlannerRun, null);
});

test("H.2: applyCertificationPlannerPoll never binds a response to the wrong run or worker's baseline run", () => {
  const certState = { certificationActivePlannerRun: { run_id: "planner-run-1", state: "RUNNING" } };
  assert.equal(
    applyCertificationPlannerPoll(certState, { run_id: "baseline-run-1", state: "PASS" }),
    false,
  );
  assert.equal(certState.certificationActivePlannerRun.run_id, "planner-run-1");
  assert.equal(
    applyCertificationPlannerPoll(certState, { run_id: "planner-run-1", state: "PASS" }),
    true,
  );
  assert.equal(certState.certificationActivePlannerRun.state, "PASS");
});

test("certification polling continues for QUEUED/RUNNING and stops for terminal states", () => {
  assert.equal(shouldContinueCertificationPoll({ state: "QUEUED" }), true);
  assert.equal(shouldContinueCertificationPoll({ state: "RUNNING" }), true);
  assert.equal(isCertificationActive("QUEUED"), true);
  assert.equal(isCertificationActive("RUNNING"), true);
  for (const state of ["PASS", "FAIL", "HARD_DISQUALIFIED", "INCOMPLETE"]) {
    assert.equal(isCertificationTerminal(state), true);
    assert.equal(shouldContinueCertificationPoll({ state }), false);
  }
  assert.equal(certificationPollDelay(false), 2000);
  assert.equal(certificationPollDelay(true), 15000);
});

test("stale certification worker and run responses cannot overwrite a newer selection", () => {
  const certState = {};
  const first = selectCertificationWorkerId(certState, "a");
  selectCertificationWorkerId(certState, "b");
  assert.equal(acceptCertificationWorker(certState, "a", certWorker, first), false);
  assert.equal(certState.selectedCertificationWorker, null);
  beginCertificationRun(certState, { run_id: "new", state: "QUEUED" });
  assert.equal(applyCertificationPoll(certState, { run_id: "old", state: "PASS", has_certificate: true }), false);
  assert.equal(certState.certificationActiveRun.run_id, "new");
  assert.equal(certState.certificationActiveRun.state, "QUEUED");
  assert.equal(applyCertificationPoll(certState, { run_id: "new", state: "RUNNING" }), true);
  assert.equal(certState.certificationActiveRun.state, "RUNNING");
});

test("certification evidence is explicit, run-bound, fail-closed and escaped", () => {
  const certState = {};
  beginCertificationRun(certState, { run_id: "r1", state: "PASS", evidence_ref: "ev" });
  beginCertificationEvidenceRequest(certState, "r1");
  assert.equal(certState.certificationEvidence, null);
  assert.equal(bindCertificationEvidence({ run_id: "r2", document: { ok: true } }, "r1"), null);
  assert.equal(acceptCertificationEvidence(certState, { run_id: "r2", document: { ok: true } }, "r1"), false);
  assert.equal(acceptCertificationEvidence(certState, { run_id: "r1", environment: "VALIDATION", evidence_ref: "ev", document: { a: 1 } }, "r1"), true);
  assert.equal(certState.certificationEvidence.run_id, "r1");
  rejectCertificationEvidence(certState, "r1", "missing_durable_security_evidence");
  assert.equal(certState.certificationEvidence, null);
  assert.match(renderCertificationEvidence(null, "missing_durable_security_evidence"), /missing_durable_security_evidence/);
  const attack = '<img src=x onerror="alert(1)">';
  assert.doesNotMatch(renderCertificationEvidence({
    run_id: attack,
    environment: attack,
    evidence_ref: attack,
    document: { note: attack },
  }), /<img/);
  const runHtml = renderCertificationRun({ run_id: "r1", state: "PASS", evidence_ref: "ev", has_certificate: true });
  assert.match(runHtml, /data-cert-evidence="r1"/);
});

test("certification app wiring uses dedicated state, no fetch, and no mutation retry", async () => {
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  const adminSource = await readFile(new URL("../static/views-admin.js", import.meta.url), "utf8");
  assert.doesNotMatch(appSource, /fetch\(/);
  assert.doesNotMatch(adminSource, /fetch\(/);
  assert.doesNotMatch(appSource, /localStorage|sessionStorage|indexedDB/i);
  assert.doesNotMatch(adminSource, /localStorage|sessionStorage|indexedDB/i);
  assert.match(appSource, /certificationBusy/);
  const actionFn = appSource.slice(
    appSource.indexOf("async function certificationAction"),
    appSource.indexOf("async function selectedDetail"),
  );
  assert.doesNotMatch(actionFn, /privacyBusy/);
  assert.equal((actionFn.match(/await work\(\)/g) || []).length, 1);
  assert.match(appSource, /api\.certificationWorkers\(\)/);
  assert.match(appSource, /api\.certificationWorker\(workerId\)/);
  assert.match(appSource, /api\.certificationRun\(runId\)/);
  assert.match(appSource, /api\.certificationEvidence\(runId\)/);
  const loadWorker = appSource.slice(
    appSource.indexOf("async function loadCertificationWorker"),
    appSource.indexOf("function scheduleCertificationPoll"),
  );
  assert.doesNotMatch(loadWorker, /certificationEvidence/);
  assert.match(appSource, /ready_for_certification !== true/);
});

test("certification worker selection is disabled and ignored while busy", () => {
  const idle = renderCertificationWorkers({ environment: "VALIDATION", workers: [certWorker] }, "cw1");
  assert.match(idle, /data-cert-worker="cw1"/);
  assert.doesNotMatch(idle, /data-cert-worker="cw1" disabled/);
  const busy = renderCertificationWorkers(
    { environment: "VALIDATION", workers: [certWorker] },
    "cw1",
    { busy: true },
  );
  assert.match(busy, /data-cert-worker="cw1" disabled/);
});

test("a start or preflight response cannot bind to a different selected worker", () => {
  const certState = {};
  const versionA = selectCertificationWorkerId(certState, "a");
  selectCertificationWorkerId(certState, "b");
  assert.equal(certificationSelectionMatches(certState, "a", versionA), false);
  assert.equal(acceptCertificationStart(certState, "a", versionA, {
    run_id: "run-a",
    worker_id: "a",
    state: "QUEUED",
  }), false);
  assert.notEqual(certState.certificationActiveRun?.run_id, "run-a");
  assert.equal(certState.selectedCertificationWorkerId, "b");
  assert.equal(acceptCertificationPreflight(certState, "a", versionA), false);
  assert.equal(acceptCertificationPromotion(certState, "a", versionA), false);
  const versionB = certState.certificationSelectionVersion;
  assert.equal(acceptCertificationStart(certState, "b", versionB, {
    run_id: "run-b",
    worker_id: "b",
    state: "QUEUED",
  }), true);
  assert.equal(certState.certificationActiveRun.run_id, "run-b");
});

test("beginning a new preflight clears an old run and evidence without dropping the worker", () => {
  const certState = {
    selectedCertificationWorkerId: "cw1",
    selectedCertificationWorker: certWorker,
    certificationHistory: certWorker.history,
    certificationActiveRun: { run_id: "old-pass", state: "PASS", has_certificate: true },
    certificationEvidence: { run_id: "old-pass", document: { ok: true } },
    certificationEvidenceError: "stale",
    certificationEvidenceRequestId: "old-pass",
  };
  beginCertificationPreflight(certState);
  assert.equal(certState.certificationActiveRun, null);
  assert.equal(certState.certificationEvidence, null);
  assert.equal(certState.certificationEvidenceError, null);
  assert.equal(certState.certificationEvidenceRequestId, null);
  assert.equal(certState.selectedCertificationWorker, certWorker);
  assert.equal(certState.selectedCertificationWorkerId, "cw1");
  assert.equal(certState.certificationHistory, certWorker.history);
});

test("QUEUED and RUNNING active runs disable start and preflight; terminal states do not derive readiness", () => {
  const readyWorker = { ...certWorker, ready_for_certification: true };
  assert.equal(certificationStartEnabled(readyWorker, { activeRun: { state: "QUEUED" } }), false);
  assert.equal(certificationStartEnabled(readyWorker, { activeRun: { state: "RUNNING" } }), false);
  assert.equal(certificationPreflightEnabled({ activeRun: { state: "QUEUED" } }), false);
  assert.equal(certificationPreflightEnabled({ activeRun: { state: "RUNNING" } }), false);
  const queued = renderCertificationWorkerDetail(readyWorker, { activeRun: { state: "QUEUED" } });
  assert.match(queued, /data-cert-start="cw1" disabled/);
  assert.match(queued, /data-cert-preflight="cw1" disabled/);
  const running = renderCertificationWorkerDetail(readyWorker, { activeRun: { state: "RUNNING" } });
  assert.match(running, /data-cert-start="cw1" disabled/);
  assert.match(running, /data-cert-preflight="cw1" disabled/);
  for (const state of ["PASS", "FAIL", "HARD_DISQUALIFIED", "INCOMPLETE"]) {
    assert.equal(certificationStartEnabled(readyWorker, { activeRun: { state } }), true);
    assert.equal(certificationStartEnabled(certWorker, { activeRun: { state } }), false);
    assert.equal(certificationPreflightEnabled({ activeRun: { state } }), true);
  }
  const passNotReady = renderCertificationWorkerDetail(certWorker, { activeRun: { state: "PASS" } });
  assert.match(passNotReady, /data-cert-start="cw1" disabled/);
  assert.doesNotMatch(passNotReady, /data-cert-preflight="cw1" disabled/);
});

test("preflight and terminal runs refresh server worker summaries without inferring them", async () => {
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  const refreshFn = appSource.slice(
    appSource.indexOf("async function refreshCertificationProjection"),
    appSource.indexOf("function scheduleCertificationPoll"),
  );
  assert.match(refreshFn, /api\.certificationWorkers\(\)/);
  assert.match(refreshFn, /loadCertificationWorker\(workerId, version\)/);
  assert.match(refreshFn, /rejectCertificationSnapshot\(state\)/);
  assert.doesNotMatch(refreshFn, /ready_for_certification\s*=/);
  assert.doesNotMatch(refreshFn, /baseline_security\s*=/);
  assert.doesNotMatch(refreshFn, /production_eligibility\s*=/);
  const preflightBlock = appSource.slice(appSource.indexOf("if (preflightButton)"), appSource.indexOf("if (startButton)"));
  assert.match(preflightBlock, /beginCertificationPreflight\(state\)/);
  assert.match(preflightBlock, /acceptCertificationPreflight\(state, workerId, version\)/);
  assert.match(preflightBlock, /refreshCertificationProjection\(workerId, version\)/);
  assert.doesNotMatch(preflightBlock, /startBaselineCertification/);
  const startBlock = appSource.slice(appSource.indexOf("if (startButton)"), appSource.indexOf("if (evidenceButton)"));
  assert.match(startBlock, /acceptCertificationStart\(state, workerId, version, run\)/);
  const pollFn = appSource.slice(
    appSource.indexOf("async function pollCertificationRun"),
    appSource.indexOf("async function certificationAction"),
  );
  assert.match(pollFn, /refreshCertificationProjection\(workerId, version\)/);
  assert.match(appSource, /if \(state\.certificationBusy\) return/);
});


test("Settings replaces placeholders with authoritative System and Tailscale cards",async()=>{const h=await readFile(new URL("../index.html",import.meta.url),"utf8");assert.match(h,/id="system-settings-card"/);assert.match(h,/System &amp; deployment/);assert.match(h,/id="tailscale-settings-card"/);assert.match(h,/Remote access — Tailscale Serve/);assert.doesNotMatch(h,/Cloud authorization/);});
test("system snapshot generations reject stale GETs and failures discard prior evidence",()=>{const x={};const a=beginSystemRequest(x),b=beginSystemRequest(x);assert.equal(acceptSystemSnapshot(x,{service:{deployment_status:"VERIFIED"}},a),false);assert.equal(acceptSystemSnapshot(x,{service:{deployment_status:"DIRTY"}},b),true);assert.equal(rejectSystemSnapshot(x,b),true);assert.equal(x.system,null);assert.equal(acceptSystemSnapshot(x,{service:{deployment_status:"VERIFIED"}},b),false);});
test("Tailscale generations reject stale GETs and mutation failures invalidate prior evidence",()=>{const x={};const a=beginTailscaleRequest(x),b=beginTailscaleRequest(x);assert.equal(acceptTailscaleSnapshot(x,{remote_access:"VERIFIED"},a),false);assert.equal(acceptTailscaleSnapshot(x,{remote_access:"MISMATCH"},b),true);assert.equal(rejectTailscaleSnapshot(x,b),true);assert.equal(x.tailscale,null);assert.equal(acceptTailscaleSnapshot(x,{remote_access:"VERIFIED"},b),false);});
test("system renderer displays backend deployment states without deriving them",()=>{const x={service:{state:"active",running:true,unit:"codeslayer.service",source:"systemd",version:"1",process_commit:"process-aaa",process_commit_source:"VERIFIED",process_source_dirty:false,process_source_state:"VERIFIED",running_commit:"process-aaa",running_commit_source:"VERIFIED",checkout_head:"checkout-bbb",checkout_head_source:"VERIFIED",checkout_source_dirty:false,checkout_source_state:"VERIFIED",checkout_source_state_source:"LIVE_ATTESTED",deployment_status:"MISMATCH",deployment_complete:false,uptime_seconds:10},network:{local_url:"http://127.0.0.1:8765",bind_host:"127.0.0.1",bind_port:8765},health:{status:"ok",schema_version:1,source:"backend"}};for(const st of ["VERIFIED","DIRTY","MISMATCH","UNVERIFIED"])assert.match(renderSystemSettings({...x,service:{...x.service,deployment_status:st}}),new RegExp(st));const h=renderSystemSettings(x,{operation:{kind:"update_apply",status:"RESPONSE_RECEIVED",result:{deployment_complete:false,restart_requested:true}}});assert.match(h,/process-aaa/);assert.match(h,/checkout-bbb/);assert.match(h,/Operation response — not current system state/);assert.match(h,/does not compare commits to derive a verdict/);assert.match(h,/Refresh current state/);});
test("Tailscale renderer keeps intent Serve and remote access distinct with no Funnel control",()=>{const x={node:{state:"RUNNING",source:"LIVE_ATTESTED"},serve:{status:"MISMATCH",source:"LIVE_ATTESTED",expected_backend:"http://127.0.0.1:8765",observed_backend:"http://127.0.0.1:9999",funnel_detected:true,hosts:["node.ts.net"]},host:{name:"node.ts.net",source:"LIVE_ATTESTED",accepted:false,accepted_source:"LIVE_ATTESTED"},intent:{enabled:true,enabled_source:"CONFIG_BOUND",alignment:"MISMATCH"},remote_access:"UNVERIFIED",url:null,backend:"http://127.0.0.1:8765",enabled:true,enabled_source:"CONFIG_BOUND",alignment:"MISMATCH",detail:"backend mismatch",source:"LIVE_ATTESTED"};const h=renderTailscaleSettings(x);assert.match(h,/funnel_detected is true/);assert.match(h,/Intent enabled, Serve status, and remote_access are distinct fields/);assert.match(h,/network\/Serve\/Host-path verdict/);assert.match(h,/node\.ts\.net/);assert.doesNotMatch(h,/Enable Funnel|Disable Funnel|data-[^=]*funnel/i);});
test("Settings app wiring is GET-only on page load and uses explicit fixed mutations",async()=>{const a=await readFile(new URL("../static/app.js",import.meta.url),"utf8"),v=await readFile(new URL("../static/views-admin.js",import.meta.url),"utf8");assert.doesNotMatch(a,/fetch\(/);assert.doesNotMatch(v,/fetch\(/);assert.doesNotMatch(a,/localStorage|sessionStorage|indexedDB/i);assert.doesNotMatch(v,/localStorage|sessionStorage|indexedDB/i);assert.doesNotMatch(a,/window\.confirm|confirm\(/);const p=a.slice(a.indexOf("async function loadSettings()"),a.indexOf("async function runSystemMutation"));assert.match(p,/loadSystemSettings/);assert.match(p,/loadTailscaleSettings/);assert.doesNotMatch(p,/systemUpdateCheck|systemUpdateApply|systemRestart|tailscaleEnable|tailscaleDisable/);assert.match(a,/api\.systemUpdateCheck\(\)/);assert.match(a,/api\.systemUpdateApply\(\)/);assert.match(a,/api\.systemRestart\(\)/);assert.match(a,/api\.tailscaleEnable\(\)/);assert.match(a,/api\.tailscaleDisable\(\)/);assert.match(a,/status:e\?\.code==="disconnected"\?"UNKNOWN":"FAILED"/);});


test("Dashboard has four read-only control-plane summary cards and no admin action controls", async () => {
  const html = await readFile(new URL("../index.html", import.meta.url), "utf8");
  const start = html.indexOf('id="dashboard-control-summary"');
  const end = html.indexOf("TECHNICAL ACTIVITY", start);
  assert.ok(start >= 0 && end > start);
  const block = html.slice(start, end);
  for (const id of ["dashboard-system-summary", "dashboard-runtime-summary", "dashboard-certification-summary", "dashboard-tailscale-summary"]) assert.match(block, new RegExp(`id="${id}"`));
  assert.match(block, /Read-only summaries/);
  assert.match(block, /never[\s\S]*administrative mutation/);
  assert.doesNotMatch(block, /<button|<form|data-runtime-|data-cert-|tailscale-enable|system-restart/i);
});

test("Dashboard summary generations reject stale responses and disconnect invalidates all snapshots", () => {
  const x = {};
  const first = beginDashboardSummaryRequest(x);
  const second = beginDashboardSummaryRequest(x);
  assert.equal(acceptDashboardSummary(x, first, { system: { service: { deployment_status: "VERIFIED" } } }), false);
  assert.equal(acceptDashboardSummary(x, second, { system: { service: { deployment_status: "DIRTY" } } }), true);
  assert.equal(x.dashboardSummary.system.service.deployment_status, "DIRTY");
  assert.equal(rejectDashboardSummary(x, second, "Backend disconnected."), true);
  assert.equal(x.dashboardSummary, null);
  assert.equal(x.dashboardSummaryUnavailable, true);
  assert.equal(acceptDashboardSummary(x, second, { system: { service: { deployment_status: "VERIFIED" } } }), false);
});

test("Dashboard deployment summary displays backend verdict and commits without comparing them", () => {
  const html = renderDashboardSystemSummary({ service: { deployment_status: "MISMATCH", deployment_complete: false, process_commit: "process-aaa", checkout_head: "checkout-bbb" } });
  assert.match(html, /MISMATCH/);
  assert.match(html, /deployment_complete false/);
  assert.match(html, /process-aaa/);
  assert.match(html, /checkout-bbb/);
  assert.doesNotMatch(html, /matches|same commit|derived/i);
});

test("Dashboard runtime and certification summaries remain configuration/projection only", () => {
  const runtime = renderDashboardRuntimeSummary({ ollama_servers: [{ id: "a" }, { id: "b" }], workers: [{ worker_id: "w1" }] });
  assert.match(runtime, /1 workers/);
  assert.match(runtime, /2 Ollama servers/);
  assert.match(runtime, /not live attestation/);
  assert.doesNotMatch(runtime, /LIVE_ATTESTED|VERIFIED/);
  const cert = renderDashboardCertificationSummary({ environment: "VALIDATION", workers: [{ worker_id: "w1", production_eligibility: { eligible: true } }] });
  assert.match(cert, /VALIDATION/);
  assert.match(cert, /1 certification workers/);
  assert.match(cert, /does not derive Planner eligibility/);
  assert.doesNotMatch(cert, />ELIGIBLE<|>BLOCKED</);
});

test("Dashboard remote-access summary keeps backend remote_access, Serve and intent distinct", () => {
  const html = renderDashboardTailscaleSummary({ remote_access: "UNVERIFIED", serve: { status: "MISMATCH" }, intent: { enabled: true, alignment: "MISMATCH" } });
  assert.match(html, /UNVERIFIED/);
  assert.match(html, /Serve MISMATCH/);
  assert.match(html, /intent enabled true/);
  assert.match(html, /alignment MISMATCH/);
  assert.match(html, /network\/Serve\/Host-path verdict only/);
});

test("Dashboard summary loader calls only GET admin projections and never mutates other admin state", async () => {
  const appSource = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  const start = appSource.indexOf("async function loadDashboardSummaries()");
  const end = appSource.indexOf("function settingsControls()", start);
  assert.ok(start >= 0 && end > start);
  const block = appSource.slice(start, end);
  assert.match(block, /api\.system\(\)/);
  assert.match(block, /api\.runtime\(\)/);
  assert.match(block, /api\.certificationWorkers\(\)/);
  assert.match(block, /api\.tailscale\(\)/);
  assert.doesNotMatch(block, /systemRestart|systemUpdate|runtimeAttest|addOllama|registerRuntime|approveRuntime|certificationPreflight|startBaselineCertification|tailscaleEnable|tailscaleDisable/);
  assert.doesNotMatch(block, /acceptRuntimeSnapshot|acceptCertificationWorkers|acceptSystemSnapshot|acceptTailscaleSnapshot/);
  assert.match(block, /acceptDashboardSummary/);
});

test("Dashboard summary renderers escape backend-provided strings", () => {
  const attack = '<img src=x onerror="alert(1)">';
  assert.doesNotMatch(renderDashboardSystemSummary({ service: { deployment_status: attack, deployment_complete: attack, process_commit: attack, checkout_head: attack } }), /<img/);
  assert.doesNotMatch(renderDashboardCertificationSummary({ environment: attack, workers: [] }), /<img/);
  assert.doesNotMatch(renderDashboardTailscaleSummary({ remote_access: attack, serve: { status: attack }, intent: { enabled: attack, alignment: attack } }), /<img/);
});
