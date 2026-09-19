/** Pure presentation functions. These never decide permission or derive trust. */
export const escapeHTML = (value) =>
  String(value ?? "—").replace(
    /[&<>"']/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[char],
  );
export const statusClass = (status) =>
  ({
    COMPLETED: "ok",
    GUARDED: "ok",
    PASSED: "ok",
    FAILED: "error",
    DENIED_TRUST: "error",
    BLOCKED_ON_QUESTIONS: "warning",
    INTERRUPTED_RESUMABLE: "warning",
  })[status] || "muted";
export const badge = (status) =>
  `<span class="badge ${statusClass(status)}">${escapeHTML(status)}</span>`;
export const connectionText = (state) =>
  ({
    loading: "Connecting to backend…",
    connected: "Connected to local backend.",
    disconnected: "Backend disconnected. Displayed data may be stale.",
  })[state];
export const pollDelay = (status, connected, hidden) =>
  hidden || !connected
    ? 30000
    : ["RUNNING", "ANALYZING", "READY"].includes(status)
      ? 4000
      : 15000;
export function renderRuns(runs, selected) {
  return runs.length
    ? runs
        .map(
          (run) =>
            `<button class="run-row ${run.run_id === selected ? "selected" : ""}" data-run="${escapeHTML(run.run_id)}"><span>${escapeHTML(run.run_id.slice(0, 12))}<small>${escapeHTML(run.worker_id)} · ${escapeHTML(run.role)}</small></span>${badge(run.status)}</button>`,
        )
        .join("")
    : '<p class="muted-text">No durable runs yet.</p>';
}
export function renderRun(run) {
  if (!run) return '<p class="muted-text">Select a run or start a task.</p>';
  return `<p>Task state: <strong>${escapeHTML(run.task_status || "Not created")}</strong></p><p>${escapeHTML(run.reason || run.status)}</p><p class="muted-text">Next action: ${escapeHTML(run.next_safe_action)}</p>${run.execution_state_available ? "" : '<p class="notice error">Execution state is unavailable.</p>'}`;
}
export function renderQuestions(run) {
  return (run?.questions || [])
    .map(
      (q) =>
        `<form class="question-form" data-question="${escapeHTML(q.ambiguity_id)}"><label>${escapeHTML(q.question)}<textarea name="answer" rows="2" required maxlength="16384" aria-label="Answer to ${escapeHTML(q.question)}"></textarea></label><label>Answer kind<select name="resolution_kind"><option value="FACT">Factual answer</option><option value="AUTHORIZATION">Explicit authorization for this question</option></select></label><span class="muted-text">${escapeHTML(q.risk_class)}${q.answer_recorded ? " · Answer recorded; resume to re-evaluate." : ""}</span><button class="ghost small" type="submit">Record answer</button></form>`,
    )
    .join("");
}
export function renderTrust(trust) {
  if (!trust.scopes.length) {
    return `
      <div class="trust-section">
        <div class="section-label">TRUST & ACCESS</div>

        <p class="muted-text">
          No recorded scopes.
          Unrecorded scopes: ${escapeHTML(trust.unrecorded_scope_level)}.
        </p>
      </div>
    `;
  }

  const capabilityLabels = {
    read_file: "Read files",
    write_file: "Write files",
  };

  const rows = trust.scopes
    .map((scope) => {
      const label =
        scope.capability === null
          ? "Role access"
          : capabilityLabels[scope.capability] || scope.capability;

      const technical =
        scope.capability === null
          ? `${scope.role} / role scope`
          : `${scope.role} / ${scope.capability}`;

      return `
        <div class="trust-row">

          <div class="trust-info">
            <div class="trust-name">
              ${escapeHTML(label)}
            </div>

            <div class="trust-technical">
              ${escapeHTML(technical)}
            </div>
          </div>

          ${badge(scope.level)}

        </div>
      `;
    })
    .join("");

  return `
    <section class="trust-section">

      <div class="section-label">
        TRUST & ACCESS
      </div>

      <div class="trust-list">
        ${rows}
      </div>

    </section>
  `;
}
export function renderAudit(events) {
  return events.length
    ? events
        .map(
          (event) =>
            `<div class="timeline-item"><span class="time">${escapeHTML(event.occurred_at)}</span><span class="event">${escapeHTML(event.event_type)}</span><span class="detail">${escapeHTML(Object.values(event.details).filter(Boolean).join(" · "))}${event.association === "shared_prompt_identity" ? " · Shared prompt evidence" : ""}</span></div>`,
        )
        .join("")
    : '<p class="muted-text">No audit events for this run.</p>';
}
export function renderConformance(data) {
  return data.runs.length
    ? data.runs
        .map(
          (run) =>
            `<details><summary>${escapeHTML(run.role)} · ${escapeHTML(run.suite_version)} · ${escapeHTML(run.status)}${run.suite_version !== data.current_suite_version ? " (historical suite)" : ""}</summary>${run.results.map((r) => `<div class="scope"><span>${escapeHTML(r.case_name)} — ${escapeHTML(r.reason)}</span>${badge(r.passed ? "PASSED" : "FAILED")}</div>`).join("")}</details>`,
        )
        .join("")
    : '<p class="muted-text">No conformance runs recorded.</p>';
}

/** Phase 8.1: deterministic, read-only repository intelligence. Never
 * renders a filesystem/DB path, trust control, or tool-permission input
 * -- there is nothing here for the user to grant; every value shown is
 * durable backend evidence. */
export function intelStatusClass(status) {
  if (!status.indexed) return "muted";
  return status.current ? "ok" : "warning";
}
export function intelStatusLabel(status) {
  if (!status.indexed) return "NOT INDEXED";
  return status.current ? "CURRENT" : "STALE";
}
function formatIntelTimestamp(value) {
  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("sv-SE", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}
export function renderIntelStatus(status) {
  if (!status.indexed) {
    return `
      <div class="intel-index-empty">
        <p class="muted-text">
          This repository has not been indexed yet.
          Refresh to build a deterministic repository-intelligence snapshot.
        </p>
      </div>
    `;
  }

  const head = (status.head_sha || "unborn").slice(0, 10);

  return `
    <div class="intel-index-grid">

      <div class="intel-index-stat">
        <div class="intel-index-label">HEAD</div>
        <div class="intel-index-value">
          ${escapeHTML(head)}
        </div>
      </div>

      <div class="intel-index-stat">
        <div class="intel-index-label">FILES</div>
        <div class="intel-index-value">
          ${escapeHTML(status.file_count)}
          <span class="intel-index-unit">indexed</span>
        </div>
      </div>

      <div class="intel-index-stat">
        <div class="intel-index-label">SNAPSHOT</div>
        <div class="intel-index-value intel-index-date">
          ${escapeHTML(formatIntelTimestamp(status.created_at))}
        </div>
      </div>

    </div>

    ${
      status.working_tree_dirty
        ? `
          <div class="intel-index-note">
            Working tree has local changes
          </div>
        `
        : ""
    }

    ${
      status.inventory_truncated
        ? `
          <div class="notice warning">
            Repository inventory was truncated because it exceeds the bounded indexing limit.
          </div>
        `
        : ""
    }

    ${
      status.current
        ? ""
        : `
          <div class="notice warning">
            The repository has changed since this snapshot.
            Refresh the index for current results.
          </div>
        `
    }
  `;
}
export function renderIntelProjects(projects) {
  if (!(projects || []).length) {
    return '<p class="muted-text">No project/language evidence detected.</p>';
  }

  return projects
    .map((p) => {
      const facts = p.facts || {};
      const tools = facts.configured_tools || [];
      const projectName = facts.name || "Unnamed project";
      const pythonVersion = facts.requires_python || "Not specified";

      const toolChips = tools.length
        ? tools
            .map(
              (tool) =>
                `<span class="intel-chip">${escapeHTML(tool)}</span>`,
            )
            .join("")
        : '<span class="muted-text">No tools detected</span>';

      return `
        <div class="intel-project-card">

          <div class="intel-project-head">
            <div>
              <div class="eyebrow">
                ${escapeHTML(p.kind)}
              </div>

              <div class="intel-project-name">
                ${escapeHTML(projectName)}
              </div>
            </div>

            <span class="badge muted">
              DETECTED
            </span>
          </div>

          <div class="intel-project-meta">
            <span>Python ${escapeHTML(pythonVersion)}</span>
            <span>${escapeHTML(p.evidence_paths.join(", "))}</span>
          </div>

          <div class="intel-tools">
            ${toolChips}
          </div>

          ${
            Object.keys(facts).length
              ? `
                <details class="intel-technical">
                  <summary>Technical details</summary>

                  <div class="intel-facts">

                    <div class="intel-fact-row">
                      <span>Project name</span>
                      <strong>${escapeHTML(projectName)}</strong>
                    </div>

                    <div class="intel-fact-row">
                      <span>Python requirement</span>
                      <strong>${escapeHTML(pythonVersion)}</strong>
                    </div>

                    <div class="intel-fact-row">
                      <span>Configured tools</span>
                      <strong>${escapeHTML(tools.length)}</strong>
                    </div>

                    <div class="intel-fact-row">
                      <span>Evidence</span>
                      <strong>${escapeHTML(p.evidence_paths.join(", "))}</strong>
                    </div>

                  </div>
                </details>
              `
              : ""
          }

        </div>
      `;
    })
    .join("");
}
export function renderIntelCommands(commands) {
  if (!(commands || []).length) {
    return '<p class="muted-text">No test/lint/build commands discovered from repository evidence.</p>';
  }

  return `
    <div class="intel-command-list">
      ${commands
        .map(
          (c) => `
            <div class="intel-command-row">

              <div class="intel-command-info">

                <div class="intel-command-purpose">
                  ${escapeHTML(c.purpose)}
                </div>

                <code class="intel-command-code">
                  ${escapeHTML(c.command)}
                </code>

                <div class="intel-command-source">
                  ${escapeHTML(c.evidence_source)}
                </div>

              </div>

              ${badge(c.confidence.toUpperCase())}

            </div>
          `,
        )
        .join("")}
    </div>
  `;
}
export function renderIntelResults(result) {
  if (!result)
    return '<p class="muted-text">Describe a task above to find relevant files.</p>';

  const notice = result.stale
    ? '<p class="notice warning">Results are from a stale snapshot — refresh the index for current results.</p>'
    : "";

  if (!result.candidates.length)
    return `${notice}<p class="muted-text">No relevant files found for this description.</p>`;

  return (
    notice +
    result.candidates
      .map(
        (c) =>
          `<details class="scope-details"><summary>${escapeHTML(c.path)} <span class="muted-text">score ${escapeHTML(c.score)}</span></summary><ul>${c.reasons.map((r) => `<li>${escapeHTML(r)}</li>`).join("")}</ul></details>`,
      )
      .join("")
  );
}
/** Phase 8.2: durable engineering planning. Planning only -- nothing here
 * ever asserts a file was actually changed, a command ran, or any trust
 * was granted; every affected-file claim shown carries its own evidence
 * reasons alongside the model's stated reason, never the model's prose
 * alone. */
export function planStateClass(state) {
  return (
    {
      READY: "ok",
      STALE: "warning",
      NEEDS_INPUT: "warning",
      DRAFT: "muted",
      SUPERSEDED: "muted",
    }[state] || "muted"
  );
}
// A dedicated badge, deliberately not the shared `badge()`/`statusClass()`
// pair -- those already give distinct meaning to run/trust/conformance
// statuses (e.g. a run's own READY renders muted, meaning merely
// "not yet resumed") that a plan's READY (evidence-validated, ambiguity-
// free) must never silently inherit or reinterpret.
export const planBadge = (state) =>
  `<span class="badge ${planStateClass(state)}">${escapeHTML(state)}</span>`;

export function renderPlanList(plans, selectedId) {
  if (!plans.length) {
    return '<p class="muted-text">No plans yet. Describe an engineering request above.</p>';
  }

  return `
    <div class="plan-grid">
      ${plans
        .map((plan) => {
          const title =
            plan.content?.goal ||
            `Plan ${plan.plan_id.slice(0, 8)}`;

          const created = formatIntelTimestamp(plan.created_at);

          return `
            <button
              class="plan-card ${plan.plan_id === selectedId ? "selected" : ""}"
              data-plan="${escapeHTML(plan.plan_id)}"
            >
              <div class="plan-card-head">
                <div class="plan-card-title">
                  ${escapeHTML(title)}
                </div>

                ${planBadge(plan.effective_state)}
              </div>

              <div class="plan-card-meta">
                <span>Revision ${escapeHTML(plan.revision)}</span>
                <span>${escapeHTML(created)}</span>
              </div>
            </button>
          `;
        })
        .join("")}
    </div>
  `;
}

export function renderPlanAffectedFiles(files) {
  if (!files || !files.length)
    return '<p class="muted-text">No affected files proposed.</p>';
  return files
    .map(
      (f) =>
        `<details class="scope-details"><summary>${escapeHTML(f.path)} <span class="muted-text">${escapeHTML(f.action)}</span> ${f.exists_in_repository ? '<span class="badge ok">EXISTING</span>' : '<span class="badge warning">PROPOSED</span>'}</summary><p>${escapeHTML(f.reason)}</p><ul>${(f.evidence || []).map((e) => `<li>${escapeHTML(e.kind)}: ${escapeHTML(e.key)}</li>`).join("") || '<li class="muted-text">No evidence reference recorded.</li>'}</ul></details>`,
    )
    .join("");
}

export function renderStringList(items) {
  return (items || []).length
    ? `<ul>${items.map((i) => `<li>${escapeHTML(i)}</li>`).join("")}</ul>`
    : '<p class="muted-text">None recorded.</p>';
}

export function renderPlanCommands(commands) {
  return (commands || []).length
    ? commands
        .map(
          (c) =>
            `<div class="scope"><span>${escapeHTML(c.purpose)}: <code>${escapeHTML(c.command)}</code><br><small class="muted-text">${escapeHTML(c.evidence_source)}</small></span></div>`,
        )
        .join("")
    : '<p class="muted-text">No discovered commands relied on. Never executed by Code Slayer.</p>';
}

export function renderPlanQuestions(record) {
  const questions = (record && record.questions) || [];
  const open = questions.filter((q) => !q.resolved);
  if (!open.length) return "";
  return open
    .map(
      (q) =>
        `<form class="question-form" data-plan-question="${escapeHTML(q.ambiguity_id)}"><label>${escapeHTML(q.question)}<textarea name="answer" rows="2" required maxlength="16384" aria-label="Answer to ${escapeHTML(q.question)}"></textarea></label><label>Answer kind<select name="resolution_kind"><option value="FACT">Factual answer</option><option value="AUTHORIZATION">Explicit authorization for this question</option></select></label><span class="muted-text">${escapeHTML(q.risk_class)}${q.answer_recorded ? " · Answer recorded; resume to re-evaluate." : ""}</span><button class="ghost small" type="submit">Record answer</button></form>`,
    )
    .join("");
}

/** Phase 8.2d: a background job's execution state is a separate concern
 * from the plan's own content state -- SUCCEEDED never means READY, and
 * FAILED never exposes raw model output, only a small safe category. */
export function jobStateClass(state) {
  return (
    { SUCCEEDED: "ok", FAILED: "error", RUNNING: "warning", QUEUED: "muted" }[
      state
    ] || "muted"
  );
}
export const jobBadge = (state) =>
  `<span class="badge ${jobStateClass(state)}">${escapeHTML(state)}</span>`;

export function renderJobStatus(job) {
  if (!job) return "";
  if (job.state === "QUEUED" || job.state === "RUNNING") {
    return `<p class="notice">${jobBadge(job.state)} Planning is running on the server in the background. You may close this tab, lock your phone, or lose the connection -- it keeps going.</p>`;
  }
  if (job.state === "FAILED") {
    return `<p class="notice error">${jobBadge(job.state)} This planning attempt failed${job.failure_category ? ` (${escapeHTML(job.failure_category)})` : ""}. Replan to try again.</p>`;
  }
  return `<p class="notice">${jobBadge(job.state)} Planning attempt finished.</p>`;
}

/** H.4 (+ H.4.1 for the timeout field): the backend-selected, durably-
 * bound Planner routing authority for one job -- worker_id/certificate
 * ids/output budget/tool-choice profile/timeout/policy version are
 * bounded, non-secret provenance (never an API key or raw runtime
 * credential), safe to render directly. There is deliberately no
 * worker selector and no "change worker" control anywhere in this
 * view: the backend already made this decision, and it is immutable
 * for this job once made (see `planning.routing`'s own module
 * docstring). `job.worker_id` is null only for a job created before
 * this routing existed (schema v18 and earlier) -- rendered as an
 * explicit "unbound (legacy)" state, never blank/silently omitted.
 * `job.planner_timeout_seconds` alone can ALSO be null for an
 * otherwise-bound schema-v19 job (created before H.4.1) -- rendered as
 * "—", never a guessed value. */
export function renderPlannerRouteBinding(job) {
  if (!job) return "";
  if (!job.worker_id) {
    return `
      <div class="plan-binding">
        <div class="plan-binding-item">
          <span class="plan-binding-label">Planner worker</span>
          <strong class="muted-text">Unbound (legacy job)</strong>
        </div>
      </div>
    `;
  }
  const shortCert = (id) => (id ? escapeHTML(String(id).slice(0, 12)) : "—");
  return `
    <div class="plan-binding">
      <div class="plan-binding-item">
        <span class="plan-binding-label">Planner worker</span>
        <strong>${escapeHTML(job.worker_id)}</strong>
      </div>

      <div class="plan-binding-item">
        <span class="plan-binding-label">Baseline Security certificate</span>
        <strong>${shortCert(job.security_certificate_id)}</strong>
      </div>

      <div class="plan-binding-item">
        <span class="plan-binding-label">Planner role certificate</span>
        <strong>${shortCert(job.role_certificate_id)}</strong>
      </div>

      <div class="plan-binding-item">
        <span class="plan-binding-label">Output token budget</span>
        <strong>${escapeHTML(job.output_token_budget)}</strong>
      </div>

      <div class="plan-binding-item">
        <span class="plan-binding-label">Tool-choice enforcement</span>
        <strong>${escapeHTML(job.tool_choice_enforcement)}</strong>
      </div>

      <div class="plan-binding-item">
        <span class="plan-binding-label">Planner timeout</span>
        <strong>${
          job.planner_timeout_seconds != null
            ? `${escapeHTML(job.planner_timeout_seconds)} s`
            : "—"
        }</strong>
      </div>
    </div>
  `;
}

export function renderPlanDetail(record, job) {
  if (!record) {
    return `
      <div class="plan-empty">
        <div class="plan-empty-title">No plan selected</div>
        <div class="muted-text">
          Select a plan above to inspect its engineering details.
        </div>
      </div>
    `;
  }

  const content = record.content;
  const jobNotice = renderJobStatus(job);

  const staleNotice =
    record.effective_state === "STALE"
      ? `
        <div class="notice warning">
          The repository has changed since this plan was built.
          Replan for current evidence.
        </div>
      `
      : "";

  const plannerRouteBinding = renderPlannerRouteBinding(job);

  const binding = `
    <div class="plan-binding">

      <div class="plan-binding-item">
        <span class="plan-binding-label">
          Repository revision
        </span>

        <strong>
          ${escapeHTML((record.head_sha || "unborn").slice(0, 10))}
        </strong>
      </div>

      <div class="plan-binding-item">
        <span class="plan-binding-label">
          Plan revision
        </span>

        <strong>
          ${escapeHTML(record.revision)}
        </strong>
      </div>

      <div class="plan-binding-item">
        <span class="plan-binding-label">
          Intelligence snapshot
        </span>

        <strong>
          ${escapeHTML(
            (record.intelligence_snapshot_id || "").slice(0, 12),
          )}
        </strong>
      </div>

      ${
        record.working_tree_dirty
          ? `
            <div class="plan-binding-state">
              <span class="badge warning">LOCAL CHANGES</span>
            </div>
          `
          : `
            <div class="plan-binding-state">
              <span class="badge ok">CLEAN</span>
            </div>
          `
      }

    </div>
  `;

  if (!content) {
    if (job && (job.state === "QUEUED" || job.state === "RUNNING")) {
      return `
        ${jobNotice}
        ${binding}
        ${plannerRouteBinding}

        <div class="plan-empty">
          <div class="muted-text">
            Waiting for the background planning job to finish…
          </div>
        </div>
      `;
    }

    return `
      ${jobNotice}
      ${staleNotice}
      ${binding}
      ${plannerRouteBinding}

      <div class="plan-error">

        <div class="section-label">
          PLANNING ERROR
        </div>

        <div class="plan-error-title">
          Planner output could not be validated
        </div>

        <div class="plan-error-description">
          Code Slayer rejected the planner response because it did not match
          the required structured plan format.
        </div>

        <div class="plan-error-technical">
          <span>Technical reason</span>
          <code>${escapeHTML(record.reason)}</code>
        </div>

      </div>
    `;
  }

  return `
    ${jobNotice}
    ${staleNotice}
    ${binding}
    ${plannerRouteBinding}

    <div class="plan-detail-section plan-goal">
      <div class="section-label">GOAL</div>
      <div class="plan-goal-text">
        ${escapeHTML(content.goal)}
      </div>
    </div>

    <div class="plan-detail-grid">

      <section class="plan-detail-section">
        <div class="section-label">REQUIREMENTS</div>
        ${renderStringList(content.requirements)}
      </section>

      <section class="plan-detail-section">
        <div class="section-label">ASSUMPTIONS</div>
        ${renderStringList(content.assumptions)}
      </section>

    </div>

    <section class="plan-detail-section">
      <div class="section-label">AFFECTED FILES</div>
      ${renderPlanAffectedFiles(content.affected_files)}
    </section>

    <div class="plan-detail-grid">

      <details class="plan-detail-panel">
        <summary>Planned changes</summary>
        ${renderStringList(
          content.planned_changes.map((change) => change.description),
        )}
      </details>

      <details class="plan-detail-panel">
        <summary>Risks</summary>
        ${renderStringList(content.risks)}
      </details>

      <details class="plan-detail-panel">
        <summary>Verification</summary>
        ${renderStringList(content.verification_steps)}
      </details>

      <details class="plan-detail-panel">
        <summary>Discovered commands</summary>
        ${renderPlanCommands(content.discovered_commands)}
      </details>

    </div>

    ${
      content.validation_issues.length
        ? `
          <details class="plan-detail-panel">
            <summary>Evidence validation</summary>
            ${renderStringList(content.validation_issues)}
          </details>
        `
        : ""
    }
  `;
}

/** CSLR Governance Foundation, slice G2: the Permission Engine's consent
 * UX. Every explanation rendered here comes from trusted backend
 * `PermissionDefinition` metadata (`definition` on a request/grant) --
 * never model prose, never arbitrary server error text. Allow and "Not
 * now" are rendered with identical button styling (no dark pattern: no
 * button is visually louder than the other), there is no preselected
 * choice, and no timer ever auto-decides anything. */
export function permissionSensitivityBadge(sensitivity) {
  const cls =
    { HIGH: "warning", MEDIUM: "muted", LOW: "muted" }[sensitivity] || "muted";
  return `<span class="badge ${cls}">${escapeHTML(sensitivity)}</span>`;
}

function renderBulletList(items, empty) {
  return (items || []).length
    ? `<ul>${items.map((i) => `<li>${escapeHTML(i)}</li>`).join("")}</ul>`
    : `<p class="muted-text">${escapeHTML(empty)}</p>`;
}

export function renderPermissionTechnicalDetails(definition) {
  if (!definition) return "";
  return `<details><summary>Technical details</summary>
    <p class="muted-text">Permission key/version: <code>${escapeHTML(definition.permission_key)}</code> · <code>${escapeHTML(definition.semantic_version)}</code></p>
    <p class="muted-text">Implementation/source reference: <code>${escapeHTML(definition.implementation_reference)}</code></p>
    ${renderBulletList(definition.technical_details, "No further technical detail recorded yet.")}
  </details>`;
}

export function renderPermissionExplanation(definition) {
  if (!definition) {
    return '<p class="notice error">No trusted definition metadata is available for this permission -- treat it as denied.</p>';
  }
  return `<div class="permission-explanation">
    <p><strong>What it will do</strong></p>${renderBulletList(definition.what_it_does, "Nothing recorded.")}
    <p><strong>What it will NOT do</strong></p>${renderBulletList(definition.what_it_does_not_do, "Nothing recorded.")}
    <p><strong>Data observed</strong></p>${renderBulletList(definition.data_observed, "None.")}
    <p><strong>Data retained</strong></p>${renderBulletList(definition.data_retained, "None.")}
    <p><strong>Data transmitted</strong></p>${renderBulletList(definition.data_transmitted, "None -- nothing leaves this machine as a result of this permission alone.")}
    <p class="muted-text">${definition.revocable ? "This permission can be revoked at any time." : "This permission cannot be revoked."}</p>
    ${renderPermissionTechnicalDetails(definition)}
  </div>`;
}

export function renderPendingPermissionRequest(request) {
  const d = request.definition;
  const title = d ? d.user_title : request.permission_key;
  const summary = d ? d.user_summary : "";
  return `<article class="card" data-request="${escapeHTML(request.request_id)}">
    <div class="card-head"><div><h3>${escapeHTML(title)}</h3></div>${permissionSensitivityBadge(d ? d.sensitivity : "MEDIUM")}</div>
    <p>${escapeHTML(summary)}</p>
    <p class="muted-text">Purpose: ${escapeHTML(request.purpose)}</p>
    <p class="muted-text">Requested by: ${escapeHTML(request.requesting_subsystem)}${request.resource ? ` · Scope: ${escapeHTML(request.resource)}` : ""}</p>
    <div class="topbar-actions">
      <button class="ghost small" data-permission-allow="${escapeHTML(request.request_id)}">Allow</button>
      <button class="ghost small" data-permission-deny="${escapeHTML(request.request_id)}">Not now</button>
    </div>
    <details><summary>What will CSLR do?</summary>${renderPermissionExplanation(d)}</details>
  </article>`;
}

export function renderPendingPermissionRequests(requests) {
  const pending = (requests || []).filter((r) => r.state === "PENDING");
  if (!pending.length)
    return '<p class="muted-text">No pending permission requests.</p>';
  return pending.map(renderPendingPermissionRequest).join("");
}

export function permissionGrantStateClass(state) {
  return { ACTIVE: "ok", REVOKED: "muted", EXPIRED: "muted" }[state] || "muted";
}
export const permissionGrantBadge = (state) =>
  `<span class="badge ${permissionGrantStateClass(state)}">${escapeHTML(state)}</span>`;

export function renderActivePermissionGrants(grants) {
  const active = (grants || []).filter((g) => g.state === "ACTIVE");
  if (!active.length) return '<p class="muted-text">No active permissions.</p>';
  return active
    .map(
      (g) =>
        `<div class="scope"><span>${escapeHTML(g.definition ? g.definition.user_title : g.permission_key)}${g.resource ? ` (${escapeHTML(g.resource)})` : ""}<br><small class="muted-text">Granted ${escapeHTML(g.granted_at)}</small></span>${permissionGrantBadge(g.state)}<button class="ghost small" data-permission-revoke="${escapeHTML(g.grant_id)}">Revoke</button></div>`,
    )
    .join("");
}

export function renderPermissionHistory(requests, grants) {
  const denied = (requests || []).filter((r) => r.state === "DENIED");
  const inactive = (grants || []).filter(
    (g) => g.state === "REVOKED" || g.state === "EXPIRED",
  );
  if (!denied.length && !inactive.length) {
    return '<p class="muted-text">No denied or revoked permissions.</p>';
  }
  const deniedHtml = denied
    .map(
      (r) =>
        `<div class="scope"><span>${escapeHTML(r.definition ? r.definition.user_title : r.permission_key)}<br><small class="muted-text">Denied ${escapeHTML(r.decided_at)}</small></span>${badge("DENIED")}</div>`,
    )
    .join("");
  const inactiveHtml = inactive
    .map(
      (g) =>
        `<div class="scope"><span>${escapeHTML(g.definition ? g.definition.user_title : g.permission_key)}<br><small class="muted-text">${g.state === "REVOKED" ? `Revoked ${escapeHTML(g.revoked_at)}` : `Expired ${escapeHTML(g.expiry)}`}</small></span>${permissionGrantBadge(g.state)}</div>`,
    )
    .join("");
  return deniedHtml + inactiveHtml;
}
