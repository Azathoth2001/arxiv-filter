const API_ROOT = "/api/v1";
const TOKEN_KEY = "arxiv-filter.session-token";
let sessionToken = null;

export class ApiError extends Error {
  constructor(payload, status) {
    super(payload?.error?.message_zh || "本地服务暂时无法完成请求。");
    this.name = "ApiError";
    this.code = payload?.error?.code || "API_ERROR";
    this.status = status;
    this.retryable = Boolean(payload?.error?.retryable);
    this.requestId = payload?.error?.request_id || null;
    this.details = payload?.error?.details || {};
  }
}

export function acceptSessionToken() {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const bootstrapToken = fragment.get("token");
  if (bootstrapToken && /^[0-9a-f]{32,}$/i.test(bootstrapToken)) {
    sessionStorage.setItem(TOKEN_KEY, bootstrapToken);
    history.replaceState(null, "", `${location.pathname}${location.search}`);
  }
  sessionToken = sessionStorage.getItem(TOKEN_KEY);
  return Boolean(sessionToken);
}

export function clearSessionToken() {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionToken = null;
}

async function apiRequest(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "application/json");
  if (sessionToken) headers.set("X-App-Token", sessionToken);

  let body = options.body;
  if (body !== undefined && body !== null && typeof body !== "string" && !(body instanceof Blob)) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(body);
  }

  const response = await fetch(`${API_ROOT}${path}`, {
    ...options,
    body,
    headers,
    credentials: "same-origin",
    cache: "no-store",
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const error = new ApiError(payload, response.status);
    if (error.code === "INVALID_TOKEN") clearSessionToken();
    throw error;
  }
  return payload;
}

export const api = {
  health: () => apiRequest("/system/health"),
  categories: () => apiRequest("/categories"),
  createJob: (request) => apiRequest("/search-jobs", { method: "POST", body: request }),
  job: (jobId) => apiRequest(`/search-jobs/${encodeURIComponent(jobId)}`),
  continueJob: (jobId, additionalResults = 2000) => apiRequest(
    `/search-jobs/${encodeURIComponent(jobId)}/continue`,
    { method: "POST", body: { additional_results: additionalResults } },
  ),
  cancelJob: (jobId) => apiRequest(`/search-jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" }),
  results: (jobId, { offset = 0, limit = 50, readFilter = "all" } = {}) => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit), read_filter: readFilter });
    return apiRequest(`/search-jobs/${encodeURIComponent(jobId)}/results?${params}`);
  },
  seen: (jobId, paperIds) => apiRequest("/seen", { method: "POST", body: { job_id: jobId, paper_ids: paperIds } }),
  setRead: (paperId, read) => apiRequest(`/read-state/${encodeURIComponent(paperId)}`, { method: "PUT", body: { read } }),
  history: ({ limit = 50, cursor = null } = {}) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    return apiRequest(`/history?${params}`);
  },
  logs: ({ limit = 50, cursor = null } = {}) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    return apiRequest(`/logs?${params}`);
  },
  clearCache: () => apiRequest("/cache", { method: "DELETE", body: { confirm: true } }),
  settings: () => apiRequest("/system/settings"),
  saveSettings: (settings) => apiRequest("/system/settings", { method: "PUT", body: settings }),
  heartbeat: (clientId, active = true, { keepalive = false } = {}) => apiRequest("/system/heartbeat", {
    method: "POST",
    body: { client_id: clientId, active },
    keepalive,
  }),
  shutdown: (confirmActive = false) => apiRequest("/system/shutdown", {
    method: "POST",
    body: { confirm_active: confirmActive },
  }),
  export: async ({ jobId, format, paperIds = null, includeState = true, confirmPartial = false }) => {
    const headers = new Headers({ "Content-Type": "application/json", Accept: "application/octet-stream" });
    if (sessionToken) headers.set("X-App-Token", sessionToken);
    const response = await fetch(`${API_ROOT}/exports`, {
      method: "POST",
      headers,
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify({
        job_id: jobId,
        format,
        paper_ids: paperIds,
        include_state: includeState,
        confirm_partial: confirmPartial,
      }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      const error = new ApiError(payload, response.status);
      if (error.code === "INVALID_TOKEN") clearSessionToken();
      throw error;
    }
    const disposition = response.headers.get("Content-Disposition") || "";
    const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
    const plainName = disposition.match(/filename="?([^";]+)"?/i)?.[1];
    return {
      blob: await response.blob(),
      filename: encodedName ? decodeURIComponent(encodedName) : (plainName || `arxiv-export.${format}`),
    };
  },
};

export { API_ROOT };
