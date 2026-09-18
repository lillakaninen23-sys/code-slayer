import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createAPI, APIError } from "../static/api.js";
import { workerAlias, saveWorkerAlias } from "../static/aliases.js";
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
  assert.deepEqual(calls, [
    ["/api/certification/workers", "GET", undefined],
    ["/api/certification/workers/w%2F1", "GET", undefined],
    ["/api/certification/runs/run%2F1", "GET", undefined],
    ["/api/certification/runs/run%2F1/evidence", "GET", undefined],
    ["/api/certification/workers/w%2F1/history", "GET", undefined],
    ["/api/certification/workers/w%2F1/baseline/preflight", "POST", {}],
    ["/api/certification/workers/w%2F1/baseline/runs", "POST", {}],
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
