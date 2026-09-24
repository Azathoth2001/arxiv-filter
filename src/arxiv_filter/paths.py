"""只读资源与可写数据路径解析。

方案 §3.1 与 §8.3 的硬规则：

* 可写文件**一律**写 ``%LOCALAPPDATA%\\ArxivFilter``，绝不写 exe 所在目录或 ``sys._MEIPASS``；
* 只读资源（static、data、migrations）在源码模式和 frozen 模式下都能找到；
* 便携模式只在 exe 同目录存在 ``portable.flag`` 且该目录可写时生效；位于
  Program Files 或无写权限时**拒绝**便携模式并说明中文原因。
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path

#: ``%LOCALAPPDATA%`` 下的目录名（方案 §3.1）。
APP_DIR_NAME = "ArxivFilter"

#: 便携模式标记文件名。
PORTABLE_FLAG = "portable.flag"

#: 便携模式下的数据子目录名。
PORTABLE_DATA_DIR = "data"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 冻结产物中。"""
    return bool(getattr(sys, "frozen", False))


def executable_dir() -> Path:
    """exe 所在目录；源码模式返回项目根目录。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    # src/arxiv_filter/paths.py -> 项目根
    return Path(__file__).resolve().parents[2]


def resource_root() -> Path:
    """只读资源的根目录。

    onefile 模式解压到 ``sys._MEIPASS``；onedir 模式资源随 ``_internal``；
    源码模式直接落在包目录。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        # PyInstaller 把 datas 放在 _MEIPASS 下的同名相对路径
        candidate = Path(meipass) / "arxiv_filter"
        if candidate.is_dir():
            return candidate
        return Path(meipass)
    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    """定位一个随包发布的只读资源，例如 ``resource_path("data", "categories.zh-CN.json")``。"""
    return resource_root().joinpath(*parts)


@dataclass(frozen=True, slots=True)
class PortableDecision:
    """便携模式的判定结果。"""

    enabled: bool
    root: Path | None
    reason_zh: str


def evaluate_portable_mode() -> PortableDecision:
    """判定是否启用便携模式。

    只有 exe 同目录存在 ``portable.flag`` **且** 该目录可写时才启用。
    位于 Program Files（或任何无写权限位置）时拒绝并给出中文原因。
    """
    base = executable_dir()
    flag = base / PORTABLE_FLAG
    if not flag.exists():
        return PortableDecision(False, None, "未请求便携模式（缺少 portable.flag）。")

    if sys.platform == "win32":  # pragma: no cover - 平台分支
        program_files = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ]
        resolved = str(base).lower()
        for pf in program_files:
            if pf and resolved.startswith(str(pf).lower()):
                return PortableDecision(
                    False,
                    None,
                    f"便携模式已拒绝：程序位于受保护目录 {base}，Windows 不允许在此写入数据。",
                )

    if not _is_writable(base):
        return PortableDecision(
            False,
            None,
            f"便携模式已拒绝：目录 {base} 不可写，请改用普通模式或换一个可写位置解压。",
        )

    return PortableDecision(True, base / PORTABLE_DATA_DIR, "便携模式已启用。")


def _is_writable(path: Path) -> bool:
    """用真实写入探测目录是否可写（比检查只读属性可靠）。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".writeprobe-", delete=True):
            pass
        return True
    except OSError:
        return False


def data_dir() -> Path:
    """可写数据根目录。

    支持环境变量 ``ARXIV_FILTER_DATA_DIR`` 覆盖，供测试注入临时目录
    （方案 §7.4 要求每个集成场景使用临时 LocalAppData）。
    """
    override = os.environ.get("ARXIV_FILTER_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()

    decision = evaluate_portable_mode()
    if decision.enabled and decision.root is not None:
        return decision.root.resolve()

    return Path(user_data_path(APP_DIR_NAME, appauthor=False)).resolve()


def config_path() -> Path:
    return data_dir() / "config.json"


def db_path() -> Path:
    return data_dir() / "arxiv-filter.db"


def runtime_path() -> Path:
    """单实例记录：PID、端口、随机挑战（方案 §1.3）。"""
    return data_dir() / "runtime.json"


def logs_dir() -> Path:
    return data_dir() / "logs"


def log_path() -> Path:
    return logs_dir() / "app.log"


def exports_dir() -> Path:
    """用户没有另选目录时的默认下载位置。"""
    return data_dir() / "exports"


def backup_dir() -> Path:
    """迁移前的数据库备份目录，保留最近 3 个已验证备份。"""
    return data_dir() / "backup"


def migrations_dir() -> Path:
    """SQLite migration SQL 所在目录（只读资源）。"""
    return resource_path("storage", "migrations")


def static_dir() -> Path:
    """前端静态资源目录（只读资源）。"""
    return resource_path("static")


def ensure_dirs() -> None:
    """创建所有可写目录；由 Launcher 在启动早期调用一次。"""
    for directory in (
        data_dir(),
        logs_dir(),
        exports_dir(),
        backup_dir(),
    ):
        directory.mkdir(parents=True, exist_ok=True)


__all__ = [
    "APP_DIR_NAME",
    "PORTABLE_FLAG",
    "PORTABLE_DATA_DIR",
    "PortableDecision",
    "backup_dir",
    "config_path",
    "data_dir",
    "db_path",
    "ensure_dirs",
    "evaluate_portable_mode",
    "executable_dir",
    "exports_dir",
    "is_frozen",
    "log_path",
    "logs_dir",
    "migrations_dir",
    "resource_path",
    "resource_root",
    "runtime_path",
    "static_dir",
]
