"""验证离线 deny-list 本身：既拦住出网，又不误伤 asyncio 的回环 self-pipe。"""

from __future__ import annotations

import asyncio
import socket

import pytest


def test_outbound_dns_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="禁止联网"):
        socket.getaddrinfo("export.arxiv.org", 443)


def test_outbound_connect_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="禁止联网"):
        socket.create_connection(("93.184.216.34", 80), timeout=1)


def test_loopback_dns_is_allowed() -> None:
    assert socket.getaddrinfo("127.0.0.1", 0)


def test_loopback_hostname_is_allowed() -> None:
    assert socket.getaddrinfo("localhost", 0)


@pytest.mark.asyncio
async def test_asyncio_event_still_works_under_the_deny_list() -> None:
    """``asyncio.Event`` 在 Windows 上用回环 socket 做 self-pipe，不能被拦。"""
    event = asyncio.Event()
    assert not event.is_set()
    event.set()
    assert event.is_set()


@pytest.mark.asyncio
async def test_asyncio_sleep_still_works_under_the_deny_list() -> None:
    await asyncio.sleep(0)
