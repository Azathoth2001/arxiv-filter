"""``MetadataProvider`` / ``CancelToken`` 协议契约（方案 §4.2、§5.8）。

这两个模块只有接口没有实现，覆盖率天然是 0；这里验证的是**协议本身可用**：
第三方 provider 只要照这个形状实现，就能在搜索编排里替换掉 arXiv 适配器。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from arxiv_filter.arxiv.provider import CancelToken, MetadataProvider
from arxiv_filter.models.query import BuiltQuery, FeedPage


class AsyncioCancelToken:
    """按方案 §5.4 实现的取消标记。"""

    def __init__(self, event: asyncio.Event | None = None) -> None:
        self._event = event or asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise asyncio.CancelledError


class StubProvider:
    """一个最小的 provider 实现，用来验证协议是否可实现。"""

    name = "stub"

    def __init__(self, page: FeedPage) -> None:
        self._page = page
        self.calls: list[tuple[int, int]] = []

    async def fetch_page(
        self,
        query: BuiltQuery,
        start: int,
        limit: int,
        cancel: CancelToken,
    ) -> FeedPage:
        cancel.raise_if_cancelled()
        self.calls.append((start, limit))
        return self._page


def make_query() -> BuiltQuery:
    return BuiltQuery(
        category_ids=["cs.CL"],
        expression="cat:cs.CL",
        sort_by="submittedDate",
        sort_order="descending",
        canonical_json="{}",
        cache_key="a" * 64,
    )


def test_stub_provider_satisfies_the_protocol() -> None:
    provider = StubProvider(FeedPage(total_results=0, start_index=0, items_per_page=0))
    assert isinstance(provider, MetadataProvider)


def test_provider_protocol_requires_name() -> None:
    class Nameless:
        async def fetch_page(self, query, start, limit, cancel):  # type: ignore[no-untyped-def]
            return FeedPage(total_results=0, start_index=0, items_per_page=0)

    assert not isinstance(Nameless(), MetadataProvider)


def test_cancel_token_satisfies_the_protocol() -> None:
    assert isinstance(AsyncioCancelToken(), CancelToken)


@pytest.mark.asyncio
async def test_provider_is_awaitable_and_records_pagination() -> None:
    page = FeedPage(total_results=1, start_index=0, items_per_page=1)
    provider = StubProvider(page)
    result = await provider.fetch_page(make_query(), 0, 200, AsyncioCancelToken())
    assert result is page
    assert provider.calls == [(0, 200)]


@pytest.mark.asyncio
async def test_cancelled_token_stops_the_fetch() -> None:
    token = AsyncioCancelToken()
    token.cancel()
    assert token.is_set() is True
    provider = StubProvider(FeedPage(total_results=0, start_index=0, items_per_page=0))
    with pytest.raises(asyncio.CancelledError):
        await provider.fetch_page(make_query(), 0, 200, token)
    assert provider.calls == []


def test_cancel_token_raise_if_cancelled_is_a_noop_when_clear() -> None:
    assert AsyncioCancelToken().raise_if_cancelled() is None


def test_feed_page_defaults_support_indeterminate_progress() -> None:
    page = FeedPage(start_index=0, items_per_page=0)
    assert page.total_results is None
    assert page.warnings == []
    assert page.feed_updated_at is None


def test_feed_page_carries_parse_warnings() -> None:
    page = FeedPage(
        total_results=1,
        start_index=0,
        items_per_page=1,
        warnings=["缺 primary_category"],
        feed_updated_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
    )
    assert page.warnings == ["缺 primary_category"]
