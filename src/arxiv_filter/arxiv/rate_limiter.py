"""全进程唯一的请求限速器（方案 §5.4 T3.1）。

硬规则（方案 §5.4、``docs/decisions.md`` 的实测修正）：

* **任意两次「实际开始发送」的 monotonic 时间差至少** ``min_interval_s``；
* 默认 **4.0 秒**。方案原文写的 3.1 秒经 2026-09-24 实测不足：贴着实测到的最小起点间隔
  只有 3.093 秒（``asyncio.sleep`` 同样会提前返回），而官方下限是 3 秒，
  余量会被网络抖动吃光并触发上游 HTTP 406 限流；
* 用 **deadline 循环**而不是单次 sleep——单次 sleep 在 Windows 上可能提前返回；
* 等待限速名额期间必须能被取消，且要在 ~100 ms 内结束（方案 §4.2「等待中取消」）。

本模块**只**管节奏，不管重试、状态码或 I/O；那些属于 ``client.py``。
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from arxiv_filter.arxiv.provider import CancelToken

#: 官方文档下限：每 3 秒最多 1 请求。
DOCUMENTED_INTERVAL_S = 3.0

#: 应用默认间隔。见模块 docstring 的实测说明。
DEFAULT_INTERVAL_S = 4.0

#: 取消轮询粒度：决定「等待中取消」的响应速度上限。
CANCEL_POLL_INTERVAL_S = 0.05

#: deadline 循环的迭代上界，防止时钟异常导致死循环。
_MAX_SLEEP_ITERATIONS = 32

#: deadline 判定容差（秒）。
#:
#: 必须用容差而不是 ``remaining <= 0``：deadline 循环每轮只睡掉「剩余量的一部分」，
#: 剩余量按几何级数缩小，但浮点累加会让它停在 ~1e-13 而**永远不恰好等于 0**，
#: 于是循环耗尽上界后误报「时钟异常」。1 微秒远低于限速所需精度。
_DEADLINE_EPSILON_S = 1e-6


class RateLimiter:
    """串行化并拉开请求起点间隔的限速器。

    参数
    ----
    min_interval_s:
        两次发送起点之间的最小间隔秒数。
    monotonic:
        单调时钟；测试注入 fake clock 用。
    sleeper:
        可等待的 sleep 函数；测试注入 fake 版本用。
    """

    __slots__ = (
        "_grants",
        "_last_grant_at",
        "_lock",
        "_min_interval_s",
        "_monotonic",
        "_observed_intervals",
        "_sleep",
    )

    def __init__(
        self,
        min_interval_s: float = DEFAULT_INTERVAL_S,
        *,
        monotonic: Callable[[], float] | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if min_interval_s < DOCUMENTED_INTERVAL_S:
            raise ValueError(
                f"限速间隔不能低于官方下限 {DOCUMENTED_INTERVAL_S} 秒；"
                f"实测贴边会被上游以 HTTP 406 拒绝。收到 {min_interval_s}。"
            )
        self._min_interval_s = float(min_interval_s)
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleeper or asyncio.sleep
        self._lock = asyncio.Lock()
        self._last_grant_at: float | None = None
        self._grants = 0
        self._observed_intervals: list[float] = []

    # —— 只读属性 ——

    @property
    def min_interval_s(self) -> float:
        return self._min_interval_s

    @property
    def grants(self) -> int:
        """已发放的发送名额数。"""
        return self._grants

    @property
    def last_grant_at(self) -> float | None:
        return self._last_grant_at

    def min_observed_interval(self) -> float | None:
        """已发放名额之间观察到的最小起点间隔；只有一个名额时为 ``None``。"""
        if not self._observed_intervals:
            return None
        return min(self._observed_intervals)

    def observed_intervals(self) -> list[float]:
        return list(self._observed_intervals)

    # —— 核心 ——

    async def acquire(self, cancel: CancelToken | None = None) -> float:
        """阻塞到可以发送，返回本次实际等待的秒数。

        并发调用会被串行化，且相邻两次返回时刻之差 **≥** ``min_interval_s``
        （严格成立，不是近似——deadline 里已经含了退出判据的容差）。

        ``cancel`` 已置位或等待期间被置位时抛 :class:`asyncio.CancelledError`，
        并且**不会**消耗名额。
        """
        if cancel is not None:
            cancel.raise_if_cancelled()

        async with self._lock:
            waited = await self._wait_for_slot(cancel)
            now = self._monotonic()
            if self._last_grant_at is not None:
                self._observed_intervals.append(now - self._last_grant_at)
            self._last_grant_at = now
            self._grants += 1
            return waited

    async def _wait_for_slot(self, cancel: CancelToken | None) -> float:
        """睡到 deadline 真正过去为止；返回**实际**流逝的秒数。"""
        if self._last_grant_at is None:
            return 0.0

        # deadline 加上容差，抵消退出判据的容差，使「间隔 ≥ min_interval」严格成立
        deadline = self._last_grant_at + self._min_interval_s + _DEADLINE_EPSILON_S
        started = self._monotonic()

        for _ in range(_MAX_SLEEP_ITERATIONS):
            remaining = deadline - self._monotonic()
            if remaining <= _DEADLINE_EPSILON_S:
                return self._monotonic() - started
            await self._cancellable_sleep(remaining, cancel)

        # 时钟异常导致一直没到 deadline：不静默放过，避免发出超速请求
        remaining = deadline - self._monotonic()
        if remaining > _DEADLINE_EPSILON_S:  # pragma: no cover - 需要极异常的时钟才能触发
            raise RuntimeError(
                f"限速器等待 {_MAX_SLEEP_ITERATIONS} 轮后仍未到达发送时刻，时钟可能异常。"
            )
        return self._monotonic() - started

    async def _cancellable_sleep(self, seconds: float, cancel: CancelToken | None) -> None:
        await cancellable_sleep(seconds, cancel, sleeper=self._sleep)


async def cancellable_sleep(
    seconds: float,
    cancel: CancelToken | None = None,
    *,
    sleeper: Callable[[float], Awaitable[None]] | None = None,
) -> None:
    """可取消的 sleep，供限速器排队与请求退避共用。

    不用「把 sleep 与 cancel 事件竞速」的写法，因为 ``CancelToken`` 是协议而不是
    ``asyncio.Event``。改为按 ``CANCEL_POLL_INTERVAL_S`` 分片轮询——
    这让取消延迟有确定上界，正好满足方案 §4.2 的「等待中取消在 100 ms 内结束」。
    """
    sleep = sleeper or asyncio.sleep

    if cancel is None:
        await sleep(seconds)
        return

    remaining = seconds
    while remaining > 0:
        cancel.raise_if_cancelled()
        slice_s = min(remaining, CANCEL_POLL_INTERVAL_S)
        await sleep(slice_s)
        remaining -= slice_s
    cancel.raise_if_cancelled()


# —— 全进程单例 ——

_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    """取全进程唯一的限速器。

    方案 §5.4 要求「所有首次请求和 retry 共享同一个 limiter」——
    因此**不要**在别处直接构造 ``RateLimiter``，一律走这个入口。
    """
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter


def configure_rate_limiter(limiter: RateLimiter | None) -> None:
    """替换（或清空）全进程单例，供测试与高级设置使用。"""
    global _limiter
    _limiter = limiter


__all__ = [
    "CANCEL_POLL_INTERVAL_S",
    "DEFAULT_INTERVAL_S",
    "DOCUMENTED_INTERVAL_S",
    "RateLimiter",
    "cancellable_sleep",
    "configure_rate_limiter",
    "get_rate_limiter",
]
