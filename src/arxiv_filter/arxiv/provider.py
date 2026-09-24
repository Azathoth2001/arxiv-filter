"""元数据来源接口（方案 §4.2、§5.8）。

把上游访问收敛到一个 Protocol 后面，是为了让「legacy API 被替换」这个风险
（方案第 9 部分第一行）只需要新增一个适配器，而不是改动搜索编排。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from arxiv_filter.models.query import BuiltQuery, FeedPage


@runtime_checkable
class CancelToken(Protocol):
    """可取消标记。

    方案 §5.4 要求取消能打断三处等待：限速器排队、退避 sleep、在途 HTTP 请求。
    因此调用方拿到的是这个协议而不是裸的 ``asyncio.Event``——
    ``raise_if_cancelled`` 让深层的等待点自己抛出，不用层层传事件对象。
    """

    def is_set(self) -> bool:
        """是否已请求取消。"""
        ...

    def raise_if_cancelled(self) -> None:
        """已请求取消时抛出 ``asyncio.CancelledError``。"""
        ...


@runtime_checkable
class MetadataProvider(Protocol):
    """论文元数据来源。

    实现必须保证：

    * 每次调用（含内部重试）都经过**全进程唯一**的限速器；
    * ``start`` / ``limit`` 直接对应上游分页参数，不做隐式放大；
    * 抛出 :class:`~arxiv_filter.errors.ArxivFilterError`，而不是裸的 httpx 异常；
    * 取消时不写任何「完整」缓存。
    """

    @property
    def name(self) -> str:
        """来源标识，写入 :attr:`~arxiv_filter.models.paper.Paper.source`。"""
        ...

    async def fetch_page(
        self,
        query: BuiltQuery,
        start: int,
        limit: int,
        cancel: CancelToken,
    ) -> FeedPage:
        """取一页结果。"""
        ...


__all__ = ["CancelToken", "MetadataProvider"]
