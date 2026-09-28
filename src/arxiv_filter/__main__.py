"""``python -m arxiv_filter`` 与 ``arxiv-filter`` 控制台脚本的共同入口。

方案第 3 部分规定本模块只做两件事：

1. 调用 :func:`multiprocessing.freeze_support`，让 PyInstaller onefile 不重复拉起进程；
2. 把参数交给 :mod:`arxiv_filter.launcher`。

完整 Launcher（单实例、端口、健康检查、开浏览器）由 ``launcher`` 提供。
"""

from __future__ import annotations

import multiprocessing
import sys


def main(argv: list[str] | None = None) -> int:
    """进程入口，返回进程退出码。"""
    # 必须在任何 multiprocessing 使用之前调用；在非 frozen 环境下是空操作。
    multiprocessing.freeze_support()

    from arxiv_filter.launcher import main as launcher_main

    return launcher_main(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
