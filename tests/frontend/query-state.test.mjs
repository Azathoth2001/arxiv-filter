import test from "node:test";
import assert from "node:assert/strict";
import { dateRangeForPreset, utcDateString } from "../../src/arxiv_filter/static/js/query-form.js";
import { initialState, reduceState } from "../../src/arxiv_filter/static/js/state.js";

test("UTC date shortcuts are inclusive, calendar-based ranges", () => {
  const now = new Date("2026-03-01T01:30:00+08:00");
  assert.deepEqual(dateRangeForPreset("3", now), { start_date: "2026-02-26", end_date: "2026-02-28" });
  assert.deepEqual(dateRangeForPreset("7", now), { start_date: "2026-02-22", end_date: "2026-02-28" });
  assert.deepEqual(dateRangeForPreset("month", now), { start_date: "2026-02-01", end_date: "2026-02-28" });
  assert.equal(utcDateString(new Date("2026-12-31T23:30:00-02:00")), "2027-01-01");
});

test("failed second search retains the prior exportable result identity", () => {
  const completed = { phase: "completed" };
  let state = reduceState(initialState, { type: "SEARCH_STARTED", request: { group_id: "cs" } });
  state = reduceState(state, { type: "JOB_ACCEPTED", jobId: "job-old", status: completed });
  state = reduceState(state, {
    type: "RESULTS_LOADED", items: [{ canonical_id: "2601.00001" }], total: 1,
    page: 0, pages: 1, partial: false, truncated: false, readFilter: "all",
    jobId: "job-old", jobStatus: completed,
  });
  state = reduceState(state, { type: "SEARCH_STARTED", request: { group_id: "math" } });
  state = reduceState(state, { type: "JOB_ACCEPTED", jobId: "job-new", status: { phase: "queued" } });
  state = reduceState(state, { type: "JOB_FAILED", status: { phase: "failed" }, error: { message_zh: "failed" } });
  assert.equal(state.jobId, "job-new");
  assert.equal(state.resultJobId, "job-old");
  assert.equal(state.resultStatus.phase, "completed");
  assert.equal(state.items[0].canonical_id, "2601.00001");
  assert.equal(state.previousResults, true);
  state = reduceState(state, {
    type: "RESULTS_LOADED", items: [{ canonical_id: "2601.00001" }], total: 1,
    page: 0, pages: 1, partial: false, truncated: false, readFilter: "unread",
    jobId: "job-old", jobStatus: state.resultStatus,
  });
  assert.equal(state.resultStatus.phase, "completed");
  assert.equal(state.resultJobId, "job-old");
  assert.equal(state.previousResults, true);
});

test("new terminal result replaces prior result identity and partial metadata", () => {
  let state = {
    ...initialState,
    mode: "running",
    jobId: "job-current",
    resultJobId: "job-previous",
    previousResults: true,
    jobStatus: { phase: "completed" },
  };
  state = reduceState(state, {
    type: "RESULTS_LOADED", items: [], total: 0, page: 0, pages: 0,
    partial: true, truncated: false, readFilter: "all", jobId: "job-current",
    jobStatus: { phase: "cancelled" },
  });
  assert.equal(state.resultJobId, "job-current");
  assert.equal(state.resultStatus.phase, "cancelled");
  assert.equal(state.partial, true);
  assert.equal(state.previousResults, false);
});
