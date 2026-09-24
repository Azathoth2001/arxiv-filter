"""T3.2 arXiv 客户端（方案 §5.4）。

全程离线：HTTP 用 ``respx`` 冻结，时间用注入的 fake clock/limiter，
不真实 sleep、不真实联网。
"""

from __future__ import annotations

import asyncio
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from arxiv_filter.arxiv.client import (
    API_URL,
    DEFAULT_BACKOFF,
    DEFAULT_PAGE_SIZE,
    MAX_RETRY_AFTER_S,
    ArxivClient,
    ClientConfig,
    is_dns_error,
    is_proxy_error,
    is_tls_error,
    parse_retry_after,
)
from arxiv_filter.arxiv.rate_limiter import RateLimiter
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.query import BuiltQuery
from arxiv_filter.taxonomy import load_taxonomy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "atom"
ATOM_OK = (FIXTURES / "normal.xml").read_bytes()
ATOM_EMPTY = (FIXTURES / "empty.xml").read_bytes()
ATOM_ERROR = (FIXTURES / "api-error.xml").read_bytes()

ATOM_HEADERS = {"content-type": "application/atom+xml; charset=utf-8"}
HTML_HEADERS = {"content-type": "text/html; charset=utf-8"}


class FakeClock:
    """可推进的单调时钟 + 异步 sleeper。"""

    def __init__(self) -> None:
        self.now = 5000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


class RecordingSleeper:
    """只记录、不等待的 sleeper。

    **必须**注入客户端：默认的 ``asyncio.sleep`` 会让覆盖重试路径的测试
    真实等待 3.2 / 6.4 / 12.8 / 30 秒——实测过一次 317 秒的测试套件。
    """

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        await asyncio.sleep(0)


class FakeCancel:
    def __init__(self, *, set_: bool = False) -> None:
        self._set = set_

    def cancel(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set

    def raise_if_cancelled(self) -> None:
        if self._set:
            raise asyncio.CancelledError


class ZeroJitter:
    def uniform(self, a: float, b: float) -> float:  # noqa: ARG002
        return 0.0


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def backoff_delays(client: ArxivClient) -> list[float]:
    """取出客户端记录的退避等待序列。"""
    return client._sleep.delays  # type: ignore[attr-defined]  # noqa: SLF001


def make_query() -> BuiltQuery:
    from datetime import date

    from arxiv_filter.models.query import SearchRequest
    from arxiv_filter.arxiv.query_builder import build_query

    return build_query(
        SearchRequest(
            group_id="cs",
            category_id="cs.CL",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 7),
        ),
        taxonomy=load_taxonomy(),
    )


def make_client(
    clock: FakeClock, *, sleeper: RecordingSleeper | None = None, **config_overrides: object
) -> ArxivClient:
    """构造测试用客户端。

    limiter 用虚拟时钟 ``clock``，退避 sleep 用 ``RecordingSleeper``（不等待）。
    两者刻意分开：否则退避断言会被限速等待混进来，而且会真实睡满 3.2/6.4/12.8/30 秒。
    """
    config = ClientConfig(jitter_s=0.0, **config_overrides)  # type: ignore[arg-type]
    limiter = RateLimiter(3.0, monotonic=clock.monotonic, sleeper=clock.sleep)
    return ArxivClient(
        limiter=limiter,
        config=config,
        taxonomy=load_taxonomy(),
        rng=ZeroJitter(),  # type: ignore[arg-type]
        sleeper=sleeper or RecordingSleeper(),  # type: ignore[arg-type]
    )


# —— Retry-After 解析 ——


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("30", 30.0), ("0", 0.0), ("1.5", 1.5), (" 12 ", 12.0), (None, None), ("", None)],
)
def test_parse_retry_after_accepts_plain_seconds(raw: str | None, expected: float | None) -> None:
    assert parse_retry_after(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["-1", "-0.5", "999999", "Wed, 21 Oct 2026 07:28:00 GMT", "abc", "1e999"],
)
def test_parse_retry_after_rejects_untrustworthy_values(raw: str) -> None:
    """HTTP-date、负数、超大值都不可信——宁可按默认值保守等待。"""
    assert parse_retry_after(raw) is None


def test_retry_after_upper_bound_is_documented() -> None:
    assert parse_retry_after(str(MAX_RETRY_AFTER_S)) == MAX_RETRY_AFTER_S
    assert parse_retry_after(str(MAX_RETRY_AFTER_S + 1)) is None


# —— 异常分类辅助 ——


def test_dns_error_detection() -> None:
    assert is_dns_error(httpx.ConnectError("[Errno 11001] getaddrinfo failed")) is True
    assert is_dns_error(
        httpx.ConnectError("[Errno -2] Name or service not known")
    ) is True
    assert is_dns_error(httpx.ConnectError("Connection refused")) is False


def test_tls_error_detection() -> None:
    assert is_tls_error(ssl.SSLCertVerificationError("certificate verify failed")) is True
    assert is_tls_error(httpx.ConnectError("connection refused")) is False


def test_proxy_error_detection() -> None:
    assert is_proxy_error(httpx.ProxyError("407 Proxy Authentication Required")) is True
    assert is_proxy_error(httpx.ConnectError("connection refused")) is False


# —— 正常路径 ——


@pytest.mark.asyncio
@respx.mock
async def test_successful_fetch_parses_the_feed(clock: FakeClock) -> None:
    route = respx.get(API_URL).mock(return_value=httpx.Response(200, content=ATOM_OK, headers=ATOM_HEADERS))
    client = make_client(clock)
    try:
        page = await client.fetch_page(make_query(), 0, DEFAULT_PAGE_SIZE)
    finally:
        await client.aclose()

    assert route.call_count == 1
    assert len(page.papers) == 3
    assert page.total_results == 3
    assert page.papers[0].canonical_id == "1706.03762"


@pytest.mark.asyncio
@respx.mock
async def test_zero_result_feed_is_not_an_error(clock: FakeClock) -> None:
    respx.get(API_URL).mock(return_value=httpx.Response(200, content=ATOM_EMPTY, headers=ATOM_HEADERS))
    client = make_client(clock)
    try:
        page = await client.fetch_page(make_query(), 0, DEFAULT_PAGE_SIZE)
    finally:
        await client.aclose()
    assert page.papers == []
    assert page.total_results == 0


@pytest.mark.asyncio
@respx.mock
async def test_request_parameters_match_the_plan(clock: FakeClock) -> None:
    """参数必须原样交给 httpx：不预编码、不改造。"""
    route = respx.get(API_URL).mock(return_value=httpx.Response(200, content=ATOM_OK, headers=ATOM_HEADERS))
    client = make_client(clock)
    try:
        await client.fetch_page(make_query(), 0, 200)
    finally:
        await client.aclose()

    request = route.calls[0].request
    assert request.url.params["max_results"] == "200"
    assert request.url.params["start"] == "0"
    assert request.url.params["sortBy"] == "submittedDate"
    # search_query 是 URL 编码后的形式，解码回来应当等于原始表达式
    assert "cat:cs.CL" in request.url.params["search_query"]


@pytest.mark.asyncio
@respx.mock
async def test_client_uses_exactly_one_connection(clock: FakeClock) -> None:
    client = make_client(clock)
    try:
        assert client.connection_limits == (1, 1)
    finally:
        await client.aclose()


# —— 重试与退避 ——


@pytest.mark.asyncio
@respx.mock
async def test_retryable_5xx_is_retried_with_the_documented_backoff(clock: FakeClock) -> None:
    respx.get(API_URL).mock(
        side_effect=[
            httpx.Response(503, text="unavailable", headers=HTML_HEADERS),
            httpx.Response(503, text="unavailable", headers=HTML_HEADERS),
            httpx.Response(200, content=ATOM_OK, headers=ATOM_HEADERS),
        ]
    )
    client = make_client(clock)
    try:
        page = await client.fetch_page(make_query(), 0, 200)
    finally:
        await client.aclose()

    assert len(page.papers) == 3
    assert backoff_delays(client) == [DEFAULT_BACKOFF[0], DEFAULT_BACKOFF[1]]


@pytest.mark.asyncio
@respx.mock
async def test_retries_exhaust_then_trip_the_circuit(clock: FakeClock) -> None:
    route = respx.get(API_URL).mock(
        return_value=httpx.Response(500, text="boom", headers=HTML_HEADERS)
    )
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
        assert excinfo.value.code is ErrorCode.ARXIV_UNAVAILABLE
        assert route.call_count == 4, "初次 + 3 次重试"
        assert backoff_delays(client) == list(DEFAULT_BACKOFF)
        assert client.circuit_open is True

        # 熔断期间直接快速失败，不再发请求
        with pytest.raises(ArxivFilterError) as second:
            await client.fetch_page(make_query(), 0, 200)
        assert second.value.details["reason"] == "circuit_open"
        assert route.call_count == 4
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_client_errors_are_not_retried(clock: FakeClock) -> None:
    route = respx.get(API_URL).mock(
        return_value=httpx.Response(404, text="not found", headers=HTML_HEADERS)
    )
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
        assert excinfo.value.code is ErrorCode.ARXIV_QUERY_CONTRACT
        assert route.call_count == 1
        assert backoff_delays(client) == []
    finally:
        await client.aclose()


# —— 限流：429 与 406 ——


@pytest.mark.asyncio
@respx.mock
async def test_429_honours_retry_after(clock: FakeClock) -> None:
    respx.get(API_URL).mock(
        side_effect=[
            httpx.Response(429, text="slow down", headers={"retry-after": "42"}),
            httpx.Response(200, content=ATOM_OK, headers=ATOM_HEADERS),
        ]
    )
    client = make_client(clock)
    try:
        page = await client.fetch_page(make_query(), 0, 200)
    finally:
        await client.aclose()
    assert len(page.papers) == 3
    assert backoff_delays(client) == [42.0]


@pytest.mark.asyncio
@respx.mock
async def test_429_without_retry_after_waits_thirty_seconds(clock: FakeClock) -> None:
    respx.get(API_URL).mock(
        side_effect=[
            httpx.Response(429, text="slow down"),
            httpx.Response(200, content=ATOM_OK, headers=ATOM_HEADERS),
        ]
    )
    client = make_client(clock)
    try:
        await client.fetch_page(make_query(), 0, 200)
        assert backoff_delays(client) == [30.0]
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_406_is_treated_as_rate_limiting_not_syntax_error(clock: FakeClock) -> None:
    """2026-09-24 实测：406 空响应体是限流信号（见 docs/api-verification.md §3）。

    绝不能归类成 INVALID_KEYWORD_SYNTAX 或 ARXIV_QUERY_CONTRACT——
    那会让用户以为是自己关键词写错了。
    """
    respx.get(API_URL).mock(
        side_effect=[
            httpx.Response(406),
            httpx.Response(200, content=ATOM_OK, headers=ATOM_HEADERS),
        ]
    )
    client = make_client(clock)
    try:
        page = await client.fetch_page(make_query(), 0, 200)
    finally:
        await client.aclose()

    assert len(page.papers) == 3
    assert backoff_delays(client) == [30.0]


@pytest.mark.asyncio
@respx.mock
async def test_persistent_406_raises_rate_limit_and_trips_the_circuit(clock: FakeClock) -> None:
    route = respx.get(API_URL).mock(return_value=httpx.Response(406))
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
        error = excinfo.value
        assert error.code is ErrorCode.ARXIV_NOT_ACCEPTABLE
        assert error.retryable is True
        assert error.details["interpreted_as"] == "rate_limit"
        assert not isinstance(error.details.get("reason"), str) or (
            error.details.get("reason") != "invalid_keywords"
        )
        assert route.call_count == 2, "只多试 1 次"
        assert client.circuit_open is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_406_user_message_does_not_blame_the_keywords(clock: FakeClock) -> None:
    respx.get(API_URL).mock(return_value=httpx.Response(406))
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
    finally:
        await client.aclose()
    message = excinfo.value.message_zh
    assert "406" not in message
    assert "关键词" not in message
    assert "频繁" in message


# —— 响应形状问题 ——


@pytest.mark.asyncio
@respx.mock
async def test_atom_error_entry_is_not_retried(clock: FakeClock) -> None:
    route = respx.get(API_URL).mock(
        return_value=httpx.Response(200, content=ATOM_ERROR, headers=ATOM_HEADERS)
    )
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
        assert excinfo.value.code is ErrorCode.ARXIV_ATOM_ERROR
        assert route.call_count == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_atom_error_behind_a_500_is_not_retried(clock: FakeClock) -> None:
    """500 也可能是 Atom error entry——先看内容再决定要不要重试。"""
    route = respx.get(API_URL).mock(
        return_value=httpx.Response(500, content=ATOM_ERROR, headers=ATOM_HEADERS)
    )
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
        assert excinfo.value.code is ErrorCode.ARXIV_ATOM_ERROR
        assert excinfo.value.details["upstream_summary"] == "incorrect id format for 0"
        assert route.call_count == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_html_error_page_on_200_is_rejected(clock: FakeClock) -> None:
    """200 配 HTML 正文（代理劫持/认证页）必须报错，不能当 feed 解析。"""
    respx.get(API_URL).mock(
        return_value=httpx.Response(200, text="<html>portal login</html>", headers=HTML_HEADERS)
    )
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
        assert excinfo.value.code is ErrorCode.ARXIV_BAD_RESPONSE
        assert excinfo.value.details["reason"] == "non_xml_content_type"
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_oversized_response_is_rejected(clock: FakeClock) -> None:
    client = make_client(clock, max_body_bytes=128)
    respx.get(API_URL).mock(return_value=httpx.Response(200, content=ATOM_OK, headers=ATOM_HEADERS))
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
        assert excinfo.value.details["reason"] == "response_too_large"
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_malformed_xml_is_rejected(clock: FakeClock) -> None:
    respx.get(API_URL).mock(
        return_value=httpx.Response(200, content=b"<feed><entry>", headers=ATOM_HEADERS)
    )
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
        assert excinfo.value.code is ErrorCode.ARXIV_BAD_RESPONSE
    finally:
        await client.aclose()


# —— 传输层失败 ——


@pytest.mark.asyncio
@respx.mock
async def test_timeout_is_retried_then_reported(clock: FakeClock) -> None:
    route = respx.get(API_URL).mock(side_effect=httpx.ReadTimeout("read timed out"))
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
        assert excinfo.value.code is ErrorCode.ARXIV_TIMEOUT
        assert excinfo.value.retryable is True
        assert route.call_count == 4
        assert backoff_delays(client) == list(DEFAULT_BACKOFF)
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_dns_failure_is_reported_as_dns(clock: FakeClock) -> None:
    respx.get(API_URL).mock(
        side_effect=httpx.ConnectError("[Errno 11001] getaddrinfo failed")
    )
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
        assert excinfo.value.code is ErrorCode.ARXIV_DNS_FAILURE
        assert "网络、代理或 DNS" in excinfo.value.message_zh
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_connection_refused_is_reported_as_connect_failure(clock: FakeClock) -> None:
    respx.get(API_URL).mock(side_effect=httpx.ConnectError("Connection refused"))
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
        assert excinfo.value.code is ErrorCode.ARXIV_CONNECT_FAILURE
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_tls_failure_is_not_retried_and_never_disables_verification(
    clock: FakeClock,
) -> None:
    route = respx.get(API_URL).mock(
        side_effect=ssl.SSLCertVerificationError("certificate verify failed")
    )
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
        assert excinfo.value.code is ErrorCode.TLS_VERIFY_FAILED
        assert route.call_count == 1
        assert backoff_delays(client) == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_proxy_auth_failure_is_reported(clock: FakeClock) -> None:
    respx.get(API_URL).mock(side_effect=httpx.ProxyError("407 Proxy Authentication Required"))
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
        assert excinfo.value.code is ErrorCode.PROXY_AUTH_FAILED
    finally:
        await client.aclose()


# —— 重定向 ——


@pytest.mark.asyncio
@respx.mock
async def test_same_host_https_redirect_is_followed(clock: FakeClock) -> None:
    """同源 https 重定向会被跟随；每一跳都占用一个限速名额。"""
    respx.get(url__startswith=API_URL).mock(
        side_effect=[
            httpx.Response(301, headers={"location": API_URL + "?x=1"}),
            httpx.Response(200, content=ATOM_OK, headers=ATOM_HEADERS),
        ]
    )
    client = make_client(clock)
    try:
        page = await client.fetch_page(make_query(), 0, 200)
        assert len(page.papers) == 3
        assert client._limiter.grants == 2  # noqa: SLF001 - 两跳各占一个名额
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_cross_site_redirect_is_refused(clock: FakeClock) -> None:
    """跨站重定向必须拒绝（方案 §5.7）。"""
    respx.get(API_URL).mock(
        return_value=httpx.Response(301, headers={"location": "https://evil.example/api"})
    )
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
        assert excinfo.value.details["reason"] == "cross_site_redirect"
        assert "已拒绝" in excinfo.value.message_zh
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_redirect_downgrade_to_http_is_refused(clock: FakeClock) -> None:
    respx.get(API_URL).mock(
        return_value=httpx.Response(301, headers={"location": "http://export.arxiv.org/api/query"})
    )
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
        assert excinfo.value.details["reason"] == "cross_site_redirect"
    finally:
        await client.aclose()


# —— 取消 ——


@pytest.mark.asyncio
@respx.mock
async def test_cancel_before_request_sends_nothing(clock: FakeClock) -> None:
    route = respx.get(API_URL).mock(return_value=httpx.Response(200, content=ATOM_OK, headers=ATOM_HEADERS))
    client = make_client(clock)
    token = FakeCancel(set_=True)
    try:
        with pytest.raises(asyncio.CancelledError):
            await client.fetch_page(make_query(), 0, 200, token)  # type: ignore[arg-type]
        assert route.call_count == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_cancel_during_backoff_stops_the_retry(clock: FakeClock) -> None:
    """取消能打断退避等待——第三次请求不应该发出去。"""
    route = respx.get(API_URL).mock(
        return_value=httpx.Response(503, text="down", headers=HTML_HEADERS)
    )
    token = FakeCancel()

    class CancellingSleeper(RecordingSleeper):
        async def __call__(self, seconds: float) -> None:
            self.delays.append(seconds)
            token.cancel()  # 第一次退避时就请求取消
            await asyncio.sleep(0)

    client = make_client(clock, sleeper=CancellingSleeper())
    try:
        with pytest.raises(asyncio.CancelledError):
            await client.fetch_page(make_query(), 0, 200, token)  # type: ignore[arg-type]
        assert route.call_count == 1, "取消后不得再发请求"
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_cancel_between_rate_limit_retries(clock: FakeClock) -> None:
    """限流退避期间取消，同样立刻停下。"""
    route = respx.get(API_URL).mock(return_value=httpx.Response(429))
    token = FakeCancel()

    class CancellingSleeper(RecordingSleeper):
        async def __call__(self, seconds: float) -> None:
            self.delays.append(seconds)
            token.cancel()
            await asyncio.sleep(0)

    client = make_client(clock, sleeper=CancellingSleeper())
    try:
        with pytest.raises(asyncio.CancelledError):
            await client.fetch_page(make_query(), 0, 200, token)  # type: ignore[arg-type]
        assert route.call_count == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_no_real_sleeping_happens_when_a_sleeper_is_injected(clock: FakeClock) -> None:
    """守住「测试不许真实等待」这条线。

    曾经因为没有把 sleeper 传进 `cancellable_sleep`，一次测试套件真实睡了 317 秒。
    """
    sleeper = RecordingSleeper()
    respx.get(API_URL).mock(
        side_effect=[
            httpx.Response(503, text="down", headers=HTML_HEADERS),
            httpx.Response(200, content=ATOM_OK, headers=ATOM_HEADERS),
        ]
    )
    client = make_client(clock, sleeper=sleeper)
    started = time.monotonic()
    try:
        await client.fetch_page(make_query(), 0, 200)
    finally:
        await client.aclose()
    elapsed = time.monotonic() - started
    assert sleeper.delays == [DEFAULT_BACKOFF[0]], "退避应当走注入的 sleeper"
    assert elapsed < 1.0, f"测试里发生了真实等待：{elapsed:.1f}s"


# —— 诊断脱敏 ——


@pytest.mark.asyncio
@respx.mock
async def test_diagnostic_never_leaks_proxy_credentials(clock: FakeClock) -> None:
    respx.get(API_URL).mock(
        return_value=httpx.Response(
            503,
            text="proxy http://alice:s3cret@proxy.corp:8080 failed password=hunter2",
            headers=HTML_HEADERS,
        )
    )
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
    finally:
        await client.aclose()

    blob = repr(excinfo.value.details) + repr(client.last_diagnostic)
    assert "s3cret" not in blob
    assert "hunter2" not in blob
    assert "***" in blob


@pytest.mark.asyncio
@respx.mock
async def test_diagnostic_body_prefix_is_bounded(clock: FakeClock) -> None:
    respx.get(API_URL).mock(
        return_value=httpx.Response(503, text="x" * 100_000, headers=HTML_HEADERS)
    )
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
    finally:
        await client.aclose()
    assert len(excinfo.value.details["body_prefix"]) <= 1024


@pytest.mark.asyncio
@respx.mock
async def test_transport_diagnostic_is_redacted(clock: FakeClock) -> None:
    respx.get(API_URL).mock(
        side_effect=httpx.ConnectError("failed via http://bob:pw@proxy:3128")
    )
    client = make_client(clock)
    try:
        with pytest.raises(ArxivFilterError) as excinfo:
            await client.fetch_page(make_query(), 0, 200)
    finally:
        await client.aclose()
    assert "pw" not in repr(excinfo.value.details) or "***" in repr(excinfo.value.details)
    assert "bob:pw" not in repr(excinfo.value.details)


# —— 限速整合 ——


@pytest.mark.asyncio
@respx.mock
async def test_every_attempt_passes_through_the_same_limiter(clock: FakeClock) -> None:
    """方案 §5.4：所有首次请求和 retry 共享同一个 limiter。"""
    respx.get(API_URL).mock(
        side_effect=[
            httpx.Response(503, text="down", headers=HTML_HEADERS),
            httpx.Response(503, text="down", headers=HTML_HEADERS),
            httpx.Response(200, content=ATOM_OK, headers=ATOM_HEADERS),
        ]
    )
    client = make_client(clock)
    try:
        await client.fetch_page(make_query(), 0, 200)
        assert client._limiter.grants == 3  # noqa: SLF001
        # 3 次请求 → 两次限速等待，每次 3 秒
        assert client._limiter.min_observed_interval() == pytest.approx(3.0)  # noqa: SLF001
    finally:
        await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_provider_protocol_conformance(clock: FakeClock) -> None:
    from arxiv_filter.arxiv.provider import MetadataProvider

    client = make_client(clock)
    try:
        assert isinstance(client, MetadataProvider)
        assert client.name == "arxiv_api"
    finally:
        await client.aclose()
