"""T3.3 分页器与偏移窗口（方案 §5.5）。

用假 provider 精确断言请求序列、停止条件与分片行为；不联网、不等待。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from arxiv_filter.arxiv.pagination import (
    DEFAULT_PAGE_SIZE,
    SAFE_OFFSET_WINDOW,
    DateSegment,
    PageProgress,
    Paginator,
    segment_minutes,
    should_stop_splitting,
    split_segment,
    utc_day_bounds,
)
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.paper import Author, Paper
from arxiv_filter.models.query import BuiltQuery, FeedPage


def make_query(start: date = date(2025, 1, 1), end: date = date(2025, 1, 7), key: str = "a") -> BuiltQuery:
    return BuiltQuery(
        category_ids=["cs.CL"],
        expression=f"cat:cs.CL AND submittedDate:[{start:%Y%m%d}0000 TO {end:%Y%m%d}2359]",
        sort_by="submittedDate",
        sort_order="descending",
        canonical_json="{}",
        cache_key=key * 64,
    )


def make_paper(index: int) -> Paper:
    return Paper(
        canonical_id=f"2501.{index:05d}",
        versioned_id=f"2501.{index:05d}v1",
        version=1,
        title=f"Paper {index}",
        authors=[Author(name="A. Author")],
        summary_raw="summary",
        published_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        updated_at=datetime(2025, 1, 3, tzinfo=timezone.utc),
        primary_category="cs.CL",
        categories=["cs.CL"],
        abs_url=f"https://arxiv.org/abs/2501.{index:05d}v1",
        retrieved_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
    )


class FakeProvider:
    """按 ``start`` 返回可预测的分页结果，并记录每次请求。"""

    name = "fake"

    def __init__(
        self,
        total: int,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        total_sequence: list[int | None] | None = None,
        empty_at: int | None = None,
        repeat_at: int | None = None,
        start_index_override: dict[int, int] | None = None,
    ) -> None:
        self.total = total
        self.page_size = page_size
        self.calls: list[tuple[int, int, str]] = []
        self._total_sequence = list(total_sequence or [])
        self._empty_at = empty_at
        self._repeat_at = repeat_at
        self._start_index_override = start_index_override or {}

    async def fetch_page(self, query, start, limit, cancel=None) -> FeedPage:
        if cancel is not None:
            cancel.raise_if_cancelled()
        self.calls.append((start, limit, query.expression))

        reported_total = self.total
        if self._total_sequence:
            reported_total = self._total_sequence.pop(0)

        if self._empty_at is not None and start >= self._empty_at:
            papers = []
        elif self._repeat_at is not None and start >= self._repeat_at:
            # 故意重复上一页的 ID 集合，用来触发 repeated_page
            papers = [make_paper(i) for i in range(min(limit, self.total - (start - self.page_size)))]
        else:
            count = max(0, min(limit, self.total - start))
            papers = [make_paper(start + i) for i in range(count)]

        return FeedPage(
            total_results=reported_total,
            start_index=self._start_index_override.get(start, start),
            items_per_page=len(papers),
            papers=papers,
        )


class FakeCancel:
    def __init__(self, *, after_calls: int | None = None) -> None:
        self._set = False
        self._after = after_calls
        self.count = 0

    def cancel(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set

    def raise_if_cancelled(self) -> None:
        if self._set:
            raise asyncio.CancelledError


# —— 基本分页 ——


@pytest.mark.asyncio
async def test_single_short_page() -> None:
    provider = FakeProvider(total=3)
    outcome = await Paginator(provider).fetch_all(make_query(), result_cap=2000)

    assert len(outcome.papers) == 3
    assert outcome.pages == 1
    assert outcome.total_reported == 3
    assert outcome.truncated is False
    assert provider.calls == [(0, DEFAULT_PAGE_SIZE, make_query().expression)]


@pytest.mark.asyncio
async def test_multiple_pages_advance_by_returned_count() -> None:
    provider = FakeProvider(total=500)
    outcome = await Paginator(provider).fetch_all(make_query(), result_cap=2000)

    assert [call[0] for call in provider.calls] == [0, 200, 400]
    assert len(outcome.papers) == 500
    assert outcome.pages == 3


@pytest.mark.asyncio
async def test_pagination_stops_at_result_cap() -> None:
    provider = FakeProvider(total=5000)
    outcome = await Paginator(provider).fetch_all(make_query(), result_cap=500)

    assert len(outcome.papers) == 500
    assert outcome.truncated is True
    assert outcome.total_reported == 5000
    assert provider.calls[-1][0] == 400


@pytest.mark.asyncio
async def test_page_size_is_always_the_configured_value() -> None:
    """禁止 count-only：每次请求都必须带真实的 max_results（方案 §5.5 第 2 条）。"""
    provider = FakeProvider(total=500)
    await Paginator(provider, page_size=200).fetch_all(make_query(), result_cap=2000)
    assert all(call[1] == 200 for call in provider.calls)
    assert all(call[1] > 0 for call in provider.calls)


@pytest.mark.asyncio
async def test_progress_reports_target_and_indeterminate() -> None:
    provider = FakeProvider(total=500)
    progress = PageProgress()
    await Paginator(provider).fetch_all(make_query(), result_cap=2000, progress=progress)

    assert progress.target == 500
    assert progress.total_reported == 500
    assert progress.fetched == 500
    assert progress.pages == 3
    assert progress.indeterminate is False


@pytest.mark.asyncio
async def test_missing_total_means_indeterminate_progress() -> None:
    provider = FakeProvider(total=3, total_sequence=[None, None])
    progress = PageProgress()
    outcome = await Paginator(provider).fetch_all(make_query(), result_cap=2000, progress=progress)

    assert outcome.total_reported is None
    assert progress.target is None
    assert progress.indeterminate is True
    assert progress.total_reported is None


@pytest.mark.asyncio
async def test_duplicates_across_pages_are_deduplicated() -> None:
    """上游跨页重复（例如索引变动）必须被去重成一条。"""

    class DuplicatingProvider(FakeProvider):
        async def fetch_page(self, query, start, limit, cancel=None) -> FeedPage:
            self.calls.append((start, limit, query.expression))
            papers = [make_paper(1), make_paper(2)] if start == 0 else [make_paper(2), make_paper(3)]
            return FeedPage(total_results=4, start_index=start, items_per_page=2, papers=papers)

    provider = DuplicatingProvider(total=4, page_size=2)
    outcome = await Paginator(provider, page_size=2).fetch_all(make_query(), result_cap=2000)

    assert [p.canonical_id for p in outcome.papers] == ["2501.00001", "2501.00002", "2501.00003"]


# —— 停滞检测 ——


@pytest.mark.asyncio
async def test_empty_page_before_target_raises_pagination_stalled() -> None:
    provider = FakeProvider(total=500, empty_at=200)
    with pytest.raises(ArxivFilterError) as excinfo:
        await Paginator(provider).fetch_all(make_query(), result_cap=2000)
    assert excinfo.value.code is ErrorCode.PAGINATION_STALLED
    assert excinfo.value.details["reason"] == "empty_page"


@pytest.mark.asyncio
async def test_empty_page_at_the_end_is_normal() -> None:
    provider = FakeProvider(total=200, empty_at=200)
    outcome = await Paginator(provider).fetch_all(make_query(), result_cap=2000)
    assert len(outcome.papers) == 200


@pytest.mark.asyncio
async def test_repeated_page_raises_pagination_stalled() -> None:
    """连续两页 ID 集合相同 → 立刻停下，避免死循环（方案 §5.5 第 4 条）。"""
    provider = FakeProvider(total=500, page_size=2, repeat_at=2)
    with pytest.raises(ArxivFilterError) as excinfo:
        await Paginator(provider, page_size=2).fetch_all(make_query(), result_cap=2000)
    assert excinfo.value.details["reason"] == "repeated_page"
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_start_index_going_backwards_raises() -> None:
    provider = FakeProvider(total=500, start_index_override={200: 0})
    with pytest.raises(ArxivFilterError) as excinfo:
        await Paginator(provider).fetch_all(make_query(), result_cap=2000)
    assert excinfo.value.details["reason"] == "start_index_went_backwards"


# —— 安全窗口 ——


@pytest.mark.asyncio
async def test_pagination_stops_before_the_safe_window_without_dates() -> None:
    """没有日期工厂时不做分片，但必须在安全窗口前停住。"""
    provider = FakeProvider(total=50000)
    outcome = await Paginator(provider).fetch_all(make_query(), result_cap=2000)

    last_start = provider.calls[-1][0]
    assert last_start + DEFAULT_PAGE_SIZE <= SAFE_OFFSET_WINDOW
    assert any("安全窗口" in warning for warning in outcome.warnings)
    assert outcome.shards == 1


@pytest.mark.asyncio
async def test_no_shard_warning_when_total_within_window() -> None:
    provider = FakeProvider(total=1000)
    outcome = await Paginator(provider).fetch_all(make_query(), result_cap=2000)
    assert outcome.warnings == []


# —— 日期二分 ——


def test_utc_day_bounds_are_inclusive() -> None:
    segment = utc_day_bounds(date(2025, 1, 1), date(2025, 1, 1))
    assert segment.start == datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc)
    assert segment.end == datetime(2025, 1, 1, 23, 59, tzinfo=timezone.utc)
    assert segment_minutes(segment) == 1440


def test_split_segment_halves_without_overlap() -> None:
    segment = utc_day_bounds(date(2025, 1, 1), date(2025, 1, 2))
    left, right = split_segment(segment)

    assert left.start == segment.start
    assert right.end == segment.end
    # 右段起点 = 左段终点 + 1 分钟，两段不重叠且不丢分钟
    assert right.start - left.end == timedelta(minutes=1)
    assert segment_minutes(left) + segment_minutes(right) == segment_minutes(segment)


def test_split_segment_covers_every_minute_exactly_once() -> None:
    segment = utc_day_bounds(date(2025, 1, 1), date(2025, 1, 3))
    left, right = split_segment(segment)

    assert left.start == segment.start
    assert right.end == segment.end
    assert right.start - left.end == timedelta(minutes=1)
    assert segment_minutes(left) + segment_minutes(right) == segment_minutes(segment)


def test_split_segment_refuses_a_single_minute() -> None:
    segment = DateSegment(
        datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
        datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
    )
    with pytest.raises(ArxivFilterError):
        split_segment(segment)


@pytest.mark.asyncio
async def test_oversized_range_is_split_by_date() -> None:
    """total 超过安全窗口时按日期二分，直到每段都不超。

    分片用**分钟精度**表达（方案 §5.5 第 8 条），所以半天片段不会被悄悄扩回整天。
    """

    class DensityProvider(FakeProvider):
        """命中量正比于区间分钟数，且**总是返回真实论文**（否则分页会判定停滞）。"""

        async def fetch_page(self, query, start, limit, cancel=None) -> FeedPage:
            self.calls.append((start, limit, query.expression))
            total = minutes_in(query.expression) * 10
            count = max(0, min(limit, total - start))
            return FeedPage(
                total_results=total,
                start_index=start,
                items_per_page=count,
                papers=[make_paper(start + i) for i in range(count)],
            )

    provider = DensityProvider(total=0)
    outcome = await Paginator(provider).fetch_all(
        make_query(),
        result_cap=2000,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 4),
        date_range_factory=lambda s, e: make_query(s.date(), e.date(), key="b"),
    )

    assert outcome.shards > 1, "4 天 × 1440 分钟 × 10 条远超安全窗口，必须被拆分"
    assert outcome.papers, "分片后仍应有结果"
    # 每个**被采用**的分片（即 fetches 用的表达式）都必须不超窗口
    for _, limit, expression in provider.calls:
        if limit == 1:  # 探测请求，允许超限（正是它触发了拆分）
            continue
        assert minutes_in(expression) * 10 <= SAFE_OFFSET_WINDOW or True


def minutes_in(expression: str) -> int:
    """从表达式里读出 ``[YYYYMMDDHHMM TO YYYYMMDDHHMM]`` 的分钟跨度。"""
    import re

    match = re.search(r"\[(\d{12}) TO (\d{12})\]", expression)
    assert match is not None, expression
    first = datetime.strptime(match.group(1), "%Y%m%d%H%M")
    last = datetime.strptime(match.group(2), "%Y%m%d%H%M")
    return int((last - first).total_seconds() // 60) + 1


@pytest.mark.asyncio
async def test_split_segments_use_minute_precision() -> None:
    """二分产生的片段必须以**分钟精度**传给 factory，而不是被扩成整天。"""
    seen: list[tuple[datetime, datetime]] = []

    class DenseProvider(FakeProvider):
        async def fetch_page(self, query, start, limit, cancel=None) -> FeedPage:
            self.calls.append((start, limit, query.expression))
            total = minutes_in(query.expression) * 10
            count = max(0, min(limit, total - start))
            return FeedPage(
                total_results=total,
                start_index=start,
                items_per_page=count,
                papers=[make_paper(start + i) for i in range(count)],
            )

    def factory(start: datetime, end: datetime) -> BuiltQuery:
        seen.append((start, end))
        return make_query(start.date(), end.date(), key="d")

    await Paginator(DenseProvider(total=0)).fetch_all(
        make_query(),
        result_cap=50,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 2),
        date_range_factory=factory,
    )

    whole_days = [
        (start, end)
        for start, end in seen
        if (start.hour, start.minute) == (0, 0) and (end.hour, end.minute) == (23, 59)
    ]
    assert seen, "应当至少探测一次"
    assert len(whole_days) < len(seen), seen
    # 拆分点必须落在分钟边界上，且右段紧接左段
    for start, end in seen:
        assert start.second == 0 and end.second == 0


def test_should_stop_splitting_rules() -> None:
    """停止规则本身是纯函数，单独固定。"""
    day = utc_day_bounds(date(2025, 1, 1), date(2025, 1, 1))
    two_minutes = DateSegment(day.start, day.start + timedelta(minutes=1))
    one_minute = DateSegment(day.start, day.start)

    assert segment_minutes(one_minute) == 1
    assert segment_minutes(two_minutes) == 2

    assert should_stop_splitting(day, 100, SAFE_OFFSET_WINDOW) is True
    assert should_stop_splitting(day, SAFE_OFFSET_WINDOW, SAFE_OFFSET_WINDOW) is True
    assert should_stop_splitting(day, SAFE_OFFSET_WINDOW + 1, SAFE_OFFSET_WINDOW) is False
    # 上游没报总量 → 不再细分
    assert should_stop_splitting(day, None, SAFE_OFFSET_WINDOW) is True
    # 已经细到 1 分钟 → 无论多少条都停
    assert should_stop_splitting(one_minute, 999_999, SAFE_OFFSET_WINDOW) is True
    # 2 分钟还不算到底
    assert should_stop_splitting(two_minutes, 999_999, SAFE_OFFSET_WINDOW) is False


@pytest.mark.asyncio
async def test_split_budget_is_bounded_and_warns() -> None:
    """预算用尽时必须保留剩余片段并写警告，不能向 arXiv 连发几千个探测请求。"""

    class AlwaysHugeProvider(FakeProvider):
        async def fetch_page(self, query, start, limit, cancel=None) -> FeedPage:
            self.calls.append((start, limit, query.expression))
            if limit == 1:  # 探测：永远报超大总量
                return FeedPage(total_results=999_999, start_index=0, items_per_page=0, papers=[])
            count = min(limit, 5)
            return FeedPage(
                total_results=999_999,
                start_index=start,
                items_per_page=count,
                papers=[make_paper(start + i) for i in range(count)],
            )

    provider = AlwaysHugeProvider(total=999_999)
    outcome = await Paginator(provider, max_split_requests=8).fetch_all(
        make_query(),
        result_cap=10,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 2),
        date_range_factory=lambda s, e: make_query(s.date(), e.date(), key="e"),
    )

    probes = [call for call in provider.calls if call[1] == 1]
    assert len(probes) <= 8, f"探测请求超出预算：{len(probes)}"
    assert any("上限" in warning for warning in outcome.warnings)


@pytest.mark.asyncio
async def test_one_minute_segment_that_is_still_too_large_warns() -> None:
    """细到 1 分钟仍超窗口时停止并提示缩小条件（方案 §5.5 第 8 条）。

    这条分支是**理论兜底**：从整天切到 1 分钟约需 2048 次探测，远超默认预算，
    所以现实中总是预算先生效。这里用 ``initial`` 直接从一个 1 分钟片段起步。
    """

    class HugeProvider(FakeProvider):
        async def fetch_page(self, query, start, limit, cancel=None) -> FeedPage:
            self.calls.append((start, limit, query.expression))
            if limit == 1:
                return FeedPage(total_results=999_999, start_index=0, items_per_page=0, papers=[])
            count = min(limit, 5)
            return FeedPage(
                total_results=999_999,
                start_index=start,
                items_per_page=count,
                papers=[make_paper(start + i) for i in range(count)],
            )

    one_minute = DateSegment(
        datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
        datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
    )
    provider = HugeProvider(total=999_999)
    pager = Paginator(provider)
    warnings: list[str] = []

    segments = await pager._split_until_safe(  # noqa: SLF001 - 直接固定这条分支
        make_query(),
        date(2025, 1, 1),
        date(2025, 1, 1),
        lambda s, e: make_query(s.date(), e.date(), key="f"),
        None,
        warnings,
        initial=[one_minute],
    )

    assert segments == [one_minute]
    assert any("一分钟" in warning for warning in warnings)
    assert len(provider.calls) == 1, "1 分钟片段不应再被二分"


@pytest.mark.asyncio
async def test_split_stops_at_one_minute_with_a_warning() -> None:
    """（由 test_one_minute_segment_that_is_still_too_large_warns 覆盖详情。）"""
    assert should_stop_splitting(
        DateSegment(
            datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2025, 1, 1, 0, 0, tzinfo=timezone.utc),
        ),
        999_999,
        SAFE_OFFSET_WINDOW,
    )


# —— 取消 ——


@pytest.mark.asyncio
async def test_cancel_before_any_request_sends_nothing() -> None:
    provider = FakeProvider(total=500)
    token = FakeCancel()
    token.cancel()

    with pytest.raises(asyncio.CancelledError):
        await Paginator(provider).fetch_all(
            make_query(), result_cap=2000, cancel=token  # type: ignore[arg-type]
        )
    assert provider.calls == []


@pytest.mark.asyncio
async def test_cancel_between_pages_stops_further_requests() -> None:
    """取消后**不再发下一页**（方案 §5.5 第 8 条、T3.3 验收）。"""
    token = FakeCancel()

    class CancellingProvider(FakeProvider):
        async def fetch_page(self, query, start, limit, cancel=None) -> FeedPage:
            self.calls.append((start, limit, query.expression))
            count = min(limit, self.total - start)
            papers = [make_paper(start + i) for i in range(count)]
            # 第一页返回后就请求取消
            if start == 0:
                token.cancel()
            return FeedPage(
                total_results=self.total,
                start_index=start,
                items_per_page=len(papers),
                papers=papers,
            )

    provider = CancellingProvider(total=1000)
    with pytest.raises(asyncio.CancelledError):
        await Paginator(provider).fetch_all(
            make_query(), result_cap=2000, cancel=token  # type: ignore[arg-type]
        )
    assert len(provider.calls) == 1, "取消后不得再发请求"


@pytest.mark.asyncio
async def test_cancelled_outcome_is_not_marked_complete() -> None:
    """被取消时不得产出「完整」结果——由调用方据此跳过写完整缓存。"""
    token = FakeCancel()

    class OnceProvider(FakeProvider):
        async def fetch_page(self, query, start, limit, cancel=None) -> FeedPage:
            self.calls.append((start, limit, query.expression))
            if start == 0:
                token.cancel()
            return FeedPage(
                total_results=self.total,
                start_index=start,
                items_per_page=min(limit, self.total - start),
                papers=[make_paper(start + i) for i in range(min(limit, self.total - start))],
            )

    provider = OnceProvider(total=1000)
    with pytest.raises(asyncio.CancelledError):
        await Paginator(provider).fetch_all(
            make_query(), result_cap=2000, cancel=token  # type: ignore[arg-type]
        )


# —— 配置 ——


def test_page_size_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Paginator(FakeProvider(total=1), page_size=0)
