"""分页器与偏移窗口处理（方案 §5.5 T3.3）。

规则（方案 §5.5）：

1. miss 时请求 ``start=0, max_results=200``，同一页得到论文与 ``totalResults``；
   **禁止 count-only**（``max_results=0`` 实测返回 500）；
2. ``target = min(totalResults, result_cap)``；循环 ``start += len(page.papers)``；
3. 若返回 0 条但 ``start < target``、``startIndex`` 回退、或连续两页 ID 集合相同，
   报 ``PAGINATION_STALLED``，避免死循环；
4. ``totalResults > result_cap`` 时 ``truncated=True``；
5. 旧手册的总窗口 30000 与当前行为冲突：当前能力边界是
   ``start + max_results <= 10000``，应用再留余量用 **9500**；
6. 需要超过 9500 时，按 UTC 分钟递归二分日期：
   左段 ``[lo, mid]``、右段 ``[mid + 1 minute, hi]``，直到每段 ``total <= 9500``；
   同一分钟仍超限时停止并提示缩小分类或关键词；
7. 合并分片后按无版本 canonical ID 去重；
8. 取消后**不再发下一页**。

分片时用 ``date_range_factory`` **重新构造** :class:`BuiltQuery`，而不是对自己的
渲染结果做字符串替换——后者一旦表达式格式变化就会静默出错。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from arxiv_filter.arxiv.dedupe import deduplicate
from arxiv_filter.arxiv.provider import CancelToken, MetadataProvider
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.paper import Paper
from arxiv_filter.models.query import BuiltQuery, FeedPage

#: 默认页大小（方案 §5.4）。
DEFAULT_PAGE_SIZE = 200

#: 上游偏移安全窗口（方案 §5.5 第 7 条）。留了 500 的余量。
SAFE_OFFSET_WINDOW = 9500

#: 二分的最小粒度：1 分钟（方案 §5.5 第 8 条）。
MIN_SPLIT_MINUTES = 1

#: 二分探测的请求预算。
#:
#: 递归二分最坏情况会展开成一棵很大的树（把一整天切到 1 分钟需要约 2880 个节点）。
#: 对 arXiv 发几千个探测请求显然不可接受，所以设一个硬预算；预算用尽时保留
#: 尚未细分的片段并写入警告，让用户去缩小分类或关键词。
DEFAULT_MAX_SPLIT_REQUESTS = 60

#: 单次 job 的页数上限（防御性上界，正常永远先撞到 target）。
MAX_PAGES = 500


@dataclass(slots=True)
class PageProgress:
    """进度快照，供 ``SearchProgress`` 使用。"""

    fetched: int = 0
    target: int | None = None
    total_reported: int | None = None
    pages: int = 0
    retries: int = 0

    @property
    def indeterminate(self) -> bool:
        """``totalResults`` 尚未到达时为真——前端显示不定进度。"""
        return self.target is None


@dataclass(slots=True)
class FetchOutcome:
    """一次完整的分页抓取结果。"""

    papers: list[Paper] = field(default_factory=list)
    total_reported: int | None = None
    truncated: bool = False
    partial: bool = False
    pages: int = 0
    warnings: list[str] = field(default_factory=list)
    #: 日期被拆成的分片数量；1 表示未拆分。
    shards: int = 1


@dataclass(frozen=True, slots=True)
class DateSegment:
    """一个 UTC 时间片段（闭区间，分钟精度）。"""

    start: datetime
    end: datetime

    def as_dates(self) -> tuple[date, date]:
        return self.start.date(), self.end.date()


#: 给定分钟精度的起止时刻重建查询；由调用方提供，保证分片走**正常的构造路径**
#: （见 ``query_builder.build_query(request, start_at=..., end_at=...)``），
#: 而不是对渲染后的表达式做字符串替换。
DateRangeFactory = Callable[[datetime, datetime], BuiltQuery]


def utc_day_bounds(start: date, end: date) -> DateSegment:
    """把 UTC 自然日闭区间转成分钟精度的时间片段（方案「已锁定假设」§3）。"""
    return DateSegment(
        start=datetime.combine(start, time(0, 0), tzinfo=timezone.utc),
        end=datetime.combine(end, time(23, 59), tzinfo=timezone.utc),
    )


def segment_minutes(segment: DateSegment) -> int:
    """片段跨越的分钟数（闭区间，最少 1）。"""
    return max(1, int((segment.end - segment.start).total_seconds() // 60) + 1)


def split_segment(segment: DateSegment) -> tuple[DateSegment, DateSegment]:
    """按 UTC 分钟二分成左右两段。

    左段 ``[lo, mid]``、右段 ``[mid + 1 minute, hi]``——两段**不重叠**，
    因此合并后没有重复时间区间（ID 去重仍然照做，覆盖 cross-list）。
    """
    if segment.end <= segment.start:
        raise ArxivFilterError(
            ErrorCode.SEARCH_TIMEOUT,
            details={"reason": "segment_not_splittable"},
            message_zh="时间片段已经细到 1 分钟，无法继续拆分。",
        )
    span_minutes = int((segment.end - segment.start).total_seconds() // 60)
    half = max(1, span_minutes // 2)
    midpoint = segment.start + timedelta(minutes=half)
    if midpoint >= segment.end:
        midpoint = segment.end - timedelta(minutes=1)
    left = DateSegment(segment.start, midpoint)
    right = DateSegment(midpoint + timedelta(minutes=1), segment.end)
    return left, right


def should_stop_splitting(segment: DateSegment, total: int | None, safe_window: int) -> bool:
    """该片段是否已经可以直接抓取，不需要继续二分。

    ``total`` 为 ``None``（上游没报总量）时不再细分——继续二分也拿不到判据。
    """
    if total is None or total <= safe_window:
        return True
    return segment_minutes(segment) <= MIN_SPLIT_MINUTES


class Paginator:
    """把一次查询抓成完整的论文列表。"""

    def __init__(
        self,
        provider: MetadataProvider,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        safe_window: int = SAFE_OFFSET_WINDOW,
        max_split_requests: int = DEFAULT_MAX_SPLIT_REQUESTS,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size 必须为正")
        if max_split_requests < 1:
            raise ValueError("max_split_requests 必须为正")
        self._provider = provider
        self._page_size = page_size
        self._safe_window = safe_window
        self._max_split_requests = max_split_requests

    @property
    def page_size(self) -> int:
        return self._page_size

    @property
    def safe_window(self) -> int:
        return self._safe_window

    @property
    def max_split_requests(self) -> int:
        return self._max_split_requests

    async def fetch_all(
        self,
        query: BuiltQuery,
        *,
        result_cap: int,
        cancel: CancelToken | None = None,
        progress: PageProgress | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        date_range_factory: DateRangeFactory | None = None,
    ) -> FetchOutcome:
        """抓取到 ``result_cap`` 为止（或上游没有更多结果）。

        只有同时提供 ``start_date`` / ``end_date`` 与 ``date_range_factory`` 时才可能
        触发日期二分——缺少任何一项，超出安全窗口的部分会被截断并写入警告。
        """
        state = progress if progress is not None else PageProgress()
        warnings: list[str] = []

        first = await self._get_page(query, 0, cancel)
        state.pages += 1
        total = first.total_results
        state.total_reported = total
        state.target = min(total, result_cap) if total is not None else None

        can_split = (
            start_date is not None and end_date is not None and date_range_factory is not None
        )

        if total is not None and total > self._safe_window and can_split:
            segments = await self._split_until_safe(
                query, start_date, end_date, date_range_factory, cancel, warnings  # type: ignore[arg-type]
            )
            collected: list[Paper] = []
            for segment in segments:
                if len(collected) >= result_cap:
                    break
                segment_query = date_range_factory(segment.start, segment.end)  # type: ignore[misc]
                await self._paginate(
                    segment_query, cancel, state, collected, result_cap, first_page=None
                )
            shards = len(segments)
        else:
            if total is not None and total > self._safe_window:
                warnings.append(
                    f"上游报告 {total} 条，超过安全窗口 {self._safe_window}；"
                    "已按结果上限截断。缩小日期范围或分类可以获得更完整的结果。"
                )
            collected = []
            await self._paginate(
                query, cancel, state, collected, result_cap, first_page=first
            )
            shards = 1

        deduped = deduplicate(collected)
        return FetchOutcome(
            papers=deduped.papers[:result_cap],
            total_reported=total,
            truncated=bool(total is not None and total > result_cap),
            pages=state.pages,
            warnings=[*warnings, *deduped.warnings],
            shards=shards,
        )

    # —— 内部 ——

    async def _get_page(
        self, query: BuiltQuery, start: int, cancel: CancelToken | None
    ) -> FeedPage:
        return await self._provider.fetch_page(query, start, self._page_size, cancel)

    async def _paginate(
        self,
        query: BuiltQuery,
        cancel: CancelToken | None,
        state: PageProgress,
        collected: list[Paper],
        result_cap: int,
        *,
        first_page: FeedPage | None,
    ) -> None:
        """在一个查询（或一个日期分片）上翻页，把结果追加进 ``collected``。"""
        start = 0
        page = first_page
        previous_ids: set[str] | None = None
        total = state.total_reported

        for _ in range(MAX_PAGES):
            if len(collected) >= result_cap:
                return
            if cancel is not None:
                cancel.raise_if_cancelled()

            # 安全窗口必须在**发请求之前**检查：start + max_results 一旦越过 9500
            # 就会被上游以 500 拒绝。放在请求之后检查等于已经发出了越界请求。
            if start + self._page_size > self._safe_window:
                return

            if page is None:
                page = await self._get_page(query, start, cancel)
                state.pages += 1

            if not page.papers:
                target = min(total, result_cap) if total is not None else None
                if target is not None and start < target:
                    raise ArxivFilterError(
                        ErrorCode.PAGINATION_STALLED,
                        details={"reason": "empty_page", "start": start, "total": total},
                    )
                return

            page_ids = {paper.canonical_id for paper in page.papers}
            if previous_ids is not None and page_ids == previous_ids:
                raise ArxivFilterError(
                    ErrorCode.PAGINATION_STALLED,
                    details={
                        "reason": "repeated_page",
                        "start": start,
                        "page_size": len(page.papers),
                    },
                )
            if page.start_index < start:
                raise ArxivFilterError(
                    ErrorCode.PAGINATION_STALLED,
                    details={
                        "reason": "start_index_went_backwards",
                        "requested": start,
                        "reported": page.start_index,
                    },
                )

            previous_ids = page_ids
            collected.extend(page.papers)
            state.fetched = len(collected)

            start += len(page.papers)
            page = None

            if total is not None and start >= min(total, result_cap):
                return

    async def _split_until_safe(
        self,
        query: BuiltQuery,
        start_date: date,
        end_date: date,
        factory: DateRangeFactory,
        cancel: CancelToken | None,
        warnings: list[str],
        *,
        initial: list[DateSegment] | None = None,
    ) -> list[DateSegment]:
        """递归二分日期，直到每段 ``total <= safe_window`` 或细到 1 分钟。

        探测请求数受 :data:`DEFAULT_MAX_SPLIT_REQUESTS` 约束；预算用尽时把尚未细分
        的片段原样保留并写警告——宁可给用户一个「结果可能不完整」的明确提示，
        也不向 arXiv 连发几千个探测请求。

        ``initial`` 可以覆盖起始片段集合；默认是「整段日期范围」。
        注意：从整天切到 1 分钟需要约 2048 次探测，远超默认预算，
        所以现实中**预算总是先于 1 分钟下限生效**——1 分钟那条规则是理论兜底。
        """
        pending = list(initial) if initial is not None else [utc_day_bounds(start_date, end_date)]
        safe: list[DateSegment] = []
        probes = 0
        budget_exhausted = False

        while pending:
            if cancel is not None:
                cancel.raise_if_cancelled()

            if probes >= self._max_split_requests:
                budget_exhausted = True
                break

            segment = pending.pop(0)
            probe = factory(segment.start, segment.end)
            page = await self._provider.fetch_page(probe, 0, 1, cancel)
            probes += 1
            total = page.total_results

            if should_stop_splitting(segment, total, self._safe_window):
                if total is not None and total > self._safe_window:
                    warnings.append(
                        f"{segment.start:%Y-%m-%d %H:%M} UTC 起的一分钟片段内仍有 "
                        f"{total} 条结果，超过安全窗口 {self._safe_window}；"
                        "请缩小分类或关键词。"
                    )
                safe.append(segment)
                continue

            left, right = split_segment(segment)
            pending.insert(0, left)
            pending.insert(0, right)

        if budget_exhausted:
            warnings.append(
                f"日期拆分达到 {self._max_split_requests} 次探测的上限，"
                f"剩余 {len(pending)} 个时间片段未再细分；结果可能不完整，"
                "建议缩小日期范围或分类。"
            )
            safe.extend(pending)

        safe.sort(key=lambda item: item.start)
        return safe


__all__ = [
    "DEFAULT_MAX_SPLIT_REQUESTS",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGES",
    "MIN_SPLIT_MINUTES",
    "SAFE_OFFSET_WINDOW",
    "DateRangeFactory",
    "DateSegment",
    "FetchOutcome",
    "PageProgress",
    "Paginator",
    "segment_minutes",
    "should_stop_splitting",
    "split_segment",
    "utc_day_bounds",
]
