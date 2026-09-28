"""T3.4 搜索编排：缓存优先、显式 stale 确认、取消与去重。"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from arxiv_filter.arxiv.provider import CancelToken
from arxiv_filter.arxiv.pagination import FetchOutcome, PageProgress
from arxiv_filter.arxiv.query_builder import build_query
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.paper import Author, Paper
from arxiv_filter.models.query import BuiltQuery, FeedPage, SearchRequest
from arxiv_filter.services.search_service import SearchService
from arxiv_filter.storage.cache_repo import CacheRepository
from arxiv_filter.storage.database import Database
from arxiv_filter.storage.history_repo import HistoryRepository
from arxiv_filter.taxonomy import load_taxonomy

UTC = timezone.utc
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


class FakeProvider:
    """只在测试中使用的 provider；不创建网络连接。"""

    name = "arxiv_api"

    def __init__(
        self,
        page: FeedPage | None = None,
        *,
        error: ArxivFilterError | None = None,
        cancel_on_fetch: bool = False,
        cancel_after_fetch: bool = False,
    ) -> None:
        self.page = page
        self.error = error
        self.cancel_on_fetch = cancel_on_fetch
        self.cancel_after_fetch = cancel_after_fetch
        self.calls: list[tuple[BuiltQuery, int, int]] = []

    async def fetch_page(
        self,
        query: BuiltQuery,
        start: int,
        limit: int,
        cancel: CancelToken | None,
    ) -> FeedPage:
        self.calls.append((query, start, limit))
        if self.error is not None:
            raise self.error
        if self.cancel_on_fetch:
            raise asyncio.CancelledError
        if cancel is not None:
            cancel.raise_if_cancelled()
        assert self.page is not None
        if self.cancel_after_fetch and isinstance(cancel, FakeCancel):
            cancel.cancel()
        return self.page


class FakeCancel:
    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled
        self.checks = 0

    def is_set(self) -> bool:
        return self.cancelled

    def cancel(self) -> None:
        self.cancelled = True

    def raise_if_cancelled(self) -> None:
        self.checks += 1
        if self.cancelled:
            raise asyncio.CancelledError


class FixedPaginator:
    def __init__(self, outcome: FetchOutcome) -> None:
        self.outcome = outcome
        self.factory_query: BuiltQuery | None = None

    async def fetch_all(
        self,
        query: BuiltQuery,
        *,
        result_cap: int,
        cancel: CancelToken | None,
        progress: PageProgress,
        start_date: date,
        end_date: date,
        date_range_factory,
    ) -> FetchOutcome:
        self.factory_query = date_range_factory(
            datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
            datetime(2026, 9, 18, 8, 30, tzinfo=UTC),
        )
        return self.outcome


def make_request(*, allow_stale: bool = False) -> SearchRequest:
    return SearchRequest(
        group_id="cs",
        category_id="cs.CL",
        start_date=date(2026, 9, 17),
        end_date=date(2026, 9, 24),
        keywords='"language model"',
        allow_stale_cache=allow_stale,
    )


def make_paper(
    *,
    version: int = 1,
    categories: list[str] | None = None,
    title: str = "A language model paper",
) -> Paper:
    return Paper(
        canonical_id="2609.01234",
        versioned_id=f"2609.01234v{version}",
        version=version,
        title=title,
        authors=[Author(name="A. Author")],
        summary_raw="A summary.",
        published_at=datetime(2026, 9, 20, tzinfo=UTC),
        updated_at=datetime(2026, 9, 21 + (version - 1), tzinfo=UTC),
        primary_category="cs.CL",
        categories=categories or ["cs.CL"],
        abs_url=f"https://arxiv.org/abs/2609.01234v{version}",
        retrieved_at=NOW,
    )


async def open_storage(tmp_path: Path) -> tuple[Database, CacheRepository, HistoryRepository]:
    db = Database(tmp_path / "arxiv-filter.db")
    await db.open()
    return db, CacheRepository(db), HistoryRepository(db)


async def seed_stale(
    cache: CacheRepository,
    request: SearchRequest,
    papers: list[Paper] | None = None,
) -> BuiltQuery:
    query = build_query(request, taxonomy=load_taxonomy())
    await cache.put_complete(
        query.cache_key,
        query.canonical_json,
        papers or [make_paper()],
        now=NOW - timedelta(hours=7),
    )
    return query


@pytest.mark.asyncio
async def test_fresh_cache_hit_never_calls_provider(tmp_path: Path) -> None:
    db, cache, history = await open_storage(tmp_path)
    request = make_request()
    query = build_query(request, taxonomy=load_taxonomy())
    await cache.put_complete(query.cache_key, query.canonical_json, [make_paper()], now=NOW)
    provider = FakeProvider(error=ArxivFilterError(ErrorCode.ARXIV_NOT_ACCEPTABLE))
    service = SearchService(cache, history, provider, clock=lambda: NOW)
    progress = PageProgress()

    try:
        assert service.cache_key_for(request) == query.cache_key
        result = await service.run(request, progress=progress)
        assert result.cache_status == "hit"
        assert result.completed
        assert [paper.canonical_id for paper in result.papers] == ["2609.01234"]
        assert provider.calls == []
        assert (progress.fetched, progress.target, progress.total_reported, progress.pages) == (
            1,
            1,
            None,
            0,
        )
        entry = (await history.list(limit=1))[0]
        assert (entry.status, entry.cache_hit, entry.result_count) == ("completed", True, 1)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_success_deduplicates_cross_list_before_caching(tmp_path: Path) -> None:
    db, cache, history = await open_storage(tmp_path)
    request = make_request()
    first = make_paper(version=1, categories=["cs.CL"])
    second = make_paper(version=2, categories=["cs.LG"])
    provider = FakeProvider(
        FeedPage(total_results=2, start_index=0, items_per_page=2, papers=[first, second])
    )
    service = SearchService(cache, history, provider, clock=lambda: NOW)

    try:
        result = await service.run(request)
        assert result.cache_status == "miss"
        assert len(result.papers) == 1
        assert result.papers[0].version == 2
        assert result.papers[0].categories == ["cs.LG", "cs.CL"]
        cached = await cache.get_fresh(result.query.cache_key, now=NOW)
        assert cached is not None and len(cached.papers) == 1
        entry = (await history.list(limit=1))[0]
        assert (entry.status, entry.cache_hit, entry.result_count) == ("completed", False, 1)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_stale_result_is_not_returned_without_explicit_confirmation(
    tmp_path: Path,
) -> None:
    db, cache, history = await open_storage(tmp_path)
    request = make_request()
    await seed_stale(cache, request)
    provider = FakeProvider(error=ArxivFilterError(ErrorCode.ARXIV_NOT_ACCEPTABLE))
    service = SearchService(cache, history, provider, clock=lambda: NOW)

    try:
        result = await service.run(request)
        assert result.stale_confirmation_required is True
        assert not result.completed
        assert result.error is not None
        assert result.error.code is ErrorCode.ARXIV_NOT_ACCEPTABLE
        assert result.papers == ()
        assert result.stale_cache_created_at is not None
        assert result.stale_cache_expires_at is not None
        assert result.stale_cache_result_count == 1
        cached = await cache.get_stale(result.query.cache_key, now=NOW)
        assert cached is not None and cached.complete
        entry = (await history.list(limit=1))[0]
        assert entry.status == "failed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_confirmed_stale_result_is_returned_and_not_replaced(tmp_path: Path) -> None:
    db, cache, history = await open_storage(tmp_path)
    request = make_request(allow_stale=True)
    query = await seed_stale(cache, request)
    old = await cache.get_stale(query.cache_key, now=NOW)
    assert old is not None
    provider = FakeProvider(error=ArxivFilterError(ErrorCode.ARXIV_NOT_ACCEPTABLE))
    service = SearchService(cache, history, provider, clock=lambda: NOW)

    try:
        result = await service.run(request)
        assert result.cache_status == "stale"
        assert result.error is None
        assert result.papers == old.papers
        assert result.stale_cache_result_count == 1
        assert any("过期缓存" in warning for warning in result.warnings)
        after = await cache.get_stale(query.cache_key, now=NOW)
        assert after is not None
        assert after.created_at == old.created_at
        entry = (await history.list(limit=1))[0]
        assert (entry.status, entry.cache_hit, entry.result_count) == ("completed", False, 1)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_upstream_error_without_stale_is_propagated_and_not_cached(
    tmp_path: Path,
) -> None:
    db, cache, history = await open_storage(tmp_path)
    provider_error = ArxivFilterError(ErrorCode.ARXIV_NOT_ACCEPTABLE)
    service = SearchService(
        cache,
        history,
        FakeProvider(error=provider_error),
        clock=lambda: NOW,
    )

    try:
        with pytest.raises(ArxivFilterError) as caught:
            await service.run(make_request())
        assert caught.value.code is ErrorCode.ARXIV_NOT_ACCEPTABLE
        assert await cache.entry_count() == 0
        entry = (await history.list(limit=1))[0]
        assert (entry.status, entry.result_count) == ("failed", None)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancel_does_not_replace_existing_complete_stale_cache(tmp_path: Path) -> None:
    db, cache, history = await open_storage(tmp_path)
    request = make_request()
    query = await seed_stale(cache, request)
    before = await cache.get_stale(query.cache_key, now=NOW)
    assert before is not None
    provider = FakeProvider(cancel_on_fetch=True)
    service = SearchService(cache, history, provider, clock=lambda: NOW)

    try:
        with pytest.raises(asyncio.CancelledError):
            await service.run(request)
        after = await cache.get_stale(query.cache_key, now=NOW)
        assert after is not None and after.complete
        assert after.created_at == before.created_at
        assert after.papers == before.papers
        entry = (await history.list(limit=1))[0]
        assert (entry.status, entry.result_count) == ("cancelled", None)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_query_contract_error_does_not_offer_stale_result(tmp_path: Path) -> None:
    db, cache, history = await open_storage(tmp_path)
    request = make_request()
    await seed_stale(cache, request)
    provider_error = ArxivFilterError(ErrorCode.ARXIV_QUERY_CONTRACT)
    service = SearchService(
        cache,
        history,
        FakeProvider(error=provider_error),
        clock=lambda: NOW,
    )

    try:
        with pytest.raises(ArxivFilterError) as caught:
            await service.run(request)
        assert caught.value.code is ErrorCode.ARXIV_QUERY_CONTRACT
        entry = (await history.list(limit=1))[0]
        assert entry.status == "failed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_pre_cancel_finishes_history_without_reading_cache_or_calling_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, cache, history = await open_storage(tmp_path)
    provider = FakeProvider(error=ArxivFilterError(ErrorCode.ARXIV_NOT_ACCEPTABLE))
    service = SearchService(cache, history, provider, clock=lambda: NOW)

    async def forbidden_cache_read(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("cancelled job must not read cache")

    monkeypatch.setattr(cache, "get_fresh", forbidden_cache_read)
    try:
        with pytest.raises(asyncio.CancelledError):
            await service.run(make_request(), cancel=FakeCancel(cancelled=True))
        assert provider.calls == []
        entry = (await history.list(limit=1))[0]
        assert entry.status == "cancelled"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cancel_after_last_page_prevents_complete_cache_commit(tmp_path: Path) -> None:
    db, cache, history = await open_storage(tmp_path)
    provider = FakeProvider(
        FeedPage(total_results=1, start_index=0, items_per_page=1, papers=[make_paper()]),
        cancel_after_fetch=True,
    )
    service = SearchService(cache, history, provider, clock=lambda: NOW)
    cancel = FakeCancel()

    try:
        with pytest.raises(asyncio.CancelledError):
            await service.run(make_request(), cancel=cancel)
        assert cancel.is_set()
        assert cancel.checks >= 3
        assert await cache.entry_count() == 0
        entry = (await history.list(limit=1))[0]
        assert entry.status == "cancelled"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_partial_outcome_stays_in_memory_and_is_not_cached(tmp_path: Path) -> None:
    db, cache, history = await open_storage(tmp_path)
    request = make_request()
    query = build_query(request, taxonomy=load_taxonomy())
    paginator = FixedPaginator(
        FetchOutcome(papers=[make_paper()], total_reported=2, partial=True, pages=1)
    )
    service = SearchService(
        cache,
        history,
        FakeProvider(),
        paginator=paginator,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    try:
        result = await service.run(request)
        assert result.partial
        assert not result.completed
        assert len(result.papers) == 1
        assert paginator.factory_query is not None
        assert "202609171200 TO 202609180830" in paginator.factory_query.expression
        assert await cache.entry_count() == 0
        entry = (await history.list(limit=1))[0]
        assert entry.status == "cancelled"
        assert entry.cache_key == query.cache_key
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cache_read_failure_falls_through_to_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, cache, history = await open_storage(tmp_path)
    provider = FakeProvider(
        FeedPage(total_results=1, start_index=0, items_per_page=1, papers=[make_paper()])
    )
    service = SearchService(cache, history, provider, clock=lambda: NOW)

    async def fail_cache_read(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("simulated cache read failure")

    monkeypatch.setattr(cache, "get_fresh", fail_cache_read)
    try:
        result = await service.run(make_request())
        assert result.completed
        assert len(provider.calls) == 1
        assert any("缓存读取失败" in warning for warning in result.warnings)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cache_write_failure_does_not_discard_live_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, cache, history = await open_storage(tmp_path)
    provider = FakeProvider(
        FeedPage(total_results=1, start_index=0, items_per_page=1, papers=[make_paper()])
    )
    service = SearchService(cache, history, provider, clock=lambda: NOW)

    async def fail_cache_write(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("simulated cache write failure")

    monkeypatch.setattr(cache, "put_complete", fail_cache_write)
    try:
        result = await service.run(make_request())
        assert result.completed
        assert len(result.papers) == 1
        assert any("本地缓存暂时不可用" in warning for warning in result.warnings)
        entry = (await history.list(limit=1))[0]
        assert entry.status == "completed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cache_prune_failure_keeps_the_complete_cache_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, cache, history = await open_storage(tmp_path)
    provider = FakeProvider(
        FeedPage(total_results=1, start_index=0, items_per_page=1, papers=[make_paper()])
    )
    service = SearchService(cache, history, provider, clock=lambda: NOW)

    async def fail_prune(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("simulated prune failure")

    monkeypatch.setattr(cache, "prune", fail_prune)
    try:
        result = await service.run(make_request())
        assert result.completed
        assert any("清理未完成" in warning for warning in result.warnings)
        cached = await cache.get_fresh(result.query.cache_key, now=NOW)
        assert cached is not None and cached.complete
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_stale_lookup_failure_preserves_original_provider_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, cache, history = await open_storage(tmp_path)
    provider_error = ArxivFilterError(ErrorCode.ARXIV_NOT_ACCEPTABLE)
    service = SearchService(
        cache,
        history,
        FakeProvider(error=provider_error),
        clock=lambda: NOW,
    )

    async def fail_stale_lookup(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("simulated stale lookup failure")

    monkeypatch.setattr(cache, "get_stale", fail_stale_lookup)
    try:
        with pytest.raises(ArxivFilterError) as caught:
            await service.run(make_request())
        assert caught.value.code is ErrorCode.ARXIV_NOT_ACCEPTABLE
        entry = (await history.list(limit=1))[0]
        assert entry.status == "failed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unexpected_provider_exception_finishes_history_as_failed(tmp_path: Path) -> None:
    db, cache, history = await open_storage(tmp_path)

    class BrokenProvider(FakeProvider):
        async def fetch_page(self, query, start, limit, cancel):  # type: ignore[no-untyped-def]
            self.calls.append((query, start, limit))
            raise RuntimeError("simulated provider bug")

    service = SearchService(cache, history, BrokenProvider(), clock=lambda: NOW)
    try:
        with pytest.raises(RuntimeError, match="simulated provider bug"):
            await service.run(make_request())
        entry = (await history.list(limit=1))[0]
        assert entry.status == "failed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_naive_service_clock_is_rejected(tmp_path: Path) -> None:
    db, cache, history = await open_storage(tmp_path)
    service = SearchService(
        cache,
        history,
        FakeProvider(),
        clock=lambda: datetime(2026, 9, 24, 12, 0),
    )
    try:
        with pytest.raises(ValueError, match="必须返回带时区"):
            await service.run(make_request())
        assert await history.count() == 0
    finally:
        await db.close()
