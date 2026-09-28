"""Local app launcher: single instance, loopback server, session token, and browser."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import platform
import secrets
import socket
import sys
import time
from typing import Sequence
from urllib.parse import urlencode

import httpx
from logging.handlers import RotatingFileHandler

from arxiv_filter import SCHEMA_VERSION, __version__
from arxiv_filter.application import serve_application
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.paths import ensure_dirs, log_path, runtime_path
from arxiv_filter.runtime import (
    RuntimeRecord,
    SingleInstanceLock,
    bind_loopback,
    process_is_alive,
    read_runtime,
    remove_runtime,
    write_runtime,
)

PROG = "arxiv-filter"

logger = logging.getLogger(__name__)
INSTANCE_START_WAIT_SECONDS = 5.0


def configure_stdio() -> None:
    """把标准输出/错误切到 UTF-8。

    Windows 控制台默认编码常为 GBK/CP936，中文错误提示会变成乱码。测试环境下
    ``sys.stdout`` 是 pytest 的捕获对象、没有 ``reconfigure``，此时静默跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 流不支持重配置
            pass


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。

    这些开关是**开发者与诊断**用途；最终用户双击 exe 时不需要任何参数。
    """
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="arxiv-filter：本地 arXiv 文献收件箱（仅监听 127.0.0.1）。",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="打印版本、Python 与平台信息后退出。",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动服务但不自动打开浏览器（诊断用）。",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        metavar="N",
        help="固定监听端口；0（默认）表示由操作系统分配随机端口。",
    )
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        default=None,
        help="覆盖可写数据目录（等价于设置 ARXIV_FILTER_DATA_DIR）。",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
        help="日志级别，默认 INFO。",
    )
    return parser


def version_text() -> str:
    """``--version`` 的单行输出。"""
    return (
        f"{PROG} {__version__} "
        f"(schema {SCHEMA_VERSION}; Python {platform.python_version()}; "
        f"{platform.system()} {platform.release()})"
    )


def _configure_logging(level: str) -> None:
    ensure_dirs()
    root = logging.getLogger()
    root.setLevel(getattr(logging, level))
    if not any(isinstance(handler, RotatingFileHandler) for handler in root.handlers):
        handler = RotatingFileHandler(
            log_path(),
            maxBytes=2 * 1024 * 1024,
            backupCount=4,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        root.addHandler(handler)


def _existing_session(record: RuntimeRecord) -> str | None:
    if not process_is_alive(record.pid):
        return None
    params = urlencode({"challenge": record.challenge})
    url = f"http://127.0.0.1:{record.port}/api/v1/system/health?{params}"
    try:
        with httpx.Client(trust_env=False, timeout=0.75) as client:
            response = client.get(url)
        response.raise_for_status()
        value = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    token = value.get("session_token") if isinstance(value, dict) else None
    if (
        isinstance(value, dict)
        and value.get("status") == "ok"
        and value.get("challenge_valid") is True
        and isinstance(token, str)
        and len(token) >= 32
    ):
        return token
    return None


def _open_existing_instance(path, *, wait_seconds: float = INSTANCE_START_WAIT_SECONDS) -> bool:
    """Open an already-running app; never start a second server on a held lock."""
    import webbrowser

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        record = read_runtime(path)
        if record is not None:
            token = _existing_session(record)
            if token is not None:
                url = f"http://127.0.0.1:{record.port}/#token={token}"
                webbrowser.open(url, new=2)
                return True
        time.sleep(0.1)
    return False


def _run_server(args: argparse.Namespace) -> int:
    """Launch one authenticated localhost server on a pre-bound loopback socket."""
    try:
        _configure_logging(args.log_level)
    except OSError as exc:
        raise ArxivFilterError(
            ErrorCode.LAUNCH_FAILED,
            message_zh="无法创建本地数据或日志目录，请检查目录权限。",
        ) from exc
    runtime_file = runtime_path()
    lock = SingleInstanceLock(runtime_file.with_suffix(".lock"))
    if not lock.acquire():
        if _open_existing_instance(runtime_file):
            return 0
        raise ArxivFilterError(
            ErrorCode.LAUNCH_FAILED,
            message_zh="已有实例正在启动，但尚未通过本地健康校验；为安全起见不会启动第二个服务。请稍候再试。",
        )

    old_record = read_runtime(runtime_file)
    if old_record is not None:
        remove_runtime(runtime_file, expected_challenge=old_record.challenge)

    listener: socket.socket | None = None
    challenge = secrets.token_hex(16)
    token = secrets.token_hex(16)
    try:
        try:
            listener = bind_loopback(args.port)
        except (OSError, ValueError) as exc:
            raise ArxivFilterError(
                ErrorCode.LAUNCH_FAILED,
                details={"reason": "loopback_bind_failed"},
                message_zh="无法在本机回环地址启动服务，请检查端口是否可用后重试。",
            ) from exc

        port = int(listener.getsockname()[1])
        record = RuntimeRecord(pid=os.getpid(), port=port, challenge=challenge)

        def record_ready() -> None:
            write_runtime(runtime_file, record)

        asyncio.run(
            serve_application(
                listener,
                port=port,
                auth_token=token,
                challenge=challenge,
                log_level=args.log_level,
                on_ready=record_ready,
                open_browser=not args.no_browser,
            )
        )
        return 0
    except ArxivFilterError:
        raise
    except Exception as exc:  # noqa: BLE001 - startup details stay in the local log
        logger.exception("本地服务启动失败", exc_info=exc)
        raise ArxivFilterError(ErrorCode.LAUNCH_FAILED) from exc
    finally:
        remove_runtime(runtime_file, expected_challenge=challenge)
        if listener is not None:
            listener.close()
        lock.release()


def main(argv: Sequence[str] | None = None) -> int:
    """解析参数并分派。返回进程退出码。"""
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.version:
        print(version_text())
        return 0

    if args.data_dir:
        import os

        os.environ["ARXIV_FILTER_DATA_DIR"] = args.data_dir

    try:
        return _run_server(args)
    except ArxivFilterError as exc:
        print(exc.message_zh, file=sys.stderr)
        return 1


__all__ = ["PROG", "build_parser", "configure_stdio", "main", "version_text"]
