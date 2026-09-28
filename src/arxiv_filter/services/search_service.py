"""搜索编排：fresh cache → arXiv → 经确认的 stale cache（T3.4）。

该服务保持同步的领域边界：构造查询、检查新鲜缓存、调用分页器、去重并保存
完整结果。任务状态机、HTTP DTO 和 read-state 展示快照留给阶段 4 的 JobManager。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from arxiv_filter.arxiv.dedupe import deduplicate
from arxiv_filter.arxiv.pagination import DateRangeFactory, PageProgress, Paginator
from arxiv_filter.arxiv.provider import CancelToken, MetadataProvider
from arxiv_filter.arxiv.query_builder import build_query
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.job import CacheStatus
from arxiv_filter.models.paper import Paper
from arxiv_filter.models.query import BuiltQuery, SearchRequest
from arxiv_filter.storage.cache_repo import CacheRepository, CachedResult
from arxiv_filter.storage.database import utc_now
from arxiv_filter.storage.history_repo import HistoryRepository
from arxiv_filter.taxonomy import Taxonomy

logger = logging.getLogger(__name__)

# These errors indicate an upstream or retrieval failure for which an old, exact-query
# result can help. Query-contract and local validation errors deliberately do not use
# stale data: an old result must not hide a rejected or malformed query.
_STALE_FALLBACK_CODES = frozenset(
    {
        ErrorCode.ARXIV_TIMEOUT,
        ErrorCode.ARXIV_DNS_FAILURE,
        ErrorCode.ARXIV_CONNECT_FAILURE,
        ErrorCode.ARXIV_RATE_LIMITED,
        ErrorCode.ARXIV_NOT_ACCEPTABLE,
        ErrorCode.ARXIV_UNAVAILABLE,
        ErrorCode.ARXIV_BAD_RESPONSE,
        ErrorCode.PAGINATION_STALLED,
        ErrorCode.SEARCH_TIMEOUT,
    }
)


@dataclass(frozen=True, slots=True)
class SearchExecution:
    """T3.4 的领域结果。

    ``stale_confirmation_required`` 表示主源失败且存在 stale cache，但用户尚未
    同意使用。此时 ``papers`` 必须为空；调用方可展示错误与缓存摘要，再以
    ``SearchRequest.allow_stale_cache=True`` 重试。

    缓存表尚未持久化 ``totalResults`` / ``truncated``，因此纯缓存命中时这两个字段
    为 ``None``，不伪造原始上游总数。
    """

    query: BuiltQuery
    papers: tuple[Paper, ...]
    cache_status: CacheStatus
    history_id: str
    total_reported: int | None = None
    truncated: bool | None = None
    partial: bool = False
    pages: int = 0
    warnings: tuple[str, ...] = ()
    stale_confirmation_required: bool = False
    stale_cache_created_at: datetime | None = None
    stale_cache_expires_at: datetime | None = None
    stale_cache_result_count: int | None = None
    error: ArxivFilterError | None = None

    @property
    def completed(self) -> bool:
        return self.error is None and not self.stale_confirmation_required and not self.partial


class SearchService:
    """把一次 ``SearchRequest`` 接到缓存、Provider、分页器和历史仓储。"""

    def __init__(
        self,
        cache: CacheRepository,
        history: HistoryRepository,
        provider: MetadataProvider,
        *,
        taxonomy: Taxonomy | None = None,
        paginator: Paginator | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._cache = cache
        self._history = history
        self._taxonomy = taxonomy
        self._paginator = paginator or Paginator(provider)
        self._clock = clock

    async def run(
        self,
        request: SearchRequest,
        *,
        cancel: CancelToken | None = None,
        progress: PageProgress | None = None,
    ) -> SearchExecution:
        """执行一次查询。

        * fresh hit 不调用 provider；
        * 完整的远端结果去重后才写 complete cache；
        * upstream 失败时只有显式 ``allow_stale_cache`` 才返回旧论文；
        * 取消、失败不调用 ``put_partial``，也不覆盖已有完整缓存。
        """
        query = self.build_query(request)
        started_at = self._aware_now()
        history_id = await self._history.start(
            request.model_dump(mode="json"), query.cache_key, now=started_at
        )

        if cancel is not None:
            try:
                cancel.raise_if_cancelled()
            except asyncio.CancelledError:
                await self._finish_history(history_id, "cancelled", None, cache_hit=False)
                raise

        cached: CachedResult | None
        warnings: tuple[str, ...] = ()
        try:
            cached = await self._cache.get_fresh(query.cache_key, now=started_at)
        except Exception as exc:  # noqa: BLE001 - cache failure must not block live search
            cached = None
            logger.warning("读取本地查询缓存失败，继续访问 provider：%s", type(exc).__name__)
            warnings = ("本地缓存读取失败，本次继续尝试在线查询。",)

        if cached is not None:
            deduped = deduplicate(cached.papers)
            await self._finish_history(
                history_id, "completed", len(deduped.papers), cache_hit=True
            )
            self._set_cached_progress(progress, len(deduped.papers))
            return SearchExecution(
                query=query,
                papers=tuple(deduped.papers),
                cache_status="hit",
                history_id=history_id,
                warnings=tuple(deduped.warnings),
            )

        state = progress if progress is not None else PageProgress()
        factory = self._date_range_factory(request)

        try:
            outcome = await self._paginator.fetch_all(
                query,
                result_cap=request.result_cap,
                cancel=cancel,
                progress=state,
                start_date=request.start_date,
                end_date=request.end_date,
                date_range_factory=factory,
            )
        except asyncio.CancelledError:
            await self._finish_history(history_id, "cancelled", None, cache_hit=False)
            raise
        except ArxivFilterError as error:
            return await self._handle_provider_error(
                request=request,
                query=query,
                history_id=history_id,
                error=error,
                progress=progress,
                warnings=warnings,
            )
        except Exception:
            await self._finish_history(history_id, "failed", None, cache_hit=False)
            raise

        deduped = deduplicate(outcome.papers)
        warnings = (*warnings, *outcome.warnings, *deduped.warnings)

        # A cancellation that arrived after the final page but before the cache commit
        # still prevents this job from publishing a supposedly complete cache entry.
        if cancel is not None:
            try:
                cancel.raise_if_cancelled()
            except asyncio.CancelledError:
                await self._finish_history(history_id, "cancelled", None, cache_hit=False)
                raise

        if not outcome.partial:
            try:
                await self._cache.put_complete(
                    query.cache_key,
                    query.canonical_json,
                    deduped.papers,
                    now=self._aware_now(),
                )
            except Exception as exc:  # noqa: BLE001 - return complete live results if cache fails
                logger.warning("写入本地查询缓存失败：%s", type(exc).__name__)
                warnings = (*warnings, "本地缓存暂时不可用，本次结果未能保存。")
            else:
                try:
                    await self._cache.prune(now=self._aware_now())
                except Exception as exc:  # noqa: BLE001 - the complete cache entry is already safe
                    logger.warning("清理本地查询缓存失败：%s", type(exc).__name__)
                    warnings = (*warnings, "本地缓存清理未完成，已保存本次完整结果。")

        status = "cancelled" if outcome.partial else "completed"
        await self._finish_history(
            history_id, status, len(deduped.papers), cache_hit=False
        )
        return SearchExecution(
            query=query,
            papers=tuple(deduped.papers),
            cache_status="miss",
            history_id=history_id,
            total_reported=outcome.total_reported,
            truncated=outcome.truncated,
            partial=outcome.partial,
            pages=outcome.pages,
            warnings=warnings,
        )

    async def _handle_provider_error(
        self,
        *,
        request: SearchRequest,
        query: BuiltQuery,
        history_id: str,
        error: ArxivFilterError,
        progress: PageProgress | None,
        warnings: tuple[str, ...],
    ) -> SearchExecution:
        stale: CachedResult | None = None
        if error.code in _STALE_FALLBACK_CODES:
            try:
                stale = await self._cache.get_stale(query.cache_key, now=self._aware_now())
            except Exception as exc:  # noqa: BLE001 - preserve the actionable upstream error
                logger.warning("读取 stale cache 失败：%s", type(exc).__name__)

        if stale is not None and request.allow_stale_cache:
            deduped = deduplicate(stale.papers)
            await self._finish_history(
                history_id, "completed", len(deduped.papers), cache_hit=False
            )
            self._set_cached_progress(progress, len(deduped.papers))
            return SearchExecution(
                query=query,
                papers=tuple(deduped.papers),
                cache_status="stale",
                history_id=history_id,
                warnings=(
                    *warnings,
                    "主数据源暂时不可用，已按确认使用过期缓存。",
                    error.message_zh,
                    *deduped.warnings,
                ),
                stale_cache_created_at=stale.created_at,
                stale_cache_expires_at=stale.stale_expires_at,
                stale_cache_result_count=len(deduped.papers),
            )

        await self._finish_history(history_id, "failed", None, cache_hit=False)
        if stale is not None:
            return SearchExecution(
                query=query,
                papers=(),
                cache_status="miss",
                history_id=history_id,
                warnings=(
                    *warnings,
                    "发现可用的过期缓存；确认后可重新查询并使用该缓存。",
                ),
                stale_confirmation_required=True,
                stale_cache_created_at=stale.created_at,
                stale_cache_expires_at=stale.stale_expires_at,
                stale_cache_result_count=len(stale.papers),
                error=error,
            )
        raise error

    async def _finish_history(
        self,
        history_id: str,
        status: str,
        result_count: int | None,
        *,
        cache_hit: bool,
    ) -> None:
        await self._history.finish(
            history_id,
            status,
            result_count=result_count,
            cache_hit=cache_hit,
            now=self._aware_now(),
        )

    def build_query(self, request: SearchRequest) -> BuiltQuery:
        """用本服务配置的 taxonomy 构造查询，供 JobManager 取得准确 cache key。"""
        return build_query(request, taxonomy=self._taxonomy)

    def cache_key_for(self, request: SearchRequest) -> str:
        """返回执行该请求会使用的缓存键，避免管理器重复实现规范化规则。"""
        return self.build_query(request).cache_key

    def _date_range_factory(self, request: SearchRequest) -> DateRangeFactory:
        def build_range(start: datetime, end: datetime) -> BuiltQuery:
            return build_query(
                request,
                taxonomy=self._taxonomy,
                start_at=start,
                end_at=end,
            )

        return build_range

    def _aware_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SearchService clock 必须返回带时区的 datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _set_cached_progress(progress: PageProgress | None, count: int) -> None:
        if progress is None:
            return
        progress.fetched = count
        progress.target = count
        progress.total_reported = None
        progress.pages = 0


__all__ = ["SearchExecution", "SearchService"]
