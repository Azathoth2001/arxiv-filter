"""全局测试夹具。

方案 §7.1 的硬规则：

* Unit / Integration / E2E **禁止联网**；只有显式标记 ``@pytest.mark.live_arxiv``
  且设置 ``ARXIV_LIVE_TESTS=1`` 才允许真实访问官方端点；
* 每个测试都使用临时数据目录，绝不读写真实的 ``%LOCALAPPDATA%\\ArxivFilter``；
* 离线测试不允许真实 ``sleep``（由各测试自行用 fake clock 保证）。
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

#: 允许联网的环境变量开关名。
LIVE_ENV_VAR = "ARXIV_LIVE_TESTS"


def _live_enabled() -> bool:
    return os.environ.get(LIVE_ENV_VAR) == "1"


@pytest.fixture(autouse=True)
def isolated_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """把可写数据目录指向每个测试独立的临时目录。"""
    target = tmp_path / "ArxivFilter"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ARXIV_FILTER_DATA_DIR", str(target))
    yield target


#: 视为「本地」的地址，放行。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", "", None})


def _is_loopback(address: object) -> bool:
    """``address`` 是否指向本机。

    Windows 上 ``asyncio`` 的事件循环用回环 socket 实现 self-pipe，
    所以**不能**粗暴地封掉 ``socket.socket``——那会让所有异步测试直接报错。
    正确做法是只拦真正的出网连接。
    """
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if host in _LOOPBACK_HOSTS:
        return True
    return isinstance(host, str) and host.startswith("127.")


@pytest.fixture(autouse=True)
def deny_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认封禁所有**出网**调用，但放行回环。

    标记为 ``live_arxiv`` 的测试在 ``ARXIV_LIVE_TESTS=1`` 时放行，否则跳过。
    Live 测试不得并行执行（方案 §7.1）。
    """
    if request.node.get_closest_marker("live_arxiv") is not None:
        if not _live_enabled():
            pytest.skip(f"live_arxiv 测试需要设置 {LIVE_ENV_VAR}=1")
        return

    def _blocked(*args: object, **kwargs: object) -> None:
        raise RuntimeError(
            "离线测试禁止联网：请用 respx 冻结 HTTP，"
            "或把测试标记为 @pytest.mark.live_arxiv 并设置 ARXIV_LIVE_TESTS=1。"
        )

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def guarded_connect(self: socket.socket, address: object):  # type: ignore[no-untyped-def]
        if _is_loopback(address):
            return real_connect(self, address)  # type: ignore[arg-type]
        _blocked()

    def guarded_connect_ex(self: socket.socket, address: object):  # type: ignore[no-untyped-def]
        if _is_loopback(address):
            return real_connect_ex(self, address)  # type: ignore[arg-type]
        _blocked()

    def guarded_getaddrinfo(host: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if host in _LOOPBACK_HOSTS or (isinstance(host, str) and host.startswith("127.")):
            return real_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]
        _blocked()

    def guarded_create_connection(address: object, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if _is_loopback(address):
            return real_create_connection(address, *args, **kwargs)  # type: ignore[arg-type]
        _blocked()

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
