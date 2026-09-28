"""arXiv API 客户端（方案 §5.4 T3.2）。

固定规则（方案 §5.4）：

* ``max_connections=1``、``max_keepalive_connections=1``；
* 超时 connect 10 / read 30 / write 10 / pool 10 秒；
* 默认页大小 200（产品工程值，不冒充官方推荐值）；
* DNS、连接/读取超时、500/502/503/504：初次 + 最多 3 次重试，
  等待 3.2 / 6.4 / 12.8 秒加 0–1 秒抖动；
* 429：优先遵守合法 ``Retry-After``；没有时等 30 秒，只重试 1 次，然后打开 5 分钟熔断；
* **406：按限流处理**（2026-09-24 实测，见 ``docs/api-verification.md`` §3），
  走与 429 相同的长退避；**绝不**归因于用户的关键词语法；
* 可识别的 Atom 参数错误、400/401/403/404 **不重试**；
* **所有首次请求和 retry 共享同一个 limiter**；
* 先看状态码与 Content-Type，再解析 XML——500 可能是 Atom error entry，
  503 可能是 HTML，429 可能是纯文本；
* 失败只保留 ≤ 1 KiB 的脱敏诊断。
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping

import httpx

from arxiv_filter.arxiv.atom_parser import (
    detect_atom_error,
    known_categories_from,
    parse_atom,
)
from arxiv_filter.arxiv.provider import CancelToken
from arxiv_filter.arxiv.rate_limiter import RateLimiter, cancellable_sleep, get_rate_limiter
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.redaction import diagnostic_prefix, redact, redact_url
from arxiv_filter.models.query import BuiltQuery, FeedPage
from arxiv_filter.taxonomy import Taxonomy, load_taxonomy

logger = logging.getLogger(__name__)

#: 查询端点（方案 §5.1）。集中在一处，便于将来换适配器。
API_URL = "https://export.arxiv.org/api/query"

#: User-Agent：方案 §5.3 / §5.6 固定的连通性检查标识。
USER_AGENT = "arxiv-filter/0.1 (personal research client; +https://info.arxiv.org/help/api/tou.html)"

#: 分项超时（方案 §4.8）。
DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

#: 可重试的 5xx。
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})

#: 不重试的客户端错误：重试只会重复同一个错误。
NON_RETRYABLE_STATUS = frozenset({400, 401, 403, 404, 410, 422})

#: 退避序列（方案 §5.4）。
DEFAULT_BACKOFF: tuple[float, ...] = (3.2, 6.4, 12.8)

#: 429/406 在没有 Retry-After 时的等待秒数。
DEFAULT_RATE_LIMIT_WAIT_S = 30.0

#: 触发限流后的熔断时长（方案 §5.4：5 分钟）。
DEFAULT_CIRCUIT_BREAKER_S = 300.0

#: 响应体读取上限（20 MiB）。
DEFAULT_MAX_BODY_BYTES = 20 * 1024 * 1024

#: 默认页大小（方案 §5.4）。
DEFAULT_PAGE_SIZE = 200

#: ``Retry-After`` 的合法上界；超过就当成不可信，回退到默认等待。
MAX_RETRY_AFTER_S = 600.0

#: 跟随同源重定向的最大跳数（方案 §5.7：固定 HTTPS、拒绝跨站）。
MAX_REDIRECT_HOPS = 2


@dataclass(slots=True)
class ClientConfig:
    """客户端工程参数。它们是**客户端默认值，不是 arXiv 官方限额**。"""

    api_url: str = API_URL
    page_size: int = DEFAULT_PAGE_SIZE
    max_attempts: int = 4
    backoff: tuple[float, ...] = DEFAULT_BACKOFF
    jitter_s: float = 1.0
    rate_limit_wait_s: float = DEFAULT_RATE_LIMIT_WAIT_S
    circuit_breaker_s: float = DEFAULT_CIRCUIT_BREAKER_S
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    timeout: httpx.Timeout = field(default_factory=lambda: DEFAULT_TIMEOUT)
    user_agent: str = USER_AGENT
    proxy_url: str | None = None
    trust_env: bool = True
    #: Opt-in transport override. ``None`` keeps httpx's default transport — which is
    #: what the test suite needs, because ``respx`` intercepts by replacing the
    #: *default* transport and cannot see a client that carries an explicit one.
    #: Production sets this to a curl-backed transport to work around arXiv's
    #: intermittent 406 (see ``arxiv_filter.arxiv.curl_transport``).
    transport: httpx.AsyncBaseTransport | None = None


def parse_retry_after(value: str | None) -> float | None:
    """解析 ``Retry-After``。

    只接受**非负的秒数**形式。HTTP-date 形式与负数、超大值一律当作不可信返回 ``None``
    ——宁可按默认值保守等待，也不按一个荒谬的数字去等或去抢。
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        return None
    if seconds < 0 or seconds > MAX_RETRY_AFTER_S:
        return None
    return seconds


def is_dns_error(exc: httpx.ConnectError) -> bool:
    """区分「DNS 解析失败」与「连得上但被拒」。"""
    text = str(exc).lower()
    markers = (
        "name or service not known",
        "getaddrinfo failed",
        "nodename nor servname",
        "no address associated",
        "name resolution",
        "11001",  # WSAHOST_NOT_FOUND
        "getaddrinfo",
    )
    return any(marker in text for marker in markers)


def is_tls_error(exc: BaseException) -> bool:
    """TLS 证书校验失败——**不能**自动关闭校验绕过（方案 §4.8）。"""
    text = f"{type(exc).__name__}: {exc}".lower()
    markers = (
        "certificate verify failed",
        "sslcertverificationerror",
        "certificateerror",
        "self signed certificate",
        "self-signed certificate",
        "unable to get local issuer",
    )
    return any(marker in text for marker in markers)


def is_proxy_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return "proxy" in text or "407" in text


class ArxivClient:
    """按方案 §5.4 约束访问 ``export.arxiv.org`` 的异步客户端。

    实现 :class:`~arxiv_filter.arxiv.provider.MetadataProvider` 协议。
    """

    name = "arxiv_api"

    def __init__(
        self,
        *,
        limiter: RateLimiter | None = None,
        client: httpx.AsyncClient | None = None,
        config: ClientConfig | None = None,
        taxonomy: Taxonomy | None = None,
        rng: random.Random | None = None,
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config or ClientConfig()
        self._limiter = limiter or get_rate_limiter()
        self._rng = rng or random.Random()
        #: 退避与限流等待用的 sleep。**必须**可注入：默认的 ``asyncio.sleep`` 会让
        #: 覆盖重试路径的测试真实等待数分钟（实测过一次 317 秒的测试套件）。
        self._sleep = sleeper or asyncio.sleep
        self._taxonomy = taxonomy
        self._known_categories: frozenset[str] | None = None
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=self.config.timeout,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={"User-Agent": self.config.user_agent, "Accept": "application/atom+xml, application/xml"},
            follow_redirects=False,
            trust_env=self.config.trust_env,
            proxy=self.config.proxy_url,
            transport=self.config.transport,
        )
        #: 熔断到期时刻（monotonic 语义由 asyncio 时钟提供，这里用事件循环时间）。
        self._circuit_open_until: float | None = None
        self.last_diagnostic: Mapping[str, object] | None = None

    # —— 生命周期 ——

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def reconfigure_proxy(self, proxy_url: str | None, *, trust_env: bool) -> None:
        """Swap transport after an idle-only proxy settings change."""
        if not self._owns_client:
            raise RuntimeError("Cannot replace an injected HTTP client")
        old_client = self._client
        self.config.proxy_url = proxy_url
        self.config.trust_env = trust_env
        self._client = httpx.AsyncClient(
            timeout=self.config.timeout,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            headers={"User-Agent": self.config.user_agent, "Accept": "application/atom+xml, application/xml"},
            follow_redirects=False,
            trust_env=trust_env,
            proxy=proxy_url,
            transport=self.config.transport,
        )
        await old_client.aclose()
        self.reset_circuit()

    async def __aenter__(self) -> ArxivClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # —— 熔断 ——

    @property
    def circuit_open(self) -> bool:
        if self._circuit_open_until is None:
            return False
        if asyncio.get_running_loop().time() >= self._circuit_open_until:
            self._circuit_open_until = None
            return False
        return True

    def _trip_circuit(self) -> None:
        self._circuit_open_until = (
            asyncio.get_running_loop().time() + self.config.circuit_breaker_s
        )
        logger.warning(
            "arXiv 连续失败，已熔断 %.0f 秒", self.config.circuit_breaker_s
        )

    def reset_circuit(self) -> None:
        self._circuit_open_until = None

    @property
    def connection_limits(self) -> tuple[int, int]:
        """``(max_connections, max_keepalive_connections)``。

        方案 §5.4 要求都为 1，验收项 A3 需要断言「最大的并发 connection 数为 1」。
        httpx 把这两个值放在 transport 的连接池上，这里集中读取，避免各处去摸私有属性。
        """
        pool = getattr(getattr(self._client, "_transport", None), "_pool", None)
        if pool is None:  # pragma: no cover - 自定义 transport（测试注入）
            return (-1, -1)
        return (pool._max_connections, pool._max_keepalive_connections)  # noqa: SLF001

    # —— 主入口 ——

    async def fetch_page(
        self,
        query: BuiltQuery,
        start: int,
        limit: int,
        cancel: CancelToken | None = None,
    ) -> FeedPage:
        """取一页结果。

        所有尝试（含重试）都经过同一个限速器；``cancel`` 置位时抛
        :class:`asyncio.CancelledError`。
        """
        if self.circuit_open:
            raise ArxivFilterError(
                ErrorCode.ARXIV_UNAVAILABLE,
                details={"reason": "circuit_open"},
                message_zh="arXiv 暂时不可用，程序正在熔断等待中，请稍后再试。",
            )

        params = query.as_params(start, limit)
        attempts = max(1, self.config.max_attempts)
        rate_limit_attempts = 0
        last_error: ArxivFilterError | None = None

        for attempt in range(attempts):
            if cancel is not None:
                cancel.raise_if_cancelled()

            try:
                response = await self._send(params, cancel)
            except (httpx.HTTPError, OSError) as exc:
                # OSError 覆盖 ssl.SSLCertVerificationError 这类不由 httpx 包装的
                # 传输层异常；asyncio.CancelledError 是 BaseException，不会被误捕。
                last_error = self._classify_transport_error(exc)
                if last_error.retryable and attempt < attempts - 1:
                    await self._backoff(attempt, cancel)
                    continue
                if last_error.retryable:
                    self._trip_circuit()
                raise last_error from exc

            outcome = self._classify_response(response)
            if outcome is None:
                return self._parse(response, params)

            last_error = outcome

            if not outcome.retryable:
                raise outcome

            # 限流（429 / 406）：整个请求只多试 1 次，然后熔断（方案 §5.4）
            if outcome.code in (
                ErrorCode.ARXIV_RATE_LIMITED,
                ErrorCode.ARXIV_NOT_ACCEPTABLE,
            ):
                rate_limit_attempts += 1
                if rate_limit_attempts > 1:
                    self._trip_circuit()
                    raise outcome
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                await cancellable_sleep(
                    retry_after if retry_after is not None else self.config.rate_limit_wait_s,
                    cancel,
                    sleeper=self._sleep,
                )
                continue

            if attempt < attempts - 1:
                await self._backoff(attempt, cancel)
                continue

            self._trip_circuit()
            raise outcome

        raise last_error or ArxivFilterError(ErrorCode.ARXIV_UNAVAILABLE)

    # —— 内部步骤 ——

    def _is_same_host(self, location: str) -> bool:
        """重定向目标是否与 API 同源（方案 §5.7：跨站 redirect 拒绝）。"""
        base = httpx.URL(self.config.api_url)
        if not location:
            return False
        target = base.join(location)
        return target.host == base.host and target.scheme == "https"

    async def _send(
        self,
        params: Mapping[str, object], cancel: CancelToken | None
    ) -> httpx.Response:
        """过一次限速器后发送请求，并只跟随**同源**重定向。

        固定用 HTTPS；``http://`` → ``https://`` 的同域 301 会被跟随，
        跨站重定向则直接拒绝（方案 §5.7）。每一跳都占用一个限速名额。
        """
        url = self.config.api_url
        query: Mapping[str, object] | None = params

        for _ in range(MAX_REDIRECT_HOPS + 1):
            await self._limiter.acquire(cancel)
            response = await self._client.get(url, params=None if query is None else dict(query))

            if response.status_code not in {301, 302, 303, 307, 308}:
                return response

            location = response.headers.get("location", "")
            if not self._is_same_host(location):
                raise ArxivFilterError(
                    ErrorCode.ARXIV_BAD_RESPONSE,
                    details={
                        "reason": "cross_site_redirect",
                        "location": redact_url(location)[:200],
                    },
                    message_zh="服务要求跳转到其它站点，出于安全考虑已拒绝。",
                )
            url = str(httpx.URL(self.config.api_url).join(location))
            # Location 自带 query 时不再重复附加参数
            query = None if "?" in location else params

        raise ArxivFilterError(
            ErrorCode.ARXIV_BAD_RESPONSE,
            details={"reason": "too_many_redirects"},
        )

    async def _backoff(self, attempt: int, cancel: CancelToken | None) -> None:
        """3.2 / 6.4 / 12.8 秒 + 0–1 秒抖动（方案 §5.4）。"""
        index = min(attempt, len(self.config.backoff) - 1)
        delay = self.config.backoff[index] + self._rng.uniform(0, self.config.jitter_s)
        logger.info("第 %d 次重试前等待 %.1f 秒", attempt + 1, delay)
        await cancellable_sleep(delay, cancel, sleeper=self._sleep)

    def _classify_transport_error(self, exc: BaseException) -> ArxivFilterError:
        """把 httpx 异常翻译成带中文文案的领域错误。"""
        self.last_diagnostic = {
            "phase": "transport",
            "exception": type(exc).__name__,
            "message": redact(str(exc))[:200],
        }

        if isinstance(exc, httpx.TimeoutException):
            return ArxivFilterError(ErrorCode.ARXIV_TIMEOUT, details=dict(self.last_diagnostic))
        if is_tls_error(exc):
            return ArxivFilterError(
                ErrorCode.TLS_VERIFY_FAILED, details=dict(self.last_diagnostic)
            )
        if is_proxy_error(exc):
            return ArxivFilterError(
                ErrorCode.PROXY_AUTH_FAILED, details=dict(self.last_diagnostic)
            )
        if isinstance(exc, httpx.ConnectError):
            code = (
                ErrorCode.ARXIV_DNS_FAILURE
                if is_dns_error(exc)
                else ErrorCode.ARXIV_CONNECT_FAILURE
            )
            return ArxivFilterError(code, details=dict(self.last_diagnostic))
        return ArxivFilterError(
            ErrorCode.ARXIV_CONNECT_FAILURE, details=dict(self.last_diagnostic)
        )

    def _classify_response(self, response: httpx.Response) -> ArxivFilterError | None:
        """返回 ``None`` 表示可以继续解析；否则返回应当抛出/重试的错误。"""
        status = response.status_code
        content_type = response.headers.get("content-type", "")
        diagnostic = {
            "status": status,
            "content_type": content_type,
            "url": redact_url(str(response.url)),
            "retry_after": response.headers.get("retry-after"),
            "bytes": len(response.content),
        }

        if status == 200:
            self.last_diagnostic = diagnostic
            return None

        diagnostic["body_prefix"] = diagnostic_prefix(response.content)
        self.last_diagnostic = diagnostic

        # 先看内容再按状态码分类：XML 体可能是上游的参数错误说明（不重试），
        # 也可能是「错误状态码 + 正常 feed」这种反常组合（按状态码处理）。
        is_xml = "xml" in content_type.lower() or "atom" in content_type.lower()
        if is_xml:
            summary = detect_atom_error(response.content)
            if summary is not None:
                return ArxivFilterError(
                    ErrorCode.ARXIV_ATOM_ERROR,
                    details=dict(diagnostic, upstream_summary=summary),
                    message_zh=f"arXiv 拒绝了这条查询：{summary}",
                )

        if status == 429:
            return ArxivFilterError(ErrorCode.ARXIV_RATE_LIMITED, details=dict(diagnostic))
        if status == 406:
            # 2026-09-24 实测：这是限流信号，不是「查询语法错误」
            return ArxivFilterError(
                ErrorCode.ARXIV_NOT_ACCEPTABLE,
                details=dict(diagnostic, interpreted_as="rate_limit"),
            )
        if status in RETRYABLE_STATUS:
            return ArxivFilterError(ErrorCode.ARXIV_UNAVAILABLE, details=dict(diagnostic))
        if status in NON_RETRYABLE_STATUS:
            return ArxivFilterError(ErrorCode.ARXIV_QUERY_CONTRACT, details=dict(diagnostic))
        if 300 <= status < 400:
            # 能走到这里说明重定向没被 _send 跟随（跨站或超过跳数上限）
            return ArxivFilterError(
                ErrorCode.ARXIV_BAD_RESPONSE,
                details=dict(diagnostic, reason="unexpected_redirect"),
            )
        return ArxivFilterError(ErrorCode.ARXIV_BAD_RESPONSE, details=dict(diagnostic))

    def _parse(self, response: httpx.Response, params: Mapping[str, object]) -> FeedPage:
        """读取有界正文并交给 Atom 解析器。"""
        content_type = response.headers.get("content-type", "").lower()
        if "xml" not in content_type and "atom" not in content_type:
            raise ArxivFilterError(
                ErrorCode.ARXIV_BAD_RESPONSE,
                details={
                    "reason": "non_xml_content_type",
                    "content_type": content_type,
                    "body_prefix": diagnostic_prefix(response.content),
                },
            )

        body = response.content
        if len(body) > self.config.max_body_bytes:
            raise ArxivFilterError(
                ErrorCode.ARXIV_BAD_RESPONSE,
                details={
                    "reason": "response_too_large",
                    "bytes": len(body),
                    "limit": self.config.max_body_bytes,
                },
            )

        if self._known_categories is None:
            table = self._taxonomy if self._taxonomy is not None else load_taxonomy()
            self._known_categories = known_categories_from(table)

        page = parse_atom(body, self._now(), known_categories=self._known_categories)
        logger.debug(
            "取回 %d 条（total=%s start=%s）",
            len(page.papers),
            page.total_results,
            params.get("start"),
        )
        return page

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)


__all__ = [
    "API_URL",
    "DEFAULT_BACKOFF",
    "DEFAULT_CIRCUIT_BREAKER_S",
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_RATE_LIMIT_WAIT_S",
    "DEFAULT_TIMEOUT",
    "MAX_REDIRECT_HOPS",
    "MAX_RETRY_AFTER_S",
    "NON_RETRYABLE_STATUS",
    "RETRYABLE_STATUS",
    "USER_AGENT",
    "ArxivClient",
    "ClientConfig",
    "is_dns_error",
    "is_proxy_error",
    "is_tls_error",
    "parse_retry_after",
]
