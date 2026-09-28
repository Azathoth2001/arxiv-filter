"""SQLite 连接、PRAGMA 与版本化迁移（方案 §4.5 T2.1）。

设计要点（全部来自方案 §4.5 与 ``docs/data-schema.md`` §3）：

* 启动执行 ``PRAGMA journal_mode=WAL``、``foreign_keys=ON``、``busy_timeout=5000``；
* **单连接 + 进程内 ``asyncio.Lock``** 串行化所有访问（单用户本地应用，正确性优先）；
* 每个工作单元在**工作线程里**用 ``BEGIN IMMEDIATE`` 开短事务——
  **绝不跨 await 持有事务**。因此对外暴露的是 :meth:`Database.read` /
  :meth:`Database.write`，接受一个同步函数，而不是一个 async 上下文管理器；
* 迁移前用 ``sqlite3.Connection.backup()`` 写时间戳备份并**验证能重新打开**；
* 每个 migration 一个事务，**成功后才**更新 ``schema_meta``；
* 失败：回滚、保留原库、以**只读**模式启动、中文提示备份路径，绝不自动删库；
* 保留最近 3 个已验证备份；
* 数据库 schema 比程序新 → 拒绝写入并提示使用匹配版本或恢复备份。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

import anyio.to_thread

from arxiv_filter.errors import ArxivFilterError, ErrorCode

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: 本程序期望的 schema 版本（等于已发布的 migration 数量）。
TARGET_SCHEMA_VERSION = 1

#: 保留的已验证备份数量（方案 §8.7）。
MAX_BACKUPS = 3

#: 备份文件名的前缀。
BACKUP_PREFIX = "arxiv-filter-"

# Only these tables contain disposable query-cache state. User-owned read state,
# search history, and schema metadata are deliberately excluded from repair.
CACHE_TABLES = frozenset({"papers", "query_cache", "query_results"})
DURABLE_TABLES = frozenset({"schema_meta", "query_history", "read_state"})
SCHEMA_TABLES = CACHE_TABLES | DURABLE_TABLES


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_ts(value: datetime) -> str:
    """RFC 3339 UTC，秒精度，固定 ``Z`` 结尾（``docs/data-schema.md`` §0）。"""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def strip_sql_comments(statement: str) -> str:
    """去掉 ``--`` 行注释后返回剩余内容，用于判断语句是否为空。"""
    lines = []
    for line in statement.splitlines():
        index = line.find("--")
        lines.append(line if index == -1 else line[:index])
    return "".join(lines).strip()


def drop_leading_comments(statement: str) -> str:
    """去掉开头的整行注释与空行，保留语句内的行尾注释。

    SQLite 本来就能接受前导注释，但把注释块和第一条语句粘在一起会让
    「这条语句是什么」难以判断（例如统计 ``CREATE TABLE`` 的数量）。
    """
    lines = statement.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if strip_sql_comments(line):
            start = index
            break
    else:
        return ""
    return "\n".join(lines[start:]).strip()


def iter_statements(script: str) -> list[str]:
    """把迁移脚本切成一条条语句。

    **不能**用 :meth:`sqlite3.Connection.executescript`：它在执行前会隐式
    ``COMMIT`` 掉外层事务，导致迁移失败时无法回滚——那正是要防的事。
    这里用标准库的 :func:`sqlite3.complete_statement` 逐行累积，正确识别语句边界。
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            cleaned = drop_leading_comments(buffer)
            if cleaned:
                statements.append(cleaned)
            buffer = ""
    cleaned = drop_leading_comments(buffer)
    if cleaned:
        statements.append(cleaned)
    return statements


def discover_migrations(directory: Path) -> list[tuple[int, Path]]:
    """扫描 ``NNN_name.sql``，返回按版本号升序的 ``(version, path)``。"""
    if not directory.is_dir():
        raise ArxivFilterError(
            ErrorCode.DATABASE_MIGRATION_FAILED,
            details={"reason": "migrations_dir_missing", "path": str(directory)},
        )
    found: list[tuple[int, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        prefix = path.stem.split("_", 1)[0]
        if not prefix.isdigit():
            continue
        found.append((int(prefix), path))
    found.sort(key=lambda item: item[0])
    return found


class Database:
    """一个 SQLite 数据库的串行化访问入口。"""

    def __init__(
        self,
        path: Path,
        *,
        backup_dir: Path | None = None,
        migrations_dir: Path | None = None,
        target_version: int = TARGET_SCHEMA_VERSION,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self.path = Path(path)
        self.backup_dir = Path(backup_dir) if backup_dir is not None else self.path.parent / "backup"
        self.migrations_dir = Path(migrations_dir) if migrations_dir is not None else _default_migrations_dir()
        self.target_version = target_version
        self.busy_timeout_ms = busy_timeout_ms

        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._read_only = False
        self._schema_version = 0
        self._last_backup: Path | None = None
        self._migration_error: ArxivFilterError | None = None

    # —— 只读状态 ——

    @property
    def read_only(self) -> bool:
        """是否以只读模式运行（迁移失败或数据库版本过新）。"""
        return self._read_only

    @property
    def schema_version(self) -> int:
        return self._schema_version

    @property
    def last_backup(self) -> Path | None:
        """最近一次成功验证的备份路径，用于中文提示。"""
        return self._last_backup

    @property
    def migration_error(self) -> ArxivFilterError | None:
        """导致只读启动的迁移错误；正常启动时为 ``None``。

        :meth:`open` **不会**因为迁移失败而抛异常——方案 §8.7 要求
        「保留原库并以只读模式启动」，所以程序要能继续跑，
        由 Launcher 读这个属性决定给用户看什么提示。
        """
        return self._migration_error

    @property
    def is_open(self) -> bool:
        return self._connection is not None

    # —— 生命周期 ——

    async def open(self) -> None:
        """连接、设置 PRAGMA、按需迁移。"""
        await anyio.to_thread.run_sync(self._open_sync)

    async def close(self) -> None:
        async with self._lock:
            connection, self._connection = self._connection, None
        if connection is not None:
            await anyio.to_thread.run_sync(connection.close)

    async def __aenter__(self) -> Database:
        await self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # —— 访问原语 ——

    async def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """在锁内执行一个只读函数。"""
        async with self._lock:
            connection = self._require_connection()
            return await anyio.to_thread.run_sync(lambda: fn(connection))

    async def write(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """在锁内、以 ``BEGIN IMMEDIATE`` 事务执行一个写函数。

        ``fn`` 必须是**同步**的：它的整个执行过程在工作线程里完成，
        因此事务生命周期内不会发生 await（方案 §4.5 的硬要求）。
        """
        if self._read_only:
            raise ArxivFilterError(
                ErrorCode.DATABASE_TOO_NEW
                if self._schema_version > self.target_version
                else ErrorCode.DATABASE_MIGRATION_FAILED,
                details={
                    "schema_version": self._schema_version,
                    "target_version": self.target_version,
                    "backup": str(self._last_backup) if self._last_backup else None,
                },
                message_zh=(
                    "数据库以只读模式打开，无法写入。\n"
                    + (
                        f"数据库版本 {self._schema_version} 比程序支持的 "
                        f"{self.target_version} 新，请使用匹配版本或恢复备份。"
                        if self._schema_version > self.target_version
                        else "上次数据库升级失败，已保留原库。"
                    )
                ),
            )

        async with self._lock:
            connection = self._require_connection()
            return await anyio.to_thread.run_sync(lambda: self._run_transaction(connection, fn))

    # —— 内部：连接 ——

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise ArxivFilterError(
                ErrorCode.INTERNAL_ERROR,
                details={"reason": "database_not_open"},
                message_zh="数据库尚未打开。",
            )
        return self._connection

    def _open_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists() and self.path.stat().st_size > 0

        connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            self._configure(connection)
            self._schema_version = self._current_version(connection)

            if self._schema_version > self.target_version:
                # 降级安装：拒绝写入，只读启动（方案 §8.7）
                logger.warning(
                    "数据库 schema 版本 %d 比程序支持的 %d 新，以只读模式启动",
                    self._schema_version,
                    self.target_version,
                )
                self._read_only = True
            elif self._schema_version < self.target_version:
                try:
                    self._schema_version = self._migrate_sync(connection, existed)
                except ArxivFilterError as exc:
                    # 方案 §8.7：迁移失败要「保留原库并以只读模式启动」——
                    # 因此**不能**关掉连接、也不能让 open() 抛出，否则用户连
                    # 历史记录都看不到。回滚已在 _migrate_sync 里完成。
                    self._read_only = True
                    self._migration_error = exc
                    self._last_backup = (
                        Path(exc.details["backup"])
                        if exc.details.get("backup")
                        else self._last_backup
                    )
                    logger.error("数据库迁移失败，已回退到只读模式：%s", exc.message_zh)

            if not self._read_only:
                try:
                    self._validate_schema_sync(connection)
                except ArxivFilterError as exc:
                    # Schema repair/validation follows the same safe startup
                    # contract as migrations: keep the connection available for
                    # reads, but refuse writes when compatibility is uncertain.
                    self._read_only = True
                    self._migration_error = exc
                    self._last_backup = (
                        Path(exc.details["backup"])
                        if exc.details.get("backup")
                        else self._last_backup
                    )
                    logger.error("数据库结构不兼容，已回退到只读模式：%s", exc.message_zh)
        except Exception:
            connection.close()
            raise

        self._connection = connection

    def _configure(self, connection: sqlite3.Connection) -> None:
        """WAL、外键、busy_timeout。"""
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_ms)}")

    def _current_version(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
        if row is None:
            return 0
        found = connection.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
        return int(found[0]) if found is not None else 0

    def _run_transaction(self, connection: sqlite3.Connection, fn: Callable[[sqlite3.Connection], T]) -> T:
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = fn(connection)
        except Exception:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
        return result

    # —— 内部：迁移 ——

    def _migrate_sync(self, connection: sqlite3.Connection, existed: bool) -> int:
        migrations = discover_migrations(self.migrations_dir)
        pending = [(version, path) for version, path in migrations if version > self._schema_version]

        if not pending:
            if self._schema_version < self.target_version:
                raise ArxivFilterError(
                    ErrorCode.DATABASE_MIGRATION_FAILED,
                    details={
                        "reason": "missing_migration",
                        "current": self._schema_version,
                        "target": self.target_version,
                        "dir": str(self.migrations_dir),
                    },
                    message_zh=(
                        f"缺少升级到 schema v{self.target_version} 所需的迁移脚本，"
                        "请重新安装完整发布包。"
                    ),
                )
            return self._schema_version

        backup: Path | None = None
        if existed:
            # 已经有数据：先备份并验证可重新打开（方案 §8.7）
            backup = self._backup_sync(connection)
            self._last_backup = backup

        version = self._schema_version
        for target, script in pending:
            statements = iter_statements(script.read_text(encoding="utf-8"))
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    connection.execute(statement)
                if version == 0:
                    connection.execute("INSERT INTO schema_meta(version) VALUES (?)", (target,))
                else:
                    connection.execute("UPDATE schema_meta SET version = ?", (target,))
                connection.execute("COMMIT")
            except Exception as exc:
                connection.execute("ROLLBACK")
                raise ArxivFilterError(
                    ErrorCode.DATABASE_MIGRATION_FAILED,
                    details={
                        "reason": "migration_failed",
                        "from_version": version,
                        "target_version": target,
                        "script": script.name,
                        "backup": str(backup) if backup else None,
                        "detail": type(exc).__name__,
                    },
                    message_zh=(
                        f"数据库升级失败（{script.name}），已回滚并保留原库。\n"
                        + (
                            f"备份文件：{backup}"
                            if backup is not None
                            else "原库未改动。"
                        )
                    ),
                ) from exc
            version = target
            logger.info("已应用 migration %s（schema v%d）", script.name, target)

        if version != self.target_version:
            # 可用的迁移脚本不足以到达目标版本——不能假装升级成功
            raise ArxivFilterError(
                ErrorCode.DATABASE_MIGRATION_FAILED,
                details={
                    "reason": "missing_migration",
                    "reached": version,
                    "target": self.target_version,
                    "dir": str(self.migrations_dir),
                    "backup": str(backup) if backup else None,
                },
                message_zh=(
                    f"迁移只升级到 schema v{version}，未达到程序要求的 "
                    f"v{self.target_version}。请重新安装完整发布包。"
                ),
            )

        if backup is not None:
            self._prune_backups_sync()
        return version

    def _validate_schema_sync(self, connection: sqlite3.Connection) -> None:
        """Compare core tables with the schema implied by applied migrations.

        A cache-only mismatch can be recovered because all cache content is
        disposable. Durable-table mismatches are never auto-repaired.
        """
        try:
            expected = self._expected_schema_objects()
            actual = self._core_schema_objects(connection)
        except Exception as exc:
            raise ArxivFilterError(
                ErrorCode.DATABASE_MIGRATION_FAILED,
                details={"reason": "schema_validation_failed", "detail": type(exc).__name__},
                message_zh="无法确认数据库结构是否兼容；为保护数据，数据库已按只读方式启动。",
            ) from exc

        expected_durable = self._schema_subset(expected, DURABLE_TABLES)
        actual_durable = self._schema_subset(actual, DURABLE_TABLES)
        if not self._schemas_equal(expected_durable, actual_durable):
            raise ArxivFilterError(
                ErrorCode.DATABASE_MIGRATION_FAILED,
                details={"reason": "durable_schema_incompatible"},
                message_zh=(
                    "已读状态、搜索历史或版本记录的数据库结构与当前程序不兼容；"
                    "为避免丢失数据，数据库已按只读方式启动。"
                ),
            )

        expected_cache = self._schema_subset(expected, CACHE_TABLES)
        actual_cache = self._schema_subset(actual, CACHE_TABLES)
        if self._schemas_equal(expected_cache, actual_cache):
            return

        # Back up before touching any disposable cache data. A failed backup
        # aborts repair, leaving the existing database untouched.
        backup = self._backup_sync(connection)
        self._last_backup = backup
        try:
            self._rebuild_cache_schema_sync(connection, expected_cache)
        except Exception as exc:
            if isinstance(exc, ArxivFilterError):
                details = dict(exc.details)
                details.setdefault("backup", str(backup))
            else:
                details = {"reason": "cache_schema_rebuild_failed", "detail": type(exc).__name__}
                details["backup"] = str(backup)
            raise ArxivFilterError(
                ErrorCode.DATABASE_MIGRATION_FAILED,
                details=details,
                message_zh=(
                    "缓存结构重建失败，原数据库已回滚并保留备份；"
                    "数据库已按只读方式启动。"
                ),
            ) from exc

        logger.warning("检测到缓存 schema 不兼容，已在备份后重建缓存表：%s", backup)
        self._prune_backups_sync()

    def _expected_schema_objects(self) -> dict[tuple[str, str, str], str]:
        """Build the canonical schema in memory by replaying migrations."""
        expected = sqlite3.connect(":memory:", isolation_level=None)
        try:
            version = 0
            migrations = discover_migrations(self.migrations_dir)
            for target, script in migrations:
                if target > self.target_version:
                    break
                expected.execute("BEGIN IMMEDIATE")
                try:
                    for statement in iter_statements(script.read_text(encoding="utf-8")):
                        expected.execute(statement)
                    if version == 0:
                        expected.execute("INSERT INTO schema_meta(version) VALUES (?)", (target,))
                    else:
                        expected.execute("UPDATE schema_meta SET version = ?", (target,))
                    expected.execute("COMMIT")
                except Exception:
                    expected.execute("ROLLBACK")
                    raise
                version = target

            if version != self.target_version:
                raise ValueError("canonical migration set does not reach target version")
            return self._core_schema_objects(expected)
        finally:
            expected.close()

    @staticmethod
    def _core_schema_objects(
        connection: sqlite3.Connection,
    ) -> dict[tuple[str, str, str], str]:
        rows = connection.execute(
            """SELECT type, name, tbl_name, sql
               FROM sqlite_master
               WHERE type IN ('table', 'index', 'trigger')
                 AND name NOT LIKE 'sqlite_%'
                 AND sql IS NOT NULL"""
        ).fetchall()
        result: dict[tuple[str, str, str], str] = {}
        for row in rows:
            object_type, name, table_name, sql = row
            if name in SCHEMA_TABLES or table_name in SCHEMA_TABLES:
                result[(str(object_type), str(name), str(table_name))] = str(sql)
        return result

    @staticmethod
    def _schema_subset(
        objects: dict[tuple[str, str, str], str], tables: frozenset[str]
    ) -> dict[tuple[str, str, str], str]:
        return {
            key: sql
            for key, sql in objects.items()
            if key[1] in tables or key[2] in tables
        }

    @staticmethod
    def _schemas_equal(
        left: dict[tuple[str, str, str], str],
        right: dict[tuple[str, str, str], str],
    ) -> bool:
        if left.keys() != right.keys():
            return False
        return all(
            " ".join(left[key].split()) == " ".join(right[key].split())
            for key in left
        )

    @staticmethod
    def _rebuild_cache_schema_sync(
        connection: sqlite3.Connection,
        expected_cache: dict[tuple[str, str, str], str],
    ) -> None:
        """Replace only disposable cache tables, atomically and FK-safely."""
        expected_tables = {
            key: sql for key, sql in expected_cache.items() if key[0] == "table"
        }
        expected_auxiliary = {
            key: sql for key, sql in expected_cache.items() if key[0] != "table"
        }
        if {key[1] for key in expected_tables} != CACHE_TABLES:
            raise ValueError("canonical cache table set is incomplete")

        # SQLite only permits changing foreign_keys outside a transaction. Turn
        # it off while replacing the parent tables so cascades cannot touch
        # durable rows; verify constraints before committing the rebuilt schema.
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            for name in ("query_results", "query_cache", "papers"):
                quoted = '"' + name.replace('"', '""') + '"'
                connection.execute(f"DROP TABLE IF EXISTS {quoted}")

            table_order = {"papers": 0, "query_cache": 1, "query_results": 2}
            for key, sql in sorted(expected_tables.items(), key=lambda item: table_order[item[0][1]]):
                connection.execute(sql)
            for sql in expected_auxiliary.values():
                connection.execute(sql)

            violation = connection.execute("PRAGMA foreign_key_check").fetchone()
            if violation is not None:
                raise sqlite3.IntegrityError("rebuilt cache violates foreign-key constraints")
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    def _backup_sync(self, connection: sqlite3.Connection) -> Path:
        """写一份时间戳备份并验证它能被重新打开。"""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        target = self.backup_dir / f"{BACKUP_PREFIX}{stamp}.db"
        if target.exists():  # pragma: no cover - 同一秒内重复迁移
            target = self.backup_dir / f"{BACKUP_PREFIX}{stamp}-{id(connection) & 0xFFFF:04x}.db"

        destination = sqlite3.connect(target)
        try:
            connection.backup(destination)
        finally:
            destination.close()

        if not self._verify_backup(target):
            raise ArxivFilterError(
                ErrorCode.DATABASE_MIGRATION_FAILED,
                details={"reason": "backup_unreadable", "backup": str(target)},
                message_zh=(
                    f"数据库备份无法重新打开（{target}），已中止升级以免损坏数据。"
                ),
            )
        return target

    @staticmethod
    def _verify_backup(path: Path) -> bool:
        """验证备份可重新打开且结构完整。"""
        try:
            check = sqlite3.connect(path)
        except sqlite3.Error:
            return False
        try:
            row = check.execute("PRAGMA integrity_check").fetchone()
            if row is None or row[0] != "ok":
                return False
            check.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
            return True
        except sqlite3.Error:
            return False
        finally:
            check.close()

    def _prune_backups_sync(self) -> None:
        """只保留最近 :data:`MAX_BACKUPS` 个备份。"""
        if not self.backup_dir.is_dir():
            return
        backups = sorted(
            (p for p in self.backup_dir.glob(f"{BACKUP_PREFIX}*.db") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in backups[MAX_BACKUPS:]:
            try:
                stale.unlink()
            except OSError:  # pragma: no cover - 文件被占用
                logger.warning("无法删除旧备份 %s", stale)

    # —— 诊断 ——

    @staticmethod
    def file_hash(path: Path) -> str:
        """文件内容的 SHA256；迁移失败验收用它证明原库未被改动。"""
        import hashlib

        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()


def _default_migrations_dir() -> Path:
    from arxiv_filter.paths import migrations_dir

    return migrations_dir()


__all__ = [
    "BACKUP_PREFIX",
    "MAX_BACKUPS",
    "TARGET_SCHEMA_VERSION",
    "Database",
    "discover_migrations",
    "drop_leading_comments",
    "format_ts",
    "iter_statements",
    "parse_ts",
    "strip_sql_comments",
    "utc_now",
]
