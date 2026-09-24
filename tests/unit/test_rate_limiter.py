"""T3.1 限速器（方案 §5.4）。

用注入的 fake clock + fake sleeper 做**确定性**断言，不真实 sleep。
"""

from __future__ import annotations

import asyncio

import pytest

from arxiv_filter.arxiv.rate_limiter import (
    CANCEL_POLL_INTERVAL_S,
    DEFAULT_INTERVAL_S,
    DOCUMENTED_INTERVAL_S,
    RateLimiter,
    configure_rate_limiter,
    get_rate_limiter,
)


class FakeClock:
    """可推进的单调时钟 + 与之联动的异步 sleeper。"""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        # 让出控制权，保持与真实 asyncio.sleep 一致的调度语义
        await asyncio.sleep(0)


class FakeCancelToken:
    """按协议实现的取消标记。"""

    def __init__(self) -> None:
        self._set = False

    def cancel(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set

    def raise_if_cancelled(self) -> None:
        if self._set:
            raise asyncio.CancelledError


def make_limiter(**kwargs: object) -> tuple[RateLimiter, FakeClock]:
    clock = FakeClock()
    limiter = RateLimiter(
        kwargs.pop("min_interval_s", DEFAULT_INTERVAL_S),  # type: ignore[arg-type]
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    return limiter, clock


# —— 配置边界 ——


def test_default_interval_is_four_seconds() -> None:
    """方案原文的 3.1 秒经实测不足，已改成 4.0 秒。"""
    assert DEFAULT_INTERVAL_S == 4.0
    assert DEFAULT_INTERVAL_S > DOCUMENTED_INTERVAL_S


def test_interval_below_the_documented_floor_is_refused() -> None:
    with pytest.raises(ValueError, match="不能低于官方下限"):
        RateLimiter(2.0)


def test_interval_equal_to_the_documented_floor_is_allowed() -> None:
    """3.0 秒是官方下限，允许显式配置，但不会是默认值。"""
    assert RateLimiter(DOCUMENTED_INTERVAL_S).min_interval_s == 3.0


# —— 间隔 ——


@pytest.mark.asyncio
async def test_first_request_is_not_delayed() -> None:
    limiter, clock = make_limiter()
    assert await limiter.acquire() == 0.0
    assert clock.sleeps == []
    assert limiter.grants == 1


@pytest.mark.asyncio
async def test_ten_requests_are_spaced_by_at_least_the_interval() -> None:
    """方案 T3.1 验收：10 次请求起点差 ≥ 间隔。"""
    limiter, clock = make_limiter()
    starts: list[float] = []
    for _ in range(10):
        await limiter.acquire()
        starts.append(clock.monotonic())

    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(gaps) == 9
    assert all(gap >= DEFAULT_INTERVAL_S - 1e-9 for gap in gaps), gaps
    assert limiter.min_observed_interval() == pytest.approx(DEFAULT_INTERVAL_S)


@pytest.mark.asyncio
async def test_no_wait_when_enough_time_already_passed() -> None:
    limiter, clock = make_limiter()
    await limiter.acquire()
    clock.now += 60.0
    assert await limiter.acquire() == 0.0
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_partial_wait_when_some_time_passed() -> None:
    limiter, clock = make_limiter()
    await limiter.acquire()
    clock.now += 1.5
    waited = await limiter.acquire()
    assert waited == pytest.approx(DEFAULT_INTERVAL_S - 1.5)


@pytest.mark.asyncio
async def test_early_waking_sleeper_is_retried_via_deadline_loop() -> None:
    """单次 sleep 可能提前返回；deadline 循环必须补足剩余时间。

    这是实测踩过的坑：目标 3.1 秒只跑出 3.093 秒。
    """

    class EarlyWakingClock(FakeClock):
        async def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            # 只推进 60%，模拟 sleep 提前返回
            self.now += seconds * 0.6
            await asyncio.sleep(0)

    clock = EarlyWakingClock()
    limiter = RateLimiter(DEFAULT_INTERVAL_S, monotonic=clock.monotonic, sleeper=clock.sleep)

    start = clock.now
    await limiter.acquire()
    await limiter.acquire()
    assert clock.now - start >= DEFAULT_INTERVAL_S
    assert len(clock.sleeps) > 1, "应当分多轮补足"


@pytest.mark.asyncio
async def test_custom_interval_is_respected() -> None:
    limiter, clock = make_limiter(min_interval_s=10.0)
    await limiter.acquire()
    start = clock.now
    await limiter.acquire()
    assert clock.now - start == pytest.approx(10.0)


# —— 取消 ——


@pytest.mark.asyncio
async def test_cancel_before_waiting_is_immediate() -> None:
    limiter, clock = make_limiter()
    token = FakeCancelToken()
    token.cancel()
    with pytest.raises(asyncio.CancelledError):
        await limiter.acquire(token)
    assert limiter.grants == 0
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_cancel_during_wait_ends_within_100ms_of_poll_granularity() -> None:
    """方案 T3.1 验收：等待中取消要在 100 ms 内结束。

    用 polling 粒度反推：单次可取消 sleep 的分片不能超过 100 ms。
    """
    assert CANCEL_POLL_INTERVAL_S <= 0.1

    limiter, clock = make_limiter()
    await limiter.acquire()

    token = FakeCancelToken()

    class CancellingClock(FakeClock):
        async def sleep(self, seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds
            # 第一片之后就请求取消
            token.cancel()
            await asyncio.sleep(0)

    clock2 = CancellingClock()
    limiter2 = RateLimiter(
        DEFAULT_INTERVAL_S, monotonic=clock2.monotonic, sleeper=clock2.sleep
    )
    await limiter2.acquire()

    elapsed_before_cancel = clock2.now
    with pytest.raises(asyncio.CancelledError):
        await limiter2.acquire(token)

    # 最多睡过一片就应当抛出
    overshoot = clock2.now - elapsed_before_cancel
    assert overshoot <= CANCEL_POLL_INTERVAL_S + 1e-9, overshoot
    assert limiter2.grants == 1, "被取消的 acquire 不应消耗名额"


@pytest.mark.asyncio
async def test_cancelled_acquire_does_not_advance_the_schedule() -> None:
    """取消不能推迟后续合法请求，也不能抢占名额。"""
    limiter, clock = make_limiter()
    await limiter.acquire()

    token = FakeCancelToken()
    token.cancel()

    clock.now += DEFAULT_INTERVAL_S
    with pytest.raises(asyncio.CancelledError):
        await limiter.acquire(token)

    # 取消后仍可正常取名额，且不需要额外等待
    assert await limiter.acquire() == 0.0
    assert limiter.grants == 2


# —— 并发 ——


@pytest.mark.asyncio
async def test_concurrent_acquires_are_serialised() -> None:
    """10 个并发调用必须被串行化，起点差全部达标。"""
    clock = FakeClock()
    limiter = RateLimiter(DEFAULT_INTERVAL_S, monotonic=clock.monotonic, sleeper=clock.sleep)

    starts: list[float] = []

    async def worker() -> None:
        await limiter.acquire()
        starts.append(clock.monotonic())

    await asyncio.gather(*(worker() for _ in range(10)))

    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(gap >= DEFAULT_INTERVAL_S - 1e-9 for gap in gaps), gaps
    assert limiter.grants == 10


# —— 单例 ——


def test_get_rate_limiter_is_a_singleton() -> None:
    configure_rate_limiter(None)
    try:
        assert get_rate_limiter() is get_rate_limiter()
    finally:
        configure_rate_limiter(None)


def test_configure_rate_limiter_replaces_the_singleton() -> None:
    configure_rate_limiter(None)
    custom = RateLimiter(5.0)
    try:
        configure_rate_limiter(custom)
        assert get_rate_limiter() is custom
    finally:
        configure_rate_limiter(None)


def test_fresh_singleton_uses_the_four_second_default() -> None:
    configure_rate_limiter(None)
    try:
        assert get_rate_limiter().min_interval_s == DEFAULT_INTERVAL_S
    finally:
        configure_rate_limiter(None)
