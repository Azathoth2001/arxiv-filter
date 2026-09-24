"""启动器：命令行入口、单实例、端口绑定、健康检查、打开浏览器。

阶段 0（T0.4）只交付 **命令行契约**：``--version`` 必须工作，且参数解析在
没有服务端实现时也能给出中文提示。单实例、端口、令牌、健康检查和浏览器
拉起属于 T4.3，届时填充 :func:`_run_server`。
"""

from __future__ import annotations

import argparse
import platform
import sys
from typing import Sequence

from arxiv_filter import SCHEMA_VERSION, __version__
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.paths import data_dir, log_path

PROG = "arxiv-filter"

#: 服务端实现完成后由 T4.3 置为 True；在此之前 ``python -m arxiv_filter`` 只打印提示。
_SERVER_IMPLEMENTED = False


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


def _run_server(args: argparse.Namespace) -> int:  # pragma: no cover - T4.3
    """启动本地服务。由 T4.3 实现（单实例、token、健康检查、开浏览器）。"""
    raise ArxivFilterError(
        ErrorCode.LAUNCH_FAILED,
        details={"phase": "T4.3", "data_dir": str(data_dir())},
        message_zh=(
            "阶段 0 只交付工程骨架，Web 服务尚未实现（计划在 T4.3 完成）。\n"
            f"当前可写数据目录：{data_dir()}\n"
            f"日志将写入：{log_path()}\n"
            f"可用参数：--version、--help。"
        ),
    )


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
