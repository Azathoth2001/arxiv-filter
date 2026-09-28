"""curl 传输层的离线测试（方案 §5.7 补充）。

不访问网络：用一个**假 curl 可执行文件**替代真实 curl，它按第一个 `-H` 头里的
用例名返回预设的 stdout / 退出码。这样状态码标记解析、正文解码、超时换算、
错误映射、以及 `build_transport` 的默认/回退/强制三种模式都能离线覆盖。

注意测试纪律（conftest 的 socket deny-list）：本文件全程不建 socket。
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import httpx
import pytest

from arxiv_filter.arxiv import curl_transport
from arxiv_filter.arxiv.curl_transport import (
    CurlAsyncTransport,
    active_transport_choice,
    build_transport,
    curl_available,
    curl_get,
    resolve_curl,
)

MARKER = curl_transport._STATUS_MARKER.decode()

#: 假 curl 的行为表：`X-Case` 头 -> (退出码, stdout, stderr)
#: 未列出的用例返回 200 + 一段最小 Atom。
FAKE_CURL = '''\
"""Test double for curl.exe: prints a canned body plus the -w status marker."""
import gzip
import sys

MARKER = {marker!r}
ATOM = b"<?xml version='1.0' encoding='UTF-8'?><feed><entry><id>1</id></entry></feed>"

argv = sys.argv[1:]
case = ""
for index, item in enumerate(argv):
    if item == "-H" and index + 1 < len(argv) and argv[index + 1].lower().startswith("x-case:"):
        case = argv[index + 1].split(":", 1)[1].strip()

if case == "empty_body_406":
    sys.stdout.write(MARKER + "406")
elif case == "gzip_body":
    sys.stdout.buffer.write(gzip.compress(ATOM) + MARKER.encode() + b"200")
elif case == "no_marker":
    sys.stdout.write("this is not a curl response")
elif case == "bad_status":
    sys.stdout.write(MARKER + "not-a-number")
elif case == "curl_failure":
    sys.stderr.write("curl: (6) Could not resolve host: export.arxiv.org")
    sys.exit(6)
else:
    sys.stdout.buffer.write(ATOM + MARKER.encode() + b"200")
'''


@pytest.fixture()
def fake_curl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """安装假 curl 并把它设为 `ARXIV_FILTER_CURL_BIN`。"""
    script = tmp_path / "fake_curl.py"
    script.write_text(FAKE_CURL.format(marker=MARKER), encoding="utf-8")

    if sys.platform == "win32":
        # curl 由 subprocess 不带 shell 直接执行，因此需要一个真正的可执行文件；
        # 用 .cmd 包装脚本调用当前解释器。
        launcher = tmp_path / "fake_curl.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="ascii",
        )
        binary = launcher
    else:
        launcher = tmp_path / "fake_curl"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
            encoding="ascii",
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
        binary = launcher

    monkeypatch.setenv(curl_transport.ENV_CURL_BIN, str(binary))
    return binary


# —— curl_get 的解析行为 ——


def test_curl_get_parses_body_and_status(fake_curl: Path) -> None:
    status, body = curl_get("https://example.invalid/api/query", {}, 5.0)
    assert status == 200
    assert b"<feed>" in body


def test_curl_get_keeps_empty_body_on_406(fake_curl: Path) -> None:
    """406 空响应体是 arXiv 的真实拒绝形状，必须原样向上传，不能抛错。"""
    status, body = curl_get("https://example.invalid/api/query", {"X-Case": "empty_body_406"}, 5.0)
    assert status == 406
    assert body == b""


def test_curl_get_decompresses_when_server_ignores_accept_encoding(fake_curl: Path) -> None:
    status, body = curl_get("https://example.invalid/api/query", {"X-Case": "gzip_body"}, 5.0)
    assert status == 200
    assert b"<feed>" in body


def test_curl_get_without_marker_raises(fake_curl: Path) -> None:
    with pytest.raises(RuntimeError, match="状态标记"):
        curl_get("https://example.invalid/api/query", {"X-Case": "no_marker"}, 5.0)


def test_curl_get_with_unparsable_status_raises(fake_curl: Path) -> None:
    with pytest.raises(RuntimeError, match="无法解析"):
        curl_get("https://example.invalid/api/query", {"X-Case": "bad_status"}, 5.0)


def test_curl_get_forwards_headers_but_not_host(fake_curl: Path) -> None:
    """curl 自行管理 Host / Content-Length / Accept-Encoding，转发会破坏请求。"""
    captured: list[list[str]] = []
    real_run = curl_transport.subprocess.run

    def spy(command, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(list(command))
        return real_run(command, **kwargs)

    curl_transport.subprocess.run = spy  # type: ignore[assignment]
    try:
        curl_get(
            "https://example.invalid/api/query",
            {
                "User-Agent": "arxiv-filter-test/1.0",
                "Host": "evil.invalid",
                "Accept-Encoding": "br",
                "Content-Length": "999",
            },
            5.0,
        )
    finally:
        curl_transport.subprocess.run = real_run  # type: ignore[assignment]

    headers = [captured[0][i + 1] for i, item in enumerate(captured[0]) if item == "-H"]
    assert any(h.startswith("User-Agent:") for h in headers)
    assert not any(h.lower().startswith("host:") for h in headers)
    assert not any(h.lower().startswith("accept-encoding:") for h in headers)
    assert not any(h.lower().startswith("content-length:") for h in headers)


# —— 传输实现 ——


@pytest.mark.asyncio
async def test_transport_builds_httpx_response(fake_curl: Path) -> None:
    transport = CurlAsyncTransport()
    request = httpx.Request("GET", "https://example.invalid/api/query")
    response = await transport.handle_async_request(request)
    assert response.status_code == 200
    assert b"<feed>" in response.content
    assert response.headers["content-type"].startswith("application/atom+xml")


@pytest.mark.asyncio
async def test_transport_does_not_raise_on_406(fake_curl: Path) -> None:
    """406 必须作为普通响应交给 _classify_response 判定为限流。"""
    transport = CurlAsyncTransport()
    request = httpx.Request(
        "GET", "https://example.invalid/api/query", headers={"X-Case": "empty_body_406"}
    )
    response = await transport.handle_async_request(request)
    assert response.status_code == 406
    assert response.content == b""


# —— build_transport 的三种模式 ——


def test_default_mode_is_curl(fake_curl: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(curl_transport.ENV_TRANSPORT, raising=False)
    transport = build_transport()
    assert isinstance(transport, CurlAsyncTransport)
    assert active_transport_choice() == "curl"


def test_explicit_auto_also_uses_curl(fake_curl: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(curl_transport.ENV_TRANSPORT, "auto")
    assert isinstance(build_transport(), CurlAsyncTransport)


def test_can_force_httpx(fake_curl: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """一键退回旧行为（排查或确认 406 是否来自传输层时用）。"""
    monkeypatch.setenv(curl_transport.ENV_TRANSPORT, "httpx")
    assert build_transport() is None
    assert active_transport_choice() == "httpx"


def test_mode_is_case_insensitive(fake_curl: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(curl_transport.ENV_TRANSPORT, "  CuRL  ")
    assert isinstance(build_transport(), CurlAsyncTransport)


def test_unknown_mode_raises(fake_curl: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(curl_transport.ENV_TRANSPORT, "wget")
    with pytest.raises(ValueError, match="只接受"):
        build_transport()


def test_falls_back_to_httpx_when_curl_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """curl 缺失不能导致应用起不来：回退 httpx 并留下 WARNING。"""
    monkeypatch.delenv(curl_transport.ENV_TRANSPORT, raising=False)
    monkeypatch.setenv(curl_transport.ENV_CURL_BIN, str(tmp_path / "not-here"))
    monkeypatch.setattr(curl_transport.shutil, "which", lambda _name: None)
    # 本机真实存在 C:\Windows\System32\curl.exe，必须把候选表也清掉才算"没有 curl"
    monkeypatch.setattr(
        curl_transport, "_CURL_CANDIDATES", (str(tmp_path / "also-missing"),)
    )

    records: list[str] = []

    class Capture:
        def debug(self, message: str, *args: object) -> None:
            pass

        def warning(self, message: str, *args: object) -> None:
            records.append(message % args if args else message)

    monkeypatch.setattr(curl_transport, "logger", Capture())

    assert build_transport() is None
    assert active_transport_choice() == "httpx"
    assert any("回退到 httpx" in record for record in records), "回退时必须留下 WARNING"


def test_resolve_curl_reports_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(curl_transport.ENV_CURL_BIN, str(tmp_path / "not-here"))
    monkeypatch.setattr(curl_transport.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        curl_transport, "_CURL_CANDIDATES", (str(tmp_path / "also-missing"),)
    )
    with pytest.raises(RuntimeError, match="未找到 curl"):
        resolve_curl()
    assert curl_available() is False


def test_curl_bin_env_var_wins(fake_curl: Path) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        # 假 curl 路径优先于系统 curl
        assert resolve_curl() == str(fake_curl)
    finally:
        monkeypatch.undo()


@pytest.mark.skipif(
    os.environ.get("ARXIV_FILTER_CURL_BIN") is None and sys.platform != "win32",
    reason="仅在 Windows 上断言系统 curl 存在",
)
def test_system_curl_is_discoverable_without_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows 10 1803+ 自带 curl；这条断言保证默认模式在本机真的能启用。"""
    monkeypatch.delenv(curl_transport.ENV_CURL_BIN, raising=False)
    assert curl_available() is True
