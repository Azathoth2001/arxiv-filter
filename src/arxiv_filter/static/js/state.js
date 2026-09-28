export const initialState = Object.freeze({
  mode: "idle",
  jobId: null,
  resultJobId: null,
  jobStatus: null,
  resultStatus: null,
  cacheStatus: null,
  request: null,
  items: [],
  total: 0,
  page: 0,
  pages: 0,
  readFilter: "all",
  partial: false,
  truncated: false,
  previousResults: false,
  error: null,
  staleCache: null,
});

export function reduceState(state, action) {
  switch (action.type) {
    case "SEARCH_STARTED":
      return {
        ...state,
        mode: "running",
        jobId: null,
        jobStatus: null,
        request: action.request,
        error: null,
        staleCache: null,
        previousResults: state.items.length > 0,
      };
    case "JOB_ACCEPTED":
      return { ...state, jobId: action.jobId, jobStatus: action.status, cacheStatus: action.cacheStatus || null };
    case "JOB_STATUS":
      return { ...state, jobStatus: action.status, cacheStatus: action.cacheStatus || state.cacheStatus };
    case "RESULTS_LOADED":
      return {
        ...state,
        mode: action.mode || state.mode,
        items: action.items,
        total: action.total,
        page: action.page,
        pages: action.pages,
        partial: action.partial,
        truncated: action.truncated,
        readFilter: action.readFilter,
        resultJobId: action.jobId || state.jobId || state.resultJobId,
        resultStatus: action.jobStatus || state.jobStatus || state.resultStatus,
        previousResults: action.jobId && action.jobId !== state.jobId
          ? state.previousResults
          : false,
      };
    case "JOB_COMPLETED":
      return {
        ...state,
        mode: "completed",
        jobStatus: action.status,
        error: null,
        staleCache: null,
        partial: action.status.partial,
        truncated: action.status.truncated,
        previousResults: false,
      };
    case "JOB_CANCELLED":
      return {
        ...state,
        mode: "cancelled",
        jobStatus: action.status,
        partial: action.status.partial,
        truncated: action.status.truncated,
        previousResults: false,
      };
    case "JOB_FAILED":
      return {
        ...state,
        mode: "error",
        jobStatus: action.status,
        error: action.error,
        staleCache: action.staleCache,
      };
    case "REQUEST_FAILED":
      return { ...state, mode: "error", error: action.error, previousResults: state.items.length > 0 };
    case "FILTER_CHANGED":
      return { ...state, readFilter: action.readFilter, page: 0 };
    case "READ_UPDATED":
      return {
        ...state,
        items: state.items.map((paper) => paper.canonical_id === action.canonicalId
          ? { ...paper, is_read: action.read }
          : paper),
      };
    case "PAGE_CHANGED":
      return { ...state, page: action.page };
    case "CLEAR_RESULTS":
      return { ...initialState, readFilter: state.readFilter };
    default:
      return state;
  }
}
