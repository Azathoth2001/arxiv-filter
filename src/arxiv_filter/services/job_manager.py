"""异步搜索任务管理（T4.1）。

同一缓存键的并发请求共享一个 Job；不同查询不排队，也不并发访问上游，
而是以稳定的 ``JOB_LIMIT_REACHED`` (409) 拒绝，符合本地 MVP 的单活动任务契约。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from arxiv_filter.arxiv.pagination import SAFE_OFFSET_WINDOW, PageProgress
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.job import SearchJob, SearchPhase, can_transition
from arxiv_filter.models.paper import PaperView
from arxiv_filter.models.query import MAX_RESULT_CAP, SearchRequest
from arxiv_filter.services.search_service import SearchExecution, SearchService
from arxiv_filter.storage.read_repo import ReadRepository, ReadState

MAX_RETAINED_TERMINAL_JOBS = 5

_PHASE_MESSAGES: dict[SearchPhase, str] = {
    SearchPhase.QUEUED: "任务正在启动。",
    SearchPhase.FETCHING: "正在获取论文，请稍候。",
    SearchPhase.POST_PROCESSING: "正在整理查询结果。",
    SearchPhase.COMPLETED: "查询完成。",
    SearchPhase.CANCELLING: "正在取消查询。",
    SearchPhase.CANCELLED: "查询已取消。",
    SearchPhase.FAILED: "查询未能完成。",
}


@dataclass(slots=True)
class _EventCancelToken:
    """给 SearchService 使用的同步可检查取消标记。"""

    _event: asyncio.Event = field(default_factory=asyncio.Event)

    def is_set(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.is_set():
            raise asyncio.CancelledError


@dataclass(slots=True)
class _JobRecord:
    request: SearchRequest
    coalesce_key: tuple[str, bool]
    job: SearchJob
    progress: PageProgress
    cancel_token: _EventCancelToken
    started_at: float
    read_state_snapshot: dict[str, ReadState]
    task: asyncio.Task[None] | None = None
    execution: SearchExecution | None = None
    error: Exception | None = None
    finished_elapsed_ms: int | None = None


class JobManager:
    """管理 SearchService 的任务生命周期、合并、取消与进度快照。

    ``create`` 对相同查询 cache key 与 stale-cache 授权进行合并。
    ``read_filter`` 是展示层选项，不影响论文集合，因此不阻止合并。
    不同的活动查询会收到 ``JOB_LIMIT_REACHED``，不会创建排队任务；最多保留最近
    ``max_retained_jobs`` 个终态 job，避免连续查询时无界持有结果快照。
    """

    def __init__(
        self,
        search_service: SearchService,
        *,
        read_repository: ReadRepository | None = None,
        max_retained_jobs: int = MAX_RETAINED_TERMINAL_JOBS,
    ) -> None:
        if max_retained_jobs < 1:
            raise ValueError("max_retained_jobs 必须至少为 1")
        self._search_service = search_service
        self._read_repository = read_repository
        self._max_retained_jobs = max_retained_jobs
        self._lock = asyncio.Lock()
        self._records: dict[UUID, _JobRecord] = {}
        self._inflight: dict[tuple[str, bool], UUID] = {}
        self._active_job_id: UUID | None = None

    def bind_read_repository(self, read_repository: ReadRepository) -> None:
        """应用装配时绑定 read-state 仓储；任务创建后不可更换快照来源。"""
        if self._records and self._read_repository is not read_repository:
            raise RuntimeError("JobManager 已创建任务，不能替换 read-state 仓储")
        self._read_repository = read_repository

    async def create(self, request: SearchRequest) -> SearchJob:
        """创建任务；相同活动查询复用原 job_id。"""
        cache_key = self._search_service.cache_key_for(request)
        read_snapshot = (
            await self._read_repository.snapshot_state()
            if self._read_repository is not None
            else {}
        )
        # Consent changes whether old papers may be revealed, so it is part of
        # in-flight identity even though it is deliberately absent from cache key.
        coalesce_key = (cache_key, request.allow_stale_cache)

        async with self._lock:
            existing_id = self._inflight.get(coalesce_key)
            if existing_id is not None:
                existing = self._records.get(existing_id)
                if existing is not None and existing.job.is_running:
                    return self._snapshot(existing)
                self._inflight.pop(coalesce_key, None)

            if self._active_job_id is not None:
                active = self._records.get(self._active_job_id)
                if active is not None and active.job.is_running:
                    raise ArxivFilterError(
                        ErrorCode.JOB_LIMIT_REACHED,
                        details={"active_job_id": str(active.job.job_id)},
                    )
                self._active_job_id = None

            job_id = uuid4()
            record = _JobRecord(
                request=request,
                coalesce_key=coalesce_key,
                job=SearchJob(job_id=job_id),
                progress=PageProgress(),
                cancel_token=_EventCancelToken(),
                started_at=time.monotonic(),
                read_state_snapshot=read_snapshot,
            )
            self._records[job_id] = record
            self._inflight[coalesce_key] = job_id
            self._active_job_id = job_id
            record.task = asyncio.create_task(
                self._run(record), name=f"arxiv-search-{job_id}"
            )
            return self._snapshot(record)

    async def snapshot(self, job_id: UUID | str) -> SearchJob:
        """读取任务快照；运行中进度从 SearchService 的 PageProgress 同步。"""
        record_id = self._parse_job_id(job_id)
        async with self._lock:
            return self._snapshot(self._get_record(record_id))

    async def active_job(self) -> SearchJob | None:
        """返回唯一活动任务；终态或缺省时返回 ``None``。"""
        async with self._lock:
            if self._active_job_id is None:
                return None
            record = self._records.get(self._active_job_id)
            if record is None or not record.job.is_running:
                self._active_job_id = None
                return None
            return self._snapshot(record)

    async def cancel(self, job_id: UUID | str) -> SearchJob:
        """请求取消。对已取消任务重复调用幂等；其它终态返回 409。"""
        record_id = self._parse_job_id(job_id)
        async with self._lock:
            record = self._get_record(record_id)
            phase = record.job.status.phase
            if phase in {SearchPhase.CANCELLED, SearchPhase.CANCELLING}:
                return self._snapshot(record)
            if record.job.is_terminal:
                raise ArxivFilterError(
                    ErrorCode.JOB_ALREADY_FINISHED,
                    details={"phase": phase.value},
                )

            self._transition(record, SearchPhase.CANCELLING)
            record.cancel_token.cancel()
            return self._snapshot(record)

    async def continue_search(
        self,
        job_id: UUID | str,
        additional_results: int,
    ) -> SearchJob:
        """以新的 job 扩大截断查询的目标；单次增量最多 2000，累计最多 9500。"""
        if not 1 <= additional_results <= MAX_RESULT_CAP:
            raise ArxivFilterError(
                ErrorCode.RESULT_CAP_INVALID,
                details={"additional_results": additional_results},
            )
        record_id = self._parse_job_id(job_id)
        async with self._lock:
            record = self._get_record(record_id)
            phase = record.job.status.phase
            if not record.job.is_terminal:
                raise ArxivFilterError(
                    ErrorCode.JOB_NOT_FINISHED,
                    details={"phase": phase.value},
                )
            if phase is not SearchPhase.COMPLETED or not record.job.truncated:
                raise ArxivFilterError(
                    ErrorCode.JOB_INVALID_TRANSITION,
                    details={"phase": phase.value, "reason": "not_truncated"},
                )
            if record.request.result_cap >= SAFE_OFFSET_WINDOW:
                raise ArxivFilterError(
                    ErrorCode.JOB_INVALID_TRANSITION,
                    details={"reason": "safe_result_cap_reached"},
                    message_zh="已达到当前安全抓取上限，请缩小日期范围或分类后重试。",
                )
            next_cap = min(
                record.request.result_cap + additional_results,
                SAFE_OFFSET_WINDOW,
            )
            # Public requests remain capped at 2000. This internal copy is bounded
            # above and is only reachable via the explicit continuation endpoint.
            next_request = record.request.model_copy(update={"result_cap": next_cap})
        return await self.create(next_request)

    async def wait(self, job_id: UUID | str) -> SearchJob:
        """等待后台 coroutine 结束，供服务生命周期与测试使用。"""
        record_id = self._parse_job_id(job_id)
        async with self._lock:
            record = self._get_record(record_id)
            task = record.task
        if task is not None:
            await task
        return await self.snapshot(record_id)

    async def get_execution(self, job_id: UUID | str) -> SearchExecution | None:
        """取得终态领域结果；失败时重抛安全错误供 API 层渲染。"""
        record_id = self._parse_job_id(job_id)
        async with self._lock:
            record = self._get_record(record_id)
            if not record.job.is_terminal:
                raise ArxivFilterError(
                    ErrorCode.JOB_NOT_FINISHED,
                    details={"phase": record.job.status.phase.value},
                )
            if record.execution is not None:
                return record.execution
            if isinstance(record.error, ArxivFilterError):
                raise record.error
            if record.error is not None:
                raise ArxivFilterError(ErrorCode.INTERNAL_ERROR)
            return None

    async def get_request(self, job_id: UUID | str) -> SearchRequest:
        """Return the immutable query context needed by export naming."""
        record_id = self._parse_job_id(job_id)
        async with self._lock:
            return self._get_record(record_id).request

    async def get_error(self, job_id: UUID | str) -> ArxivFilterError | None:
        """取得面向状态轮询的安全错误摘要（若任务以错误结束）。"""
        record_id = self._parse_job_id(job_id)
        async with self._lock:
            record = self._get_record(record_id)
            if record.execution is not None and record.execution.error is not None:
                return record.execution.error
            if isinstance(record.error, ArxivFilterError):
                return record.error
            if record.error is not None:
                return ArxivFilterError(ErrorCode.INTERNAL_ERROR)
            return None

    async def get_paper_views(self, job_id: UUID | str) -> tuple[PaperView, ...]:
        """以 job 创建时的 read-state 快照生成稳定 PaperView。"""
        record_id = self._parse_job_id(job_id)
        async with self._lock:
            record = self._get_record(record_id)
            if not record.job.is_terminal:
                raise ArxivFilterError(
                    ErrorCode.JOB_NOT_FINISHED,
                    details={"phase": record.job.status.phase.value},
                )
            if record.execution is None:
                if isinstance(record.error, ArxivFilterError):
                    raise record.error
                if record.error is not None:
                    raise ArxivFilterError(ErrorCode.INTERNAL_ERROR)
                return ()
            views: list[PaperView] = []
            for paper in record.execution.papers:
                state = record.read_state_snapshot.get(paper.canonical_id)
                views.append(
                    PaperView.from_paper(
                        paper,
                        first_seen_at=state.first_seen_at if state else None,
                        read_at=state.read_at if state else None,
                        is_new=state is None,
                        is_read=state.is_read if state else False,
                    )
                )
            return tuple(views)

    async def _run(self, record: _JobRecord) -> None:
        """后台执行单 job，并把所有退出路径收敛到一个终态。"""
        async with self._lock:
            if record.job.status.phase is SearchPhase.CANCELLING:
                self._finish_cancelled(record)
                self._trim_terminal_records()
                return
            self._transition(record, SearchPhase.FETCHING)

        try:
            execution = await self._search_service.run(
                record.request,
                cancel=record.cancel_token,
                progress=record.progress,
            )
        except asyncio.CancelledError:
            async with self._lock:
                self._finish_cancelled(record)
                self._trim_terminal_records()
        except Exception as exc:  # noqa: BLE001 - persist failure in the job snapshot
            async with self._lock:
                record.error = exc
                if record.job.status.phase is SearchPhase.CANCELLING:
                    record.error = None
                    self._finish_cancelled(record)
                else:
                    self._transition(record, SearchPhase.FAILED)
                    self._release_active(record)
                self._trim_terminal_records()
        else:
            async with self._lock:
                record.execution = execution
                record.job.cache_status = execution.cache_status
                record.job.partial = execution.partial
                record.job.truncated = bool(execution.truncated)
                record.job.warnings = list(execution.warnings)

                if (
                    record.job.status.phase is SearchPhase.CANCELLING
                    or record.cancel_token.is_set()
                ):
                    self._finish_cancelled(record)
                elif execution.partial:
                    self._transition(record, SearchPhase.CANCELLING)
                    self._finish_cancelled(record)
                elif execution.stale_confirmation_required or execution.error is not None:
                    record.error = execution.error
                    self._transition(record, SearchPhase.FAILED)
                    self._release_active(record)
                else:
                    self._transition(record, SearchPhase.POST_PROCESSING)
                    self._transition(record, SearchPhase.COMPLETED)
                    self._release_active(record)
                self._trim_terminal_records()

    def _finish_cancelled(self, record: _JobRecord) -> None:
        """Caller holds ``_lock``; enter and freeze the cancelled terminal state."""
        if record.job.status.phase is not SearchPhase.CANCELLING:
            self._transition(record, SearchPhase.CANCELLING)
        self._transition(record, SearchPhase.CANCELLED)
        self._release_active(record)

    def _transition(self, record: _JobRecord, target: SearchPhase) -> None:
        """Apply only transitions allowed by the domain state machine."""
        current = record.job.status.phase
        if not can_transition(current, target):
            raise ArxivFilterError(
                ErrorCode.JOB_INVALID_TRANSITION,
                details={"from": current.value, "to": target.value},
            )
        elapsed = self._elapsed_ms(record)
        record.job.status = record.job.status.model_copy(
            update={
                "phase": target,
                "fetched": record.progress.fetched,
                "target": record.progress.target,
                "total_reported": record.progress.total_reported,
                "pages": record.progress.pages,
                "retries": record.progress.retries,
                "message_zh": _PHASE_MESSAGES[target],
                "indeterminate": record.progress.indeterminate,
                "elapsed_ms": elapsed,
            }
        )
        if record.job.is_terminal:
            record.finished_elapsed_ms = elapsed

    def _snapshot(self, record: _JobRecord) -> SearchJob:
        status = record.job.status
        if not record.job.is_terminal:
            status = status.model_copy(
                update={
                    "fetched": record.progress.fetched,
                    "target": record.progress.target,
                    "total_reported": record.progress.total_reported,
                    "pages": record.progress.pages,
                    "retries": record.progress.retries,
                    "elapsed_ms": self._elapsed_ms(record),
                    "indeterminate": record.progress.indeterminate,
                }
            )
        elif record.finished_elapsed_ms is not None:
            status = status.model_copy(update={"elapsed_ms": record.finished_elapsed_ms})
        return record.job.model_copy(update={"status": status}, deep=True)

    @staticmethod
    def _parse_job_id(job_id: UUID | str) -> UUID:
        try:
            return job_id if isinstance(job_id, UUID) else UUID(job_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ArxivFilterError(
                ErrorCode.JOB_NOT_FOUND,
                details={"job_id": str(job_id)},
            ) from exc

    def _get_record(self, job_id: UUID) -> _JobRecord:
        try:
            return self._records[job_id]
        except KeyError as exc:
            raise ArxivFilterError(
                ErrorCode.JOB_NOT_FOUND,
                details={"job_id": str(job_id)},
            ) from exc

    def _elapsed_ms(self, record: _JobRecord) -> int:
        return max(0, int((time.monotonic() - record.started_at) * 1000))

    def _release_active(self, record: _JobRecord) -> None:
        if self._active_job_id == record.job.job_id:
            self._active_job_id = None
        if self._inflight.get(record.coalesce_key) == record.job.job_id:
            self._inflight.pop(record.coalesce_key, None)

    def _trim_terminal_records(self) -> None:
        """Bound in-memory results while preserving the newest terminal jobs.

        The caller holds ``_lock``. History and cache remain available after an old
        job record is evicted; only that job ID's in-memory result view expires.
        Running jobs are never candidates for eviction.
        """
        # ``dict`` preserves insertion order. It is a stronger recency signal
        # than ``started_at`` here: monotonic float timestamps can tie for jobs
        # created in the same clock tick, and UUID order is random.
        terminal = [
            record for record in self._records.values() if record.job.is_terminal
        ]
        excess = len(terminal) - self._max_retained_jobs
        for record in terminal[: max(0, excess)]:
            job_id = record.job.job_id
            self._records.pop(job_id, None)
            if self._inflight.get(record.coalesce_key) == job_id:
                self._inflight.pop(record.coalesce_key, None)


__all__ = ["JobManager"]
