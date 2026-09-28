import { acceptSessionToken, api, clearSessionToken, ApiError } from "./api.js";
import { initializeQueryForm } from "./query-form.js";
import { createSeenTracker, renderResults } from "./results.js";
import { diagnosticText, renderHistory, renderLogs } from "./logs.js";
import { initialState, reduceState } from "./state.js";

const PAGE_SIZE = 50;
const TERMINAL = new Set(["completed", "cancelled", "failed"]);
const browserClientId = Array.from(
  crypto.getRandomValues(new Uint8Array(16)),
  (value) => value.toString(16).padStart(2, "0"),
).join("");
let state = initialState;
let queryForm = null;
let categories = [];
let preferences = { proxy_mode: "system", proxy_configured: false, proxy_display: "", open_marks_read: false };
let toastTimer = null;
let heartbeatTimer = null;
let activePoll = null;
let confirmAction = null;

const $ = (selector) => document.querySelector(selector);
const byId = (id) => document.getElementById(id);
const seenTracker = createSeenTracker(
  async (paperIds) => {
    const jobId = state.resultJobId || state.jobId;
    if (jobId && paperIds.length) await api.seen(jobId, paperIds);
  },
  (error) => showToast(errorMessage(error)),
);

function dispatch(action) {
  state = reduceState(state, action);
  render();
}

function showToast(message) {
  const node = byId("toast");
  node.textContent = message;
  node.hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => { node.hidden = true; }, 3200);
}

function errorMessage(error) {
  if (error instanceof ApiError) return error.message;
  if (error?.message_zh) return error.message_zh;
  return error?.message || "操作未能完成，请稍后重试。";
}

function render() {
  const status = state.jobStatus;
  const running = state.mode === "running";
  const statusSection = byId("job-status");
  statusSection.hidden = !running && !state.error && !state.staleCache && status?.phase !== "cancelled";
  byId("cancel-search").hidden = !running;
  byId("search-submit").disabled = running || !sessionReady;
  byId("search-submit").lastChild.textContent = running ? "查询中…" : "开始筛选";
  byId("read-filter").disabled = running;
  document.querySelectorAll("#search-form input, #search-form select, #query-reset").forEach((control) => {
    control.disabled = running;
  });
  byId("continue-search").hidden = !(status?.phase === "completed" && state.truncated && Number(state.request?.result_cap || 0) < 9500);
  byId("export-open").disabled = !state.resultJobId || !["completed", "cancelled"].includes(state.resultStatus?.phase);
  byId("previous-results-note").hidden = !state.previousResults;

  if (statusSection.hidden) {
    byId("job-error").hidden = true;
    byId("stale-confirmation").hidden = true;
  } else {
    const label = {
      queued: "正在准备查询", fetching: "正在获取论文", post_processing: "正在整理结果",
      cancelling: "正在取消查询", completed: "查询完成", cancelled: "查询已取消", failed: "查询未能完成",
    };
    byId("job-status-label").textContent = label[status?.phase] || (state.error ? "查询未能完成" : "准备查询");
    const fetched = Number(status?.fetched || 0);
    const target = status?.target;
    byId("job-progress-label").textContent = target
      ? `已处理 ${fetched.toLocaleString("zh-CN")} / ${Number(target).toLocaleString("zh-CN")}`
      : `${fetched.toLocaleString("zh-CN")} 篇已收录 · 正在等待总量信息`;
    const progress = byId("job-progress");
    progress.hidden = !running;
    if (target > 0) progress.value = Math.min(100, fetched / target * 100);
    else progress.removeAttribute("value");
    progress.setAttribute("aria-valuetext", target ? `${Math.round(fetched / target * 100)}%` : "正在查询");

    const errorNode = byId("job-error");
    errorNode.textContent = state.error ? errorMessage(state.error) : "";
    errorNode.hidden = !state.error;
    const stale = state.staleCache;
    const staleNode = byId("stale-confirmation");
    staleNode.hidden = !stale;
    if (stale) {
      byId("stale-description").textContent = ` · 共 ${stale.result_count} 篇，缓存生成于 ${formatTimestamp(stale.created_at)}。`;
    }
  }

  const count = byId("result-count");
  if (!state.jobId && !state.items.length) count.textContent = "尚未查询";
  else count.textContent = `${state.total.toLocaleString("zh-CN")} 篇${state.partial ? " · 部分结果" : ""}${state.truncated ? " · 已达当前上限" : ""}`;
  const sourceNote = state.cacheStatus === "stale" ? " · 使用了你确认的过期缓存" : "";
  byId("results-note").textContent = state.jobId
    ? `第 ${state.pages ? state.page + 1 : 0} / ${state.pages} 页${sourceNote} · 浏览不会自动标记为已读。`
    : `结果会按提交时间排序${sourceNote}，浏览不会自动标记为已读。`;
  byId("empty-state").hidden = state.items.length > 0 || state.mode === "running";
  byId("pagination").hidden = state.pages <= 1;
  byId("page-label").textContent = state.pages ? `${state.page + 1} / ${state.pages}` : "";
  byId("page-prev").disabled = state.page <= 0;
  byId("page-next").disabled = state.page + 1 >= state.pages;
  if (state.mode !== "running" || !state.jobId) {
    renderResults(byId("results-list"), state.items, {
      setRead: setPaperRead,
      toast: showToast,
      tracker: seenTracker,
      opened: (paper) => { if (preferences.open_marks_read && !paper.is_read) void setPaperRead(paper, true); },
    });
  }
}

function formatTimestamp(value) {
  if (!value) return "未知时间";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "未知时间" : new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

let sessionReady = false;

function startHeartbeat() {
  window.clearInterval(heartbeatTimer);
  heartbeatTimer = window.setInterval(() => {
    void api.heartbeat(browserClientId).catch(() => {});
  }, 15000);
}

async function loadResultPage(page = 0, jobId = state.resultJobId || state.jobId) {
  if (!jobId) return;
  const resultStatus = jobId === state.jobId ? state.jobStatus : state.resultStatus;
  const result = await api.results(jobId, { offset: page * PAGE_SIZE, limit: PAGE_SIZE, readFilter: state.readFilter });
  dispatch({
    type: "RESULTS_LOADED", items: result.items, total: result.total, page, jobId,
    jobStatus: resultStatus,
    pages: Math.ceil(result.total / PAGE_SIZE), partial: result.partial, truncated: result.truncated,
    readFilter: state.readFilter,
  });
}

async function pollJob(jobId, pollAfter = 500) {
  if (activePoll) activePoll.abort = true;
  const poll = { abort: false };
  activePoll = poll;
  while (!poll.abort) {
    await new Promise((resolve) => window.setTimeout(resolve, pollAfter));
    if (poll.abort) break;
    let response;
    try {
      response = await api.job(jobId);
    } catch (error) {
      dispatch({ type: "REQUEST_FAILED", error });
      break;
    }
    const job = response.job;
    dispatch({ type: "JOB_STATUS", status: job.status, cacheStatus: job.cache_status });
    if (!TERMINAL.has(job.status.phase)) continue;
    if (job.status.phase === "failed") {
      dispatch({ type: "JOB_FAILED", status: job.status, error: response.error, staleCache: response.stale_cache });
      break;
    }
    if (job.status.phase === "cancelled") dispatch({ type: "JOB_CANCELLED", status: job.status });
    else dispatch({ type: "JOB_COMPLETED", status: job.status });
    try { await loadResultPage(0, jobId); }
    catch (error) { showToast(errorMessage(error)); }
    break;
  }
  if (activePoll === poll) activePoll = null;
}

async function beginSearch(request, { allowStale = false } = {}) {
  const nextRequest = { ...request, read_filter: request.read_filter || state.readFilter, allow_stale_cache: allowStale };
  dispatch({ type: "SEARCH_STARTED", request: nextRequest });
  try {
    const accepted = await api.createJob(nextRequest);
    dispatch({ type: "JOB_ACCEPTED", jobId: accepted.job_id, status: accepted.status, cacheStatus: accepted.cache_status });
    void pollJob(accepted.job_id, accepted.poll_after_ms || 500);
  } catch (error) {
    dispatch({ type: "REQUEST_FAILED", error });
  }
}

async function setPaperRead(paper, read) {
  try {
    await api.setRead(paper.canonical_id, read);
    dispatch({ type: "READ_UPDATED", canonicalId: paper.canonical_id, read });
    if (state.readFilter === "unread" || state.readFilter === "new") await loadResultPage(state.page);
  } catch (error) {
    showToast(errorMessage(error));
  }
}

function openConfirm({ title, copy, acceptLabel = "确认", danger = false, action }) {
  byId("confirm-title").textContent = title;
  byId("confirm-copy").textContent = copy;
  byId("confirm-accept").textContent = acceptLabel;
  byId("confirm-accept").classList.toggle("button-danger", danger);
  confirmAction = action;
  byId("confirm-dialog").showModal();
}

function closeDialog(id) {
  const dialog = byId(id);
  if (dialog?.open) dialog.close();
}

async function loadHistory() {
  const response = await api.history({ limit: 50 });
  renderHistory(byId("history-list"), response.items, {
    onReplay: (request) => {
      queryForm.setRequest(request);
      closeDialog("history-dialog");
      window.scrollTo({ top: 0, behavior: "smooth" });
      showToast("已恢复历史查询条件。点击“开始筛选”重新查询。");
    },
    onError: (error) => showToast(errorMessage(error)),
  });
}

async function loadLogs() {
  const response = await api.logs({ limit: 50 });
  renderLogs(byId("logs-list"), response.items);
}

async function loadSettings() {
  preferences = await api.settings();
  byId("proxy-mode").value = preferences.proxy_mode;
  byId("proxy-url-row").hidden = preferences.proxy_mode !== "custom";
  byId("proxy-url").value = "";
  byId("proxy-url").placeholder = preferences.proxy_configured
    ? `已保存：${preferences.proxy_display}（留空则保持不变）`
    : "http://用户名:密码@主机:端口";
  byId("open-marks-read").checked = preferences.open_marks_read;
}

async function exportCurrent() {
  const exportJobId = state.resultJobId;
  if (!exportJobId) return;
  const button = byId("export-submit");
  button.disabled = true;
  byId("export-error").hidden = true;
  try {
    let paperIds = null;
    if (state.readFilter !== "all") {
      paperIds = [];
      let offset = 0;
      let total = 0;
      do {
        const page = await api.results(exportJobId, { offset, limit: 100, readFilter: state.readFilter });
        paperIds.push(...page.items.map((paper) => paper.canonical_id));
        total = page.total;
        offset += page.items.length;
        if (!page.items.length) break;
      } while (offset < total);
    }
    const result = await api.export({
      jobId: exportJobId,
      format: byId("export-format").value,
      paperIds,
      includeState: byId("export-state").checked,
      confirmPartial: !(state.partial || state.resultStatus?.phase === "cancelled") || byId("confirm-partial").checked,
    });
    const url = URL.createObjectURL(result.blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = result.filename;
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    closeDialog("export-dialog");
    showToast("文件已生成并开始下载。");
  } catch (error) {
    const node = byId("export-error");
    node.textContent = errorMessage(error);
    node.hidden = false;
    if (error?.code === "EXPORT_PARTIAL_CONFIRMATION_REQUIRED") byId("confirm-partial-row").hidden = false;
  } finally {
    button.disabled = false;
  }
}

function bindEvents() {
  byId("search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    try {
      queryForm.hideError();
      void beginSearch(queryForm.getRequest());
    } catch (error) { queryForm.showError(errorMessage(error)); }
  });
  byId("query-reset").addEventListener("click", () => queryForm.reset());
  byId("cancel-search").addEventListener("click", async () => {
    if (!state.jobId) return;
    try { await api.cancelJob(state.jobId); } catch (error) { showToast(errorMessage(error)); }
  });
  byId("use-stale").addEventListener("click", () => {
    if (state.request) void beginSearch(state.request, { allowStale: true });
  });
  byId("read-filter").addEventListener("change", async (event) => {
    dispatch({ type: "FILTER_CHANGED", readFilter: event.target.value });
    if (state.resultJobId && ["completed", "cancelled"].includes(state.resultStatus?.phase)) {
      try { await loadResultPage(0); } catch (error) { showToast(errorMessage(error)); }
    }
  });
  byId("page-prev").addEventListener("click", () => void loadResultPage(Math.max(0, state.page - 1)));
  byId("page-next").addEventListener("click", () => void loadResultPage(Math.min(state.pages - 1, state.page + 1)));
  byId("continue-search").addEventListener("click", async () => {
    if (!state.jobId) return;
    try {
      const nextRequest = {
        ...state.request,
        result_cap: Math.min(Number(state.request?.result_cap || 2000) + 2000, 9500),
        read_filter: state.readFilter,
      };
      const accepted = await api.continueJob(state.jobId, 2000);
      dispatch({ type: "SEARCH_STARTED", request: nextRequest });
      dispatch({ type: "JOB_ACCEPTED", jobId: accepted.job_id, status: accepted.status, cacheStatus: accepted.cache_status });
      void pollJob(accepted.job_id, accepted.poll_after_ms || 500);
    } catch (error) { showToast(errorMessage(error)); }
  });
  byId("export-open").addEventListener("click", () => {
    byId("confirm-partial-row").hidden = !(state.partial || state.resultStatus?.phase === "cancelled");
    byId("confirm-partial").checked = false;
    byId("export-error").hidden = true;
    byId("export-dialog").showModal();
  });
  byId("export-submit").addEventListener("click", () => void exportCurrent());
  byId("history-open").addEventListener("click", async () => {
    try { await loadHistory(); byId("history-dialog").showModal(); }
    catch (error) { showToast(errorMessage(error)); }
  });
  byId("logs-open").addEventListener("click", async () => {
    try { await loadLogs(); byId("logs-dialog").showModal(); }
    catch (error) { showToast(errorMessage(error)); }
  });
  byId("logs-copy").addEventListener("click", async () => {
    try {
      const { items } = await api.logs({ limit: 100 });
      await navigator.clipboard.writeText(diagnosticText(items));
      showToast("脱敏诊断信息已复制。");
    } catch (error) { showToast(errorMessage(error)); }
  });
  byId("settings-open").addEventListener("click", async () => {
    try { await loadSettings(); byId("settings-dialog").showModal(); }
    catch (error) { showToast(errorMessage(error)); }
  });
  byId("about-open").addEventListener("click", () => byId("about-dialog").showModal());
  byId("proxy-mode").addEventListener("change", (event) => { byId("proxy-url-row").hidden = event.target.value !== "custom"; });
  byId("settings-save").addEventListener("click", async () => {
    const errorNode = byId("settings-error");
    errorNode.hidden = true;
    try {
      const saved = await api.saveSettings({
        proxy_mode: byId("proxy-mode").value,
        proxy_url: byId("proxy-url").value || null,
        open_marks_read: byId("open-marks-read").checked,
      });
      preferences = saved;
      byId("proxy-url").value = "";
      closeDialog("settings-dialog");
      showToast("设置已保存在本机。");
    } catch (error) { errorNode.textContent = errorMessage(error); errorNode.hidden = false; }
  });
  byId("cache-clear").addEventListener("click", () => openConfirm({
    title: "清理本地缓存？", copy: "这会删除论文元数据缓存，但保留已读状态和查询历史。此操作无法撤销。",
    acceptLabel: "清理缓存", danger: true,
    action: async () => {
      const report = await api.clearCache();
      showToast(`已清理 ${report.removed_entries} 条缓存。`);
    },
  }));
  byId("app-exit").addEventListener("click", () => openConfirm({
    title: "退出 arXiv Filter？", copy: "本地服务将关闭。再次打开应用即可继续使用已保存的数据。",
    acceptLabel: "退出应用", action: async () => {
      const active = state.mode === "running";
      await api.shutdown(active);
      clearSessionToken();
      sessionReady = false;
      byId("connection-status").textContent = "本地服务已关闭";
      render();
    },
  }));
  byId("confirm-accept").addEventListener("click", async () => {
    const action = confirmAction;
    confirmAction = null;
    closeDialog("confirm-dialog");
    try { await action?.(); } catch (error) { showToast(errorMessage(error)); }
  });
  byId("confirm-cancel").addEventListener("click", () => closeDialog("confirm-dialog"));
  document.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.addEventListener("click", () => closeDialog(button.dataset.closeDialog));
  });
}

async function bootstrap() {
  bindEvents();
  sessionReady = acceptSessionToken();
  if (!sessionReady) {
    byId("connection-status").textContent = "请从桌面启动应用";
    byId("search-submit").disabled = true;
    showToast("未找到本地会话凭据，请从桌面应用启动。 ");
    return;
  }
  try {
    await api.health();
    const response = await api.categories();
    categories = response.groups;
    queryForm = initializeQueryForm(categories);
    byId("connection-status").textContent = "本地服务已连接";
    byId("connection-status").classList.add("is-connected");
    byId("connection-status").classList.remove("is-offline");
    await api.heartbeat(browserClientId);
    startHeartbeat();
    render();
  } catch (error) {
    byId("connection-status").textContent = "本地服务暂不可用";
    byId("connection-status").classList.remove("is-connected");
    byId("connection-status").classList.add("is-offline");
    sessionReady = false;
    render();
    showToast(errorMessage(error));
  }
}

window.addEventListener("pagehide", (event) => {
  window.clearInterval(heartbeatTimer);
  if (activePoll) activePoll.abort = true;
  seenTracker.disconnect();
  // Keep a BFCache page leased while it is temporarily hidden; closing or
  // navigating away from the app sends a best-effort final heartbeat.
  if (sessionReady && !event.persisted) {
    void api.heartbeat(browserClientId, false, { keepalive: true }).catch(() => {});
  }
});

window.addEventListener("pageshow", (event) => {
  if (event.persisted && sessionReady) {
    void api.heartbeat(browserClientId).catch(() => {});
    startHeartbeat();
  }
});

void bootstrap();
