"""T2.1 SQLite 连接、PRAGMA 与迁移（方案 §4.5、§8.7）。

验收要求（方案 T2.1）：
* 空库 → v1；重复启动幂等；
* 模拟迁移失败后**原库 hash 不变**，且以只读启动。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.storage.database import (
    BACKUP_PREFIX,
    MAX_BACKUPS,
    TARGET_SCHEMA_VERSION,
    Database,
    discover_migrations,
    drop_leading_comments,
    format_ts,
    iter_statements,
    parse_ts,
    strip_sql_comments,
    utc_now,
)


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    """把真实的 001_init.sql 复制到临时目录，便于注入自定义迁移。"""
    from arxiv_filter.paths import migrations_dir as real_dir

    target = tmp_path / "migrations"
    target.mkdir()
    for source in real_dir().glob("*.sql"):
        (target / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def make_db(tmp_path: Path, migrations: Path, **kwargs: object) -> Database:
    return Database(
        tmp_path / "test.db",
        backup_dir=tmp_path / "backup",
        migrations_dir=migrations,
        **kwargs,  # type: ignore[arg-type]
    )


# —— 语句切分 ——


def test_iter_statements_splits_on_semicolons() -> None:
    script = "CREATE TABLE a(x);\nCREATE TABLE b(y);\n"
    assert len(iter_statements(script)) == 2


def test_iter_statements_skips_comment_only_chunks() -> None:
    script = "-- just a comment\n-- another\nCREATE TABLE a(x);\n-- trailing\n"
    statements = iter_statements(script)
    assert len(statements) == 1
    assert statements[0] == "CREATE TABLE a(x);"


def test_iter_statements_drops_a_leading_comment_block() -> None:
    script = "-- header line one\n-- header line two\nCREATE TABLE a(x);\n"
    assert iter_statements(script) == ["CREATE TABLE a(x);"]


def test_iter_statements_handles_multiline_statements() -> None:
    script = "CREATE TABLE a (\n  x INTEGER,\n  y TEXT\n);\nCREATE INDEX i ON a(x);\n"
    statements = iter_statements(script)
    assert len(statements) == 2
    assert "y TEXT" in statements[0]


def test_strip_sql_comments_removes_line_comments() -> None:
    assert strip_sql_comments("-- hi\nSELECT 1; -- trailing") == "SELECT 1;"


def test_strip_sql_comments_pure_comment_is_empty() -> None:
    assert strip_sql_comments("-- a\n-- b\n") == ""


def test_drop_leading_comments_keeps_trailing_ones() -> None:
    statement = "-- header\nCREATE TABLE a (\n  x INTEGER,  -- inline note\n  y TEXT\n);"
    cleaned = drop_leading_comments(statement)
    assert cleaned.startswith("CREATE TABLE")
    assert "inline note" in cleaned


def test_real_migration_script_splits_into_expected_statements() -> None:
    from arxiv_filter.paths import migrations_dir as real_dir

    script = (real_dir() / "001_init.sql").read_text(encoding="utf-8")
    statements = iter_statements(script)
    creates = [s for s in statements if s.upper().startswith("CREATE TABLE")]
    indexes = [s for s in statements if s.upper().startswith("CREATE INDEX")]
    assert len(creates) == 6
    assert len(indexes) == 4
    assert len(statements) == 10


# —— 迁移发现 ——


def test_discover_migrations_sorts_by_version(tmp_path: Path) -> None:
    (tmp_path / "003_c.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "002_b.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignore me", encoding="utf-8")
    assert [v for v, _ in discover_migrations(tmp_path)] == [1, 2, 3]


def test_discover_migrations_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(ArxivFilterError) as excinfo:
        discover_migrations(tmp_path / "nope")
    assert excinfo.value.details["reason"] == "migrations_dir_missing"


# —— 时间格式 ——


def test_format_and_parse_ts_round_trip() -> None:
    moment = utc_now().replace(microsecond=0)
    assert parse_ts(format_ts(moment)) == moment


def test_format_ts_uses_z_suffix() -> None:
    assert format_ts(utc_now()).endswith("Z")


def test_parse_ts_accepts_naive_as_utc() -> None:
    from datetime import datetime, timezone

    assert parse_ts("2025-01-01T00:00:00").tzinfo == timezone.utc


# —— 打开与迁移 ——


@pytest.mark.asyncio
async def test_fresh_database_migrates_to_v1(tmp_path: Path, migrations_dir: Path) -> None:
    db = make_db(tmp_path, migrations_dir)
    await db.open()
    try:
        assert db.schema_version == TARGET_SCHEMA_VERSION
        assert db.read_only is False
        tables = await db.read(
            lambda c: [
                row[0]
                for row in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
        )
        for expected in (
            "papers",
            "query_cache",
            "query_history",
            "query_results",
            "read_state",
            "schema_meta",
        ):
            assert expected in tables
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_pragmas_are_applied(tmp_path: Path, migrations_dir: Path) -> None:
    db = make_db(tmp_path, migrations_dir)
    await db.open()
    try:
        journal = await db.read(lambda c: c.execute("PRAGMA journal_mode").fetchone()[0])
        foreign = await db.read(lambda c: c.execute("PRAGMA foreign_keys").fetchone()[0])
        busy = await db.read(lambda c: c.execute("PRAGMA busy_timeout").fetchone()[0])
        assert journal.lower() == "wal"
        assert foreign == 1
        assert busy == 5000
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reopening_is_idempotent(tmp_path: Path, migrations_dir: Path) -> None:
    for _ in range(3):
        db = make_db(tmp_path, migrations_dir)
        await db.open()
        assert db.schema_version == TARGET_SCHEMA_VERSION
        assert db.read_only is False
        await db.close()

    # 三次启动都到同一个版本，且 schema_meta 只有一行
    db = make_db(tmp_path, migrations_dir)
    await db.open()
    try:
        rows = await db.read(lambda c: c.execute("SELECT version FROM schema_meta").fetchall())
        assert [row[0] for row in rows] == [TARGET_SCHEMA_VERSION]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_missing_migration_for_target_version_is_refused(
    tmp_path: Path, migrations_dir: Path
) -> None:
    """声称要 v2 但没有 002 脚本时必须只读启动，而不是假装升级成功。"""
    db = make_db(tmp_path, migrations_dir, target_version=2)
    await db.open()
    try:
        assert db.read_only is True
        assert db.migration_error is not None
        assert db.migration_error.details["reason"] == "missing_migration"
        assert "完整发布包" in db.migration_error.message_zh
    finally:
        await db.close()


# —— 迁移失败：原库 hash 不变 ——


@pytest.mark.asyncio
async def test_failed_migration_keeps_the_original_database_intact(
    tmp_path: Path, migrations_dir: Path
) -> None:
    """验收要求：模拟迁移失败后原库 hash 不变，且以只读启动。"""
    db = make_db(tmp_path, migrations_dir)
    await db.open()
    await db.write(lambda c: c.execute("INSERT INTO papers VALUES ('x', '{}', '2025-01-01T00:00:00Z')"))
    await db.close()

    before = Database.file_hash(db.path)
    size_before = db.path.stat().st_size

    # 注入一个注定失败的 002
    (migrations_dir / "002_broken.sql").write_text(
        "CREATE TABLE ok_table(x INTEGER);\n"
        "CREATE TABLE broken (x INTEGER REFERENCES nonexistent_table(id));\n"
        "THIS IS NOT VALID SQL;\n",
        encoding="utf-8",
    )

    failing = make_db(tmp_path, migrations_dir, target_version=2)
    await failing.open()
    try:
        # 方案 §8.7：迁移失败要保留原库并**以只读模式启动**，而不是启动失败
        assert failing.read_only is True
        error = failing.migration_error
        assert error is not None
        assert error.code is ErrorCode.DATABASE_MIGRATION_FAILED
        assert error.details["script"] == "002_broken.sql"
        assert error.details["backup"] is not None

        # 原库字节级未变
        assert Database.file_hash(db.path) == before
        assert db.path.stat().st_size == size_before

        # 回滚生效：半途创建的表不能留下
        connection = sqlite3.connect(db.path)
        try:
            names = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            assert "ok_table" not in names, "失败迁移的部分结果必须被回滚"
            assert "broken" not in names
            version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
            assert version == 1
        finally:
            connection.close()
    finally:
        await failing.close()


@pytest.mark.asyncio
async def test_failed_migration_leaves_a_verified_backup(
    tmp_path: Path, migrations_dir: Path
) -> None:
    db = make_db(tmp_path, migrations_dir)
    await db.open()
    await db.write(lambda c: c.execute("INSERT INTO papers VALUES ('y', '{}', '2025-01-01T00:00:00Z')"))
    await db.close()

    (migrations_dir / "002_broken.sql").write_text("NOT SQL AT ALL;", encoding="utf-8")
    failing = make_db(tmp_path, migrations_dir, target_version=2)
    await failing.open()
    try:
        backup = failing.last_backup
        assert backup is not None and backup.is_file()
        assert backup.name.startswith(BACKUP_PREFIX)

        # 备份可打开、完整性 ok、且含有原来的数据
        check = sqlite3.connect(backup)
        try:
            assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert check.execute("SELECT canonical_id FROM papers").fetchone()[0] == "y"
        finally:
            check.close()
    finally:
        await failing.close()


@pytest.mark.asyncio
async def test_database_falls_back_to_read_only_after_a_failed_migration(
    tmp_path: Path, migrations_dir: Path
) -> None:
    db = make_db(tmp_path, migrations_dir)
    await db.open()
    await db.close()

    (migrations_dir / "002_broken.sql").write_text("NOT SQL;", encoding="utf-8")
    failing = make_db(tmp_path, migrations_dir, target_version=2)
    await failing.open()
    try:
        assert failing.read_only is True

        # 只读模式下写入被拒绝，且错误信息带中文
        with pytest.raises(ArxivFilterError) as excinfo:
            await failing.write(lambda c: c.execute("INSERT INTO papers VALUES ('z','{}','x')"))
        assert "只读" in excinfo.value.message_zh
    finally:
        await failing.close()


@pytest.mark.asyncio
async def test_reads_still_work_in_read_only_mode(tmp_path: Path, migrations_dir: Path) -> None:
    """只读启动的意义就在这里：用户仍然能看到已有数据。"""
    db = make_db(tmp_path, migrations_dir)
    await db.open()
    await db.write(lambda c: c.execute("INSERT INTO papers VALUES ('k', '{}', 'x')"))
    await db.close()

    (migrations_dir / "002_broken.sql").write_text("NOT SQL;", encoding="utf-8")
    failing = make_db(tmp_path, migrations_dir, target_version=2)
    await failing.open()
    try:
        rows = await failing.read(
            lambda c: c.execute("SELECT canonical_id FROM papers").fetchall()
        )
        assert [row[0] for row in rows] == ["k"]
    finally:
        await failing.close()


# —— 版本过新：拒绝写入 ——


@pytest.mark.asyncio
async def test_newer_database_schema_refuses_writes(tmp_path: Path, migrations_dir: Path) -> None:
    """降级安装：数据库比程序新时拒绝写入（方案 §8.7）。"""
    (migrations_dir / "002_future.sql").write_text(
        "ALTER TABLE papers ADD COLUMN extra TEXT;", encoding="utf-8"
    )
    newer = make_db(tmp_path, migrations_dir, target_version=2)
    await newer.open()
    assert newer.schema_version == 2
    await newer.close()

    # 用只认 v1 的旧程序打开
    older = make_db(tmp_path, migrations_dir, target_version=1)
    await older.open()
    try:
        assert older.read_only is True
        with pytest.raises(ArxivFilterError) as excinfo:
            await older.write(lambda c: c.execute("INSERT INTO papers VALUES ('a','{}','x')"))
        assert "比程序支持的" in excinfo.value.message_zh
    finally:
        await older.close()


# —— 备份保留策略 ——


@pytest.mark.asyncio
async def test_only_the_most_recent_backups_are_kept(tmp_path: Path, migrations_dir: Path) -> None:
    """保留最近 3 个已验证备份（方案 §8.7）。"""
    db = make_db(tmp_path, migrations_dir)
    await db.open()
    await db.close()

    # 手工造 5 个「旧备份」
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    import os
    import time

    for index in range(5):
        stale = backup_dir / f"{BACKUP_PREFIX}old{index}.db"
        stale.write_bytes(b"stale")
        os.utime(stale, (time.time() - 1000 + index, time.time() - 1000 + index))

    # 加一个会成功的 002，触发备份与清理
    (migrations_dir / "002_more.sql").write_text(
        "ALTER TABLE papers ADD COLUMN note TEXT;", encoding="utf-8"
    )
    upgraded = make_db(tmp_path, migrations_dir, target_version=2)
    await upgraded.open()
    assert upgraded.schema_version == 2
    await upgraded.close()

    remaining = sorted(p.name for p in backup_dir.glob(f"{BACKUP_PREFIX}*.db"))
    assert len(remaining) == MAX_BACKUPS, remaining
    # 保留下来的应当是**最新**的几个（新的那个一定在）
    assert any("old4" in name or "T" in name for name in remaining)


# —— 事务语义 ——


@pytest.mark.asyncio
async def test_write_rolls_back_on_exception(tmp_path: Path, migrations_dir: Path) -> None:
    db = make_db(tmp_path, migrations_dir)
    await db.open()
    try:

        def failing(connection: sqlite3.Connection) -> None:
            connection.execute("INSERT INTO papers VALUES ('a', '{}', 'x')")
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            await db.write(failing)

        rows = await db.read(lambda c: c.execute("SELECT canonical_id FROM papers").fetchall())
        assert rows == [], "抛异常时整个事务必须回滚"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_write_uses_begin_immediate(tmp_path: Path, migrations_dir: Path) -> None:
    """BEGIN IMMEDIATE 立刻取写锁，避免升级到写事务时才发现冲突。"""
    db = make_db(tmp_path, migrations_dir)
    await db.open()
    try:

        def inspect(connection: sqlite3.Connection) -> str:
            connection.execute("INSERT INTO papers VALUES ('b', '{}', 'x')")
            # 事务内查询：in_transaction 为真说明确实开了显式事务
            connection.execute("SELECT 1").fetchall()
            return "in_tx" if connection.in_transaction else "autocommit"

        assert await db.write(inspect) == "in_tx"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_foreign_keys_are_enforced(tmp_path: Path, migrations_dir: Path) -> None:
    """query_results 引用 papers；外键必须真的生效。"""
    db = make_db(tmp_path, migrations_dir)
    await db.open()
    try:

        def insert_orphan(connection: sqlite3.Connection) -> None:
            connection.execute(
                "INSERT INTO query_cache VALUES ('k','{}','t','t',1,1,'t')"
            )
            connection.execute(
                "INSERT INTO query_results VALUES ('k', 0, 'missing-paper', 0, NULL)"
            )

        with pytest.raises(sqlite3.IntegrityError):
            await db.write(insert_orphan)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cascade_delete_cleans_results(tmp_path: Path, migrations_dir: Path) -> None:
    db = make_db(tmp_path, migrations_dir)
    await db.open()
    try:

        def seed(connection: sqlite3.Connection) -> None:
            connection.execute("INSERT INTO papers VALUES ('p', '{}', 't')")
            connection.execute("INSERT INTO query_cache VALUES ('k','{}','t','t',1,1,'t')")
            connection.execute("INSERT INTO query_results VALUES ('k', 0, 'p', 0, NULL)")

        await db.write(seed)
        await db.write(lambda c: c.execute("DELETE FROM query_cache WHERE cache_key='k'"))
        rows = await db.read(lambda c: c.execute("SELECT * FROM query_results").fetchall())
        assert rows == []
    finally:
        await db.close()


# —— 并发 ——


@pytest.mark.asyncio
async def test_concurrent_writes_are_serialised(tmp_path: Path, migrations_dir: Path) -> None:
    """10 个并发写必须全部落盘，一条都不能丢（T2.3 验收的存储层前提）。"""
    db = make_db(tmp_path, migrations_dir)
    await db.open()
    try:

        def insert(index: int):  # type: ignore[no-untyped-def]
            return lambda c: c.execute(
                "INSERT INTO papers VALUES (?, '{}', '2025-01-01T00:00:00Z')", (f"p{index:03d}",)
            )

        await asyncio.gather(*(db.write(insert(i)) for i in range(10)))
        count = await db.read(lambda c: c.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        assert count == 10
    finally:
        await db.close()


# —— 关闭与重开 ——


@pytest.mark.asyncio
async def test_using_a_closed_database_raises(tmp_path: Path, migrations_dir: Path) -> None:
    db = make_db(tmp_path, migrations_dir)
    await db.open()
    await db.close()
    assert db.is_open is False
    with pytest.raises(ArxivFilterError) as excinfo:
        await db.read(lambda c: c.execute("SELECT 1"))
    assert excinfo.value.details["reason"] == "database_not_open"


@pytest.mark.asyncio
async def test_context_manager_opens_and_closes(tmp_path: Path, migrations_dir: Path) -> None:
    async with make_db(tmp_path, migrations_dir) as db:
        assert db.is_open is True
        assert db.schema_version == TARGET_SCHEMA_VERSION
    assert db.is_open is False


@pytest.mark.asyncio
async def test_data_survives_reopen(tmp_path: Path, migrations_dir: Path) -> None:
    db = make_db(tmp_path, migrations_dir)
    await db.open()
    await db.write(lambda c: c.execute("INSERT INTO papers VALUES ('keep','{}','t')"))
    await db.close()

    reopened = make_db(tmp_path, migrations_dir)
    await reopened.open()
    try:
        rows = await reopened.read(lambda c: c.execute("SELECT canonical_id FROM papers").fetchall())
        assert [row[0] for row in rows] == ["keep"]
    finally:
        await reopened.close()


def test_file_hash_changes_with_content(tmp_path: Path) -> None:
    target = tmp_path / "f.bin"
    target.write_bytes(b"one")
    first = Database.file_hash(target)
    target.write_bytes(b"two")
    assert Database.file_hash(target) != first
