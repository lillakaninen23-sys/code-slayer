/** Same-origin HTTP only. Mutations are never retried automatically. */
export class APIError extends Error {
  constructor(code, message, retryable = false) {
    super(message);
    this.code = code;
    this.retryable = retryable;
  }
}

export function createAPI(fetcher = globalThis.fetch.bind(globalThis)) {
  async function request(path, data) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), data === undefined ? 10000 : 120000);
    try {
      const response = await fetcher(`/api${path}`, {
        method: data === undefined ? "GET" : "POST",
        headers: { Accept: "application/json", ...(data === undefined ? {} : { "Content-Type": "application/json" }) },
        ...(data === undefined ? {} : { body: JSON.stringify(data) }),
        signal: controller.signal,
        credentials: "same-origin",
        cache: "no-store",
      });
      let value;
      try { value = await response.json(); }
      catch { throw new APIError("invalid_response", "Backend returned an unreadable response."); }
      if (!response.ok) {
        throw new APIError(value?.error?.code || "http_error", value?.error?.message || "Request failed.", value?.error?.retryable === true);
      }
      if (!value || typeof value !== "object" || Array.isArray(value)) {
        throw new APIError("invalid_response", "Backend returned an invalid response.");
      }
      return value;
    } catch (error) {
      if (error instanceof APIError) throw error;
      throw new APIError("disconnected", data === undefined
        ? "Backend unavailable. Check that the API server is running."
        : "Connection lost during action. Refresh durable run state before trying again.");
    } finally { clearTimeout(timer); }
  }
  const id = encodeURIComponent;
  return {
    health: () => request("/health"), project: () => request("/project"),
    runs: (offset = 0) => request(`/runs?limit=100&offset=${offset}`),
    run: (runId) => request(`/runs/${id(runId)}`),
    start: (prompt, workerId, role) => request("/runs", { prompt, worker_id: workerId, role, capability_profile: "read_only" }),
    resume: (runId) => request(`/runs/${id(runId)}/resume`, {}),
    resolve: (runId, ambiguityId, answer, resolutionKind) => request(`/runs/${id(runId)}/resolutions`, {
      ambiguity_id: ambiguityId, answer, resolution_kind: resolutionKind,
    }),
    workers: () => request("/workers"),
    trust: (workerId) => request(`/workers/${id(workerId)}/trust`),
    conformance: (workerId) => request(`/workers/${id(workerId)}/conformance`),
    audit: (runId) => request(`/runs/${id(runId)}/audit?limit=100`),
    intelligenceStatus: () => request("/intelligence/status"),
    intelligenceRefresh: () => request("/intelligence/refresh", {}),
    intelligenceQuery: (text) => request("/intelligence/query", { text }),
    intelligenceContextPack: (text) => request("/intelligence/context-pack", { text }),
    plans: (offset = 0) => request(`/plans?limit=100&offset=${offset}`),
    plan: (planId) => request(`/plans/${id(planId)}`),
    createPlan: (text) => request("/plans", { request: text }),
    resumePlan: (planId) => request(`/plans/${id(planId)}/resume`, {}),
    replanPlan: (planId) => request(`/plans/${id(planId)}/replan`, {}),
    resolvePlan: (planId, ambiguityId, answer, resolutionKind) => request(`/plans/${id(planId)}/resolutions`, {
      ambiguity_id: ambiguityId, answer, resolution_kind: resolutionKind,
    }),
    planningJobs: (offset = 0) => request(`/planning-jobs?limit=100&offset=${offset}`),
    planningJob: (jobId) => request(`/planning-jobs/${id(jobId)}`),
    permissionDefinitions: () => request("/permissions"),
    permissionRequests: (offset = 0) => request(`/permissions/requests?limit=100&offset=${offset}`),
    permissionRequest: (requestId) => request(`/permissions/requests/${id(requestId)}`),
    decidePermission: (requestId, decision) => request(`/permissions/requests/${id(requestId)}/decision`, { decision }),
    permissionGrants: (offset = 0) => request(`/permissions/grants?limit=100&offset=${offset}`),
    revokePermission: (grantId) => request(`/permissions/grants/${id(grantId)}/revoke`, {}),
  };
}
