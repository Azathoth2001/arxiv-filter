"""T4.1 JobManager：合并、唯一活动任务、状态迁移与取消。"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from arxiv_filter.arxiv.provider import CancelToken
from arxiv_filter.arxiv.query_builder import build_query
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.job import SearchPhase
from arxiv_filter.models.query import SearchRequest
from arxiv_filter.services.job_manager import JobManager
from arxiv_filter.services.search_service import SearchExecution


def make_request(
    *,
    keywords: str = '"language model"',
    allow_stale: bool = False,
    read_filter: str = "all",
) -> SearchRequest:
    return SearchRequest(
        group_id="cs",
        category_id="cs.CL",
        start_date=date(2026, 9, 17),
        end_date=date(2026, 9, 24),
        keywords=keywords,
        allow_stale_cache=allow_stale,
        read_filter=read_filter,  # type: ignore[arg-type]
    )


class BlockingSearchService:
    """可控的离线 SearchService 替身。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.cancel_tokens: list[CancelToken | None] = []

    def cache_key_for(self, request: SearchRequest) -> str:
        return build_query(request).cache_key

    async def run(self, request, *, cancel=None, progress=None) -> SearchExecution:
        self.calls += 1
        self.cancel_tokens.append(cancel)
        if progress is not None:
            progress.fetched = 2
            progress.target = 5
            progress.total_reported = 5
            progress.pages = 1
            progress.retries = 1
        self.started.set()
        await self.release.wait()
        if cancel is not None:
            cancel.raise_if_cancelled()
        return SearchExecution(
            query=build_query(request),
            papers=(),
            cache_status="miss",
            history_id="history-1",
            total_reported=5,
            pages=1,
        )


class FailingSearchService(BlockingSearchService):
    async def run(self, request, *, cancel=None, progress=None) -> SearchExecution:
        self.calls += 1
        raise ArxivFilterError(ErrorCode.ARXIV_NOT_ACCEPTABLE)


class StaleConfirmationSearchService(BlockingSearchService):
    async def run(self, request, *, cancel=None, progress=None) -> SearchExecution:
        self.calls += 1
        return SearchExecution(
            query=build_query(request),
            papers=(),
            cache_status="miss",
            history_id="history-stale",
            stale_confirmation_required=True,
            error=ArxivFilterError(ErrorCode.ARXIV_NOT_ACCEPTABLE),
        )


class PartialSearchService(BlockingSearchService):
    async def run(self, request, *, cancel=None, progress=None) -> SearchExecution:
        self.calls += 1
        return SearchExecution(
            query=build_query(request),
            papers=(),
            cache_status="miss",
            history_id="history-partial",
            partial=True,
        )


@pytest.mark.asyncio
async def test_ten_identical_requests_share_one_job_and_one_service_call() -> None:
    service = BlockingSearchService()
    manager = JobManager(service)  # type: ignore[arg-type]

    jobs = await asyncio.gather(*(manager.create(make_request()) for _ in range(10)))
    await service.started.wait()

    assert len({job.job_id for job in jobs}) == 1
    assert service.calls == 1
    assert (await manager.snapshot(jobs[0].job_id)).status.phase is SearchPhase.FETCHING
    with pytest.raises(ArxivFilterError) as unfinished:
        await manager.get_execution(jobs[0].job_id)
    assert unfinished.value.code is ErrorCode.JOB_NOT_FINISHED

    service.release.set()
    final = await manager.wait(jobs[0].job_id)
    assert final.status.phase is SearchPhase.COMPLETED
    assert final.status.fetched == 2
    assert final.status.target == 5
    assert final.status.pages == 1
    assert final.status.retries == 1


@pytest.mark.asyncio
async def test_read_filter_does_not_split_same_cache_key_coalescing() -> None:
    service = BlockingSearchService()
    manager = JobManager(service)  # type: ignore[arg-type]

    first = await manager.create(make_request(read_filter="new"))
    second = await manager.create(make_request(read_filter="unread"))
    await service.started.wait()

    assert first.job_id == second.job_id
    assert service.calls == 1
    service.release.set()
    assert (await manager.wait(first.job_id)).status.phase is SearchPhase.COMPLETED


@pytest.mark.asyncio
async def test_different_query_is_rejected_while_one_job_is_active() -> None:
    service = BlockingSearchService()
    manager = JobManager(service)  # type: ignore[arg-type]
    job = await manager.create(make_request())
    await service.started.wait()

    with pytest.raises(ArxivFilterError) as caught:
        await manager.create(make_request(keywords="retrieval"))

    assert caught.value.code is ErrorCode.JOB_LIMIT_REACHED
    assert caught.value.http_status == 409
    assert service.calls == 1
    service.release.set()
    await manager.wait(job.job_id)


@pytest.mark.asyncio
async def test_stale_consent_is_not_coalesced_with_unapproved_request() -> None:
    service = BlockingSearchService()
    manager = JobManager(service)  # type: ignore[arg-type]
    job = await manager.create(make_request(allow_stale=False))
    await service.started.wait()

    with pytest.raises(ArxivFilterError) as caught:
        await manager.create(make_request(allow_stale=True))

    assert caught.value.code is ErrorCode.JOB_LIMIT_REACHED
    service.release.set()
    await manager.wait(job.job_id)


@pytest.mark.asyncio
async def test_cancel_is_idempotent_and_terminal_state_does_not_change() -> None:
    service = BlockingSearchService()
    manager = JobManager(service)  # type: ignore[arg-type]
    job = await manager.create(make_request())
    await service.started.wait()

    cancelling = await manager.cancel(job.job_id)
    repeated = await manager.cancel(job.job_id)
    assert cancelling.status.phase is SearchPhase.CANCELLING
    assert repeated.status.phase is SearchPhase.CANCELLING

    service.release.set()
    cancelled = await manager.wait(job.job_id)
    repeated_terminal = await manager.cancel(job.job_id)
    assert cancelled.status.phase is SearchPhase.CANCELLED
    assert repeated_terminal.status == cancelled.status
    assert await manager.get_execution(job.job_id) is None


@pytest.mark.asyncio
async def test_cancel_before_worker_starts_never_calls_search_service() -> None:
    service = BlockingSearchService()
    manager = JobManager(service)  # type: ignore[arg-type]
    job = await manager.create(make_request())

    cancelling = await manager.cancel(job.job_id)
    assert cancelling.status.phase is SearchPhase.CANCELLING
    cancelled = await manager.wait(job.job_id)

    assert cancelled.status.phase is SearchPhase.CANCELLED
    assert service.calls == 0


@pytest.mark.asyncio
async def test_provider_error_is_stored_as_failed_job_and_rethrown() -> None:
    service = FailingSearchService()
    manager = JobManager(service)  # type: ignore[arg-type]
    job = await manager.create(make_request())

    failed = await manager.wait(job.job_id)

    assert failed.status.phase is SearchPhase.FAILED
    with pytest.raises(ArxivFilterError) as caught:
        await manager.get_execution(job.job_id)
    assert caught.value.code is ErrorCode.ARXIV_NOT_ACCEPTABLE


@pytest.mark.asyncio
async def test_stale_confirmation_execution_remains_available_for_api() -> None:
    service = StaleConfirmationSearchService()
    manager = JobManager(service)  # type: ignore[arg-type]
    job = await manager.create(make_request())

    failed = await manager.wait(job.job_id)
    execution = await manager.get_execution(job.job_id)

    assert failed.status.phase is SearchPhase.FAILED
    assert execution is not None
    assert execution.stale_confirmation_required
    assert execution.error is not None


@pytest.mark.asyncio
async def test_partial_execution_becomes_cancelled_and_retains_execution() -> None:
    service = PartialSearchService()
    manager = JobManager(service)  # type: ignore[arg-type]
    job = await manager.create(make_request())

    cancelled = await manager.wait(job.job_id)
    execution = await manager.get_execution(job.job_id)

    assert cancelled.status.phase is SearchPhase.CANCELLED
    assert cancelled.partial
    assert execution is not None and execution.partial


@pytest.mark.asyncio
async def test_illegal_transition_returns_409_without_mutating_state() -> None:
    service = BlockingSearchService()
    manager = JobManager(service)  # type: ignore[arg-type]
    job = await manager.create(make_request())
    await service.started.wait()
    record = manager._records[job.job_id]  # exercise the internal transition guard

    with pytest.raises(ArxivFilterError) as caught:
        manager._transition(record, SearchPhase.QUEUED)

    assert caught.value.code is ErrorCode.JOB_INVALID_TRANSITION
    assert caught.value.http_status == 409
    assert (await manager.snapshot(job.job_id)).status.phase is SearchPhase.FETCHING
    service.release.set()
    await manager.wait(job.job_id)


@pytest.mark.asyncio
async def test_completed_job_cannot_be_cancelled_and_not_found_is_stable() -> None:
    service = BlockingSearchService()
    manager = JobManager(service)  # type: ignore[arg-type]
    job = await manager.create(make_request())
    service.release.set()
    completed = await manager.wait(job.job_id)

    with pytest.raises(ArxivFilterError) as caught:
        await manager.cancel(job.job_id)
    assert caught.value.code is ErrorCode.JOB_ALREADY_FINISHED
    assert caught.value.http_status == 409
    assert (await manager.snapshot(job.job_id)).status == completed.status

    with pytest.raises(ArxivFilterError) as missing:
        await manager.snapshot("not-a-uuid")
    assert missing.value.code is ErrorCode.JOB_NOT_FOUND


@pytest.mark.asyncio
async def test_old_terminal_jobs_are_evicted_but_recent_jobs_remain_available() -> None:
    service = BlockingSearchService()
    service.release.set()
    manager = JobManager(service, max_retained_jobs=2)  # type: ignore[arg-type]

    jobs = []
    for keyword in ("first query", "second query", "third query"):
        job = await manager.create(make_request(keywords=keyword))
        jobs.append(await manager.wait(job.job_id))

    with pytest.raises(ArxivFilterError) as expired:
        await manager.snapshot(jobs[0].job_id)
    assert expired.value.code is ErrorCode.JOB_NOT_FOUND
    assert (await manager.snapshot(jobs[1].job_id)).status.phase is SearchPhase.COMPLETED
    assert (await manager.snapshot(jobs[2].job_id)).status.phase is SearchPhase.COMPLETED
    assert len(manager._records) == 2

    replacement = await manager.create(make_request(keywords="first query"))
    assert replacement.job_id != jobs[0].job_id
    assert (await manager.wait(replacement.job_id)).status.phase is SearchPhase.COMPLETED


@pytest.mark.asyncio
async def test_external_task_cancellation_also_trims_terminal_records() -> None:
    service = BlockingSearchService()
    manager = JobManager(service, max_retained_jobs=1)  # type: ignore[arg-type]

    first = await manager.create(make_request(keywords="first query"))
    service.release.set()
    await manager.wait(first.job_id)

    service.started.clear()
    service.release.clear()
    second = await manager.create(make_request(keywords="second query"))
    await service.started.wait()
    manager._records[second.job_id].task.cancel()
    cancelled = await manager.wait(second.job_id)

    assert cancelled.status.phase is SearchPhase.CANCELLED
    with pytest.raises(ArxivFilterError) as expired:
        await manager.snapshot(first.job_id)
    assert expired.value.code is ErrorCode.JOB_NOT_FOUND
