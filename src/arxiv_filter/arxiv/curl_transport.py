"""curl 后端传输层：绕开 arXiv 边缘层对 httpx 的间歇性 406（方案 §5.7 补充）。

## 为什么需要它

`docs/api-verification.md` §3 记录了实测结论：`export.arxiv.org` 会对**需要回源**
的查询返回 **HTTP 406 空响应体**，且与请求头、UA、`Accept-Encoding`、代理、
`max_results` 全部无关。本模块把该现象进一步定位到**客户端 TLS 指纹**：

2026-09-27 同一台机器、同一个全新（未被边缘缓存）查询的 A/B 实测：

```text
A 生产路径 plain httpx      -> 406 ARXIV_NOT_ACCEPTABLE   4.33 s
B curl 传输（同一 URL）     -> 200，正常返回 Atom          0.98 s
```

已有的限速器与 406 长退避**无法**缩短这种拒绝：它是边缘层按客户端指纹给出的，
不是官方文档意义上的「每 3 秒 1 次」限流。换传输层指纹可以直接穿过它。

## 与既有设计的关系

* 只替换 **transport**，`ArxivClient` 的重试、限流、熔断、错误分类、`respx`
  之外的一切逻辑都不变；
* 默认**不启用**。`ClientConfig.transport` 默认 `None`，因为 `respx` 通过替换
  **默认**传输实现来冻结 HTTP，带显式 transport 的 client 会绕过它——测试必须
  保持默认值；
* 生产由 :func:`build_transport` 按环境变量决定是否启用。
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import subprocess
import zlib
from typing import Any

logger = logging.getLogger(__name__)

#: 环境变量：`curl`（默认）、`httpx`（强制关闭）或 `auto`（curl 优先，失败即回退）。
ENV_TRANSPORT = "ARXIV_FILTER_TRANSPORT"

#: 环境变量：显式指定 curl 可执行文件路径。
ENV_CURL_BIN = "ARXIV_FILTER_CURL_BIN"

_CURL_CANDIDATES = (
    r"C:\Windows\System32\curl.exe",
    "/usr/bin/curl",
    "/usr/local/bin/curl",
)

#: 默认模式。设为 `httpx` 可一键退回旧行为（环境变量优先级更高）。
DEFAULT_MODE = "curl"

#: curl 自行管理的头，转发会破坏请求。
_SKIP_HEADERS = frozenset({"accept-encoding", "content-length", "host"})

#: curl `-w` 模板在**直接 exec（不经 shell）**时不会解释 `\\n`，
#: 因此标记必须自定界，不能靠换行锚定。
_STATUS_MARKER = b"__ARXIV_FILTER_STATUS__"


def _no_window_kwargs() -> dict[str, Any]:
    """Windows: keep the curl child from flashing a console window.

    The frozen app is a GUI-subsystem binary (``console=False``), but a child
    process created without ``CREATE_NO_WINDOW`` still gets its own console —
    visible as a black window blinking on every request, which users read as
    "a command line window appeared while filtering". Measured 2026-09-27:
    ``curl.exe`` showed up as a visible window during one search job.
    On non-Windows platforms this is a no-op.
    """
    if os.name != "nt":
        return {}
    flags = 0
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if create_no_window:
        flags |= create_no_window
    if flags:
        return {"creationflags": flags}
    # Older Pythons: hide the window through STARTUPINFO instead.
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startupinfo}


def resolve_curl() -> str:
    """定位可用的 curl；`ARXIV_FILTER_CURL_BIN` 优先。找不到则抛错。"""
    candidates = (
        os.environ.get(ENV_CURL_BIN, ""),
        shutil.which("curl.exe") or "",
        shutil.which("curl") or "",
        *_CURL_CANDIDATES,
    )
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    raise RuntimeError(
        "未找到 curl 可执行文件；请安装 curl，或用 "
        f"{ENV_CURL_BIN} 指定其绝对路径。"
    )


def curl_available() -> bool:
    """curl 是否可定位。供系统信息页展示，不抛错。"""
    try:
        resolve_curl()
    except RuntimeError:
        return False
    return True


def curl_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    """用 curl 发一次 GET，返回 ``(status_code, 解压后的正文)``。"""
    curl = resolve_curl()
    command = [
        curl,
        "-sS",
        "-L",
        "--max-time", str(max(30, int(timeout))),
        "--compressed",
        "-X", "GET",
        "-w", f"{_STATUS_MARKER.decode()}%{{http_code}}",
    ]
    for key, value in headers.items():
        if key.lower() in _SKIP_HEADERS:
            continue
        command += ["-H", f"{key}: {value}"]
    command.append(url)

    completed = subprocess.run(
        command,
        capture_output=True,
        timeout=max(60, int(timeout) + 30),
        check=False,
        **_no_window_kwargs(),
    )
    raw = completed.stdout
    marker_at = raw.rfind(_STATUS_MARKER)
    if marker_at < 0:
        raise RuntimeError(
            "curl 未返回状态标记（rc="
            f"{completed.returncode}）：{completed.stderr.decode('utf-8', 'replace')[:300]}"
        )
    status_text = raw[marker_at + len(_STATUS_MARKER):].strip()
    if not status_text.isdigit():
        raise RuntimeError(
            f"curl 状态码无法解析：{status_text[:40]!r}（rc={completed.returncode}）"
        )
    body = raw[:marker_at]

    # --compressed 通常已解压；对未协商仍压缩的服务端兜底。
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except OSError:
            pass
    elif body[:1] == b"\x78":
        try:
            body = zlib.decompress(body)
        except zlib.error:
            pass

    return int(status_text), body


def _request_args(request: Any) -> tuple[str, dict[str, str], float]:
    timeout = request.extensions.get("timeout") or {}
    read_timeout = timeout.get("read") or timeout.get("connect") or 30.0
    headers = {
        key.decode("latin-1"): value.decode("latin-1")
        for key, value in request.headers.raw
    }
    return str(request.url), headers, float(read_timeout)


def _build_response(request: Any, status: int, body: bytes) -> Any:
    import httpx

    content_type = "application/atom+xml; charset=utf-8"
    if body[:1] == b"%":
        content_type = "application/pdf"
    elif body.lstrip()[:1] in (b"{", b"["):
        content_type = "application/json"
    return httpx.Response(
        status_code=status,
        headers={
            "content-type": content_type,
            # 供 classify 与用户可见文案使用的链路深度证据（方案 §3.4）。
            "content-length": str(len(body)),
        },
        content=body,
        request=request,
    )


def _make_transport_class() -> type:
    import httpx

    class CurlAsyncTransport(httpx.AsyncBaseTransport):
        """httpx 异步传输：每个请求都交给 curl 发出（因此带 curl 的 TLS 指纹）。"""

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            import anyio

            url, headers, timeout = _request_args(request)
            status, body = await anyio.to_thread.run_sync(
                lambda: curl_get(url, headers, timeout)
            )
            return _build_response(request, status, body)

    return CurlAsyncTransport


CurlAsyncTransport = _make_transport_class()


#: 上一次 :func:`build_transport` 的选择结果，供系统信息页展示。
_last_choice: str = "unknown"


def active_transport_choice() -> str:
    """返回上一次解析出的传输层：``curl`` 或 ``httpx``。"""
    return _last_choice


def build_transport() -> Any | None:
    """按环境变量返回传输实例；``None`` 表示保持 httpx 默认传输。

    模式（`ARXIV_FILTER_TRANSPORT`，大小写不敏感）：

    * ``curl``（**默认**）—— 用 curl；curl 不可用时**回退 httpx 并记 WARNING**；
    * ``auto`` —— 同 ``curl``，语义上强调"尽力而为"；
    * ``httpx`` —— 强制关闭，用于确认旧行为或排查；
    * 其他值 —— 抛 :class:`ValueError`，不做静默猜测。

    **为什么默认走 curl**：arXiv 边缘层对 httpx 指纹返回的 406 会**持续很久**，
    此时退避与熔断只能止损、无法恢复服务（实测：同一冷查询 httpx 406 / curl 200）。
    桌面工具不能要求最终用户去设环境变量，因此默认启用、并保留一键退回的开关。

    **为什么失败要回退而不是报错**：curl 是 Windows 10 1803+ 的系统自带组件，
    但精简镜像可能缺失。让应用起不来比"偶尔慢一点"更糟；回退时记 WARNING，
    用户仍可从日志与系统信息页看出当前实际使用的传输层。
    """
    global _last_choice
    mode = (os.environ.get(ENV_TRANSPORT) or DEFAULT_MODE).strip().lower()
    if mode in {"", "auto"}:
        mode = DEFAULT_MODE

    if mode == "httpx":
        _last_choice = "httpx"
        return None

    if mode != "curl":
        raise ValueError(
            f"{ENV_TRANSPORT} 只接受 'curl'、'auto' 或 'httpx'，收到 {mode!r}"
        )

    try:
        resolve_curl()
    except RuntimeError as exc:
        logger.warning(
            "未找到 curl，已回退到 httpx 传输；若持续出现 HTTP 406，请安装 curl 或设置 %s。原因：%s",
            ENV_CURL_BIN,
            exc,
        )
        _last_choice = "httpx"
        return None

    _last_choice = "curl"
    logger.debug("arXiv 请求将通过 curl 传输发出（绕开 httpx 指纹导致的 406）")
    return CurlAsyncTransport()


__all__ = [
    "DEFAULT_MODE",
    "ENV_CURL_BIN",
    "ENV_TRANSPORT",
    "CurlAsyncTransport",
    "active_transport_choice",
    "build_transport",
    "curl_available",
    "curl_get",
    "resolve_curl",
]
