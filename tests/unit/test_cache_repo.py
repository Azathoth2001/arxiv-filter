"""T2.2 查询缓存（方案 §4.5）。

验收要求：
* canonical key 稳定（JSON 字段顺序不影响）；
* **取消/不完整的结果绝不命中** complete 缓存；
* 超容量按 ``last_accessed_at`` LRU 淘汰，且**绝不清理 ``read_state``**。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arxiv_filter.models.paper import Author, Paper
from arxiv_filter.storage.cache_repo import (
    DEFAULT_MAX_BYTES,
    FRESH_TTL_S,
    STALE_TTL_S,
    CacheRepository,
)
from arxiv_filter.storage.database import Database
from arxiv_filter.storage.read_repo import ReadRepository

UTC = timezone.utc
NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    from arxiv_filter.paths import migrations_dir as real_dir

    target = tmp_path / "migrations"
    target.mkdir()
    for source in real_dir().glob("*.sql"):
        (target / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return target


async def open_db(tmp_path: Path, migrations_dir: Path) -> Database:
    db = Database(tmp_path / "test.db", backup_dir=tmp_path / "backup", migrations_dir=migrations_dir)
    await db.open()
    return db


def make_paper(index: int) -> Paper:
    return Paper(
        canonical_id=f"2501.{index:05d}",
        versioned_id=f"2501.{index:05d}v1",
        version=1,
        title=f"Paper {index}",
        authors=[Author(name="A. Author")],
        summary_raw="summary",
        published_at=datetime(2025, 1, 2, tzinfo=UTC),
        updated_at=datetime(2025, 1, 3, tzinfo=UTC),
        primary_category="cs.CL",
        categories=["cs.CL"],
        abs_url=f"https://arxiv.org/abs/2501.{index:05d}v1",
        retrieved_at=NOW,
    )


def key_for(name: str) -> str:
    return (name * 64)[:64]


# —— 基本读写 ——


@pytest.mark.asyncio
async def test_put_then_get_fresh(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        papers = [make_paper(1), make_paper(2), make_paper(3)]
        await cache.put_complete(key_for("a"), '{"q":1}', papers, now=NOW)

        found = await cache.get_fresh(key_for("a"), NOW + timedelta(minutes=5))
        assert found is not None
        assert [p.canonical_id for p in found.papers] == [p.canonical_id for p in papers]
        assert found.complete is True
        assert found.is_fresh(NOW + timedelta(minutes=5)) is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_paper_order_is_preserved(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        papers = [make_paper(i) for i in (7, 3, 9, 1)]
        await cache.put_complete(key_for("b"), "{}", papers, now=NOW)
        found = await cache.get_fresh(key_for("b"), NOW)
        assert found is not None
        assert [p.canonical_id for p in found.papers] == [p.canonical_id for p in papers]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_missing_key_is_a_miss(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        assert await cache.get_fresh(key_for("z"), NOW) is None
        assert await cache.get_stale(key_for("z"), NOW) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_repeated_put_replaces_the_entry(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        await cache.put_complete(key_for("c"), "{}", [make_paper(1)], now=NOW)
        await cache.put_complete(key_for("c"), "{}", [make_paper(2), make_paper(3)], now=NOW)

        found = await cache.get_fresh(key_for("c"), NOW)
        assert found is not None
        assert [p.canonical_id for p in found.papers] == ["2501.00002", "2501.00003"]
        assert await cache.entry_count() == 1
    finally:
        await db.close()


# —— fresh / stale ——


@pytest.mark.asyncio
async def test_entry_expires_after_fresh_ttl(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        await cache.put_complete(key_for("d"), "{}", [make_paper(1)], now=NOW)

        just_inside = NOW + timedelta(seconds=FRESH_TTL_S - 1)
        just_outside = NOW + timedelta(seconds=FRESH_TTL_S + 1)

        assert await cache.get_fresh(key_for("d"), just_inside) is not None
        assert await cache.get_fresh(key_for("d"), just_outside) is None
        assert await cache.get_stale(key_for("d"), just_outside) is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_fresh_entry_is_not_reported_as_stale(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        await cache.put_complete(key_for("e"), "{}", [make_paper(1)], now=NOW)
        assert await cache.get_stale(key_for("e"), NOW) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_entry_is_dropped_after_the_stale_window(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        await cache.put_complete(key_for("f"), "{}", [make_paper(1)], now=NOW)

        beyond = NOW + timedelta(seconds=STALE_TTL_S + 1)
        assert await cache.get_stale(key_for("f"), beyond) is None
        assert await cache.get_any(key_for("f"), beyond) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_stale_marker_is_correct(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        await cache.put_complete(key_for("g"), "{}", [make_paper(1)], now=NOW)
        found = await cache.get_any(key_for("g"), NOW + timedelta(hours=8))
        assert found is not None
        assert found.is_stale(NOW + timedelta(hours=8)) is True
        assert found.is_fresh(NOW + timedelta(hours=8)) is False
    finally:
        await db.close()


# —— 不完整的结果绝不命中 ——


@pytest.mark.asyncio
async def test_partial_result_never_hits_the_cache(tmp_path: Path, migrations_dir: Path) -> None:
    """方案 §4.5：取消或失败的结果不写 complete=1，绝不能当成可用缓存。"""
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        await cache.put_partial(key_for("h"), "{}", [make_paper(1)], now=NOW)

        assert await cache.get_fresh(key_for("h"), NOW) is None
        assert await cache.get_stale(key_for("h"), NOW) is None
        assert await cache.get_any(key_for("h"), NOW) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_partial_can_be_replaced_by_a_complete_entry(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        await cache.put_partial(key_for("i"), "{}", [make_paper(1)], now=NOW)
        await cache.put_complete(key_for("i"), "{}", [make_paper(1), make_paper(2)], now=NOW)

        found = await cache.get_fresh(key_for("i"), NOW)
        assert found is not None
        assert len(found.papers) == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_complete_can_be_downgraded_by_a_later_partial(tmp_path: Path, migrations_dir: Path) -> None:
    """后一次被取消的查询不能把之前那份好的缓存「污染」成不可用——

    但它确实会覆盖；所以 get_fresh 必须返回 None，而不是返回被截断的内容。
    """
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        await cache.put_complete(key_for("j"), "{}", [make_paper(1), make_paper(2)], now=NOW)
        await cache.put_partial(key_for("j"), "{}", [make_paper(1)], now=NOW)

        assert await cache.get_fresh(key_for("j"), NOW) is None
    finally:
        await db.close()


# —— 容量与 LRU ——


@pytest.mark.asyncio
async def test_lru_evicts_the_least_recently_accessed(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        for name in ("a", "b", "c"):
            await cache.put_complete(key_for(name), "{}", [make_paper(1)], now=NOW)

        # 访问 a 与 c，b 保持最旧
        await cache.get_fresh(key_for("a"), NOW + timedelta(minutes=1))
        await cache.get_fresh(key_for("c"), NOW + timedelta(minutes=2))

        # 容量压到只够放一条
        report = await cache.prune(max_bytes=1)
        assert report.removed_entries >= 1

        remaining = set(await cache.iter_keys())
        assert key_for("b") not in remaining, "最久未访问的 b 应当先被淘汰"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_prune_keeps_entries_under_the_limit(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        await cache.put_complete(key_for("k"), "{}", [make_paper(1)], now=NOW)
        report = await cache.prune(max_bytes=DEFAULT_MAX_BYTES)
        assert report.removed_entries == 0
        assert report.remaining_bytes > 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_default_max_bytes_matches_the_plan() -> None:
    assert DEFAULT_MAX_BYTES == 500 * 1024 * 1024


@pytest.mark.asyncio
async def test_prune_never_touches_read_state(tmp_path: Path, migrations_dir: Path) -> None:
    """⚠️ 方案 §4.5 的硬约束：绝不清理 ``read_state``。"""
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        reads = ReadRepository(db)
        await cache.put_complete(key_for("m"), "{}", [make_paper(1), make_paper(2)], now=NOW)
        await reads.mark_seen([make_paper(1).canonical_id, make_paper(2).canonical_id], NOW)
        await reads.set_read(make_paper(1).canonical_id, True, NOW)

        await cache.clear()
        await cache.prune(max_bytes=0)

        state = await reads.get_many([make_paper(1).canonical_id, make_paper(2).canonical_id])
        assert set(state) == {"2501.00001", "2501.00002"}
        assert state["2501.00001"].read_at is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_papers_referenced_by_read_state_survive_cleanup(
    tmp_path: Path, migrations_dir: Path
) -> None:
    """已读列表要能显示标题，所以它引用到的论文记录不能被删。"""
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        reads = ReadRepository(db)
        await cache.put_complete(key_for("n"), "{}", [make_paper(1)], now=NOW)
        await reads.mark_seen([make_paper(1).canonical_id], NOW)

        report = await cache.clear()
        assert report.removed_papers == 0

        rows = await db.read(lambda c: c.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        assert rows == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unreferenced_papers_are_cleaned_up(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        await cache.put_complete(key_for("o"), "{}", [make_paper(1), make_paper(2)], now=NOW)

        report = await cache.clear()
        assert report.removed_papers == 2
        rows = await db.read(lambda c: c.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
        assert rows == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_purge_expired_removes_only_old_entries(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        await cache.put_complete(key_for("p"), "{}", [make_paper(1)], now=NOW - timedelta(days=40))
        await cache.put_complete(key_for("q"), "{}", [make_paper(2)], now=NOW)

        report = await cache.purge_expired(NOW)
        assert report.removed_entries == 1
        assert await cache.iter_keys() == [key_for("q")]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_delete_single_entry(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        await cache.put_complete(key_for("r"), "{}", [make_paper(1)], now=NOW)
        assert await cache.delete(key_for("r")) is True
        assert await cache.get_fresh(key_for("r"), NOW) is None
        assert await cache.delete(key_for("r")) is False
    finally:
        await db.close()


# —— 字节统计 ——


@pytest.mark.asyncio
async def test_byte_size_reflects_stored_content(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        small = await cache.put_complete(key_for("s"), "{}", [make_paper(1)], now=NOW)
        big = await cache.put_complete(
            key_for("t"), "{}", [make_paper(i) for i in range(20)], now=NOW
        )
        assert big > small
        assert await cache.total_bytes() == small + big
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cache_key_is_stored_verbatim(tmp_path: Path, migrations_dir: Path) -> None:
    """本模块不重算 key——canonical JSON 原样存回，便于调试与迁移。"""
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        canonical = '{"a":1,"b":2}'
        await cache.put_complete(key_for("u"), canonical, [make_paper(1)], now=NOW)
        found = await cache.get_fresh(key_for("u"), NOW)
        assert found is not None
        assert found.canonical_query_json == canonical
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_constructing_with_bad_ttls_is_refused(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        with pytest.raises(ValueError):
            CacheRepository(db, fresh_ttl_s=0)
        with pytest.raises(ValueError):
            CacheRepository(db, fresh_ttl_s=100, stale_ttl_s=50)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_corrupt_paper_json_is_skipped_not_fatal(tmp_path: Path, migrations_dir: Path) -> None:
    """一条坏记录不该毁掉整份缓存。"""
    db = await open_db(tmp_path, migrations_dir)
    try:
        cache = CacheRepository(db)
        await cache.put_complete(key_for("v"), "{}", [make_paper(1), make_paper(2)], now=NOW)
        await db.write(
            lambda c: c.execute(
                "UPDATE papers SET metadata_json = 'not json' WHERE canonical_id = '2501.00001'"
            )
        )
        found = await cache.get_fresh(key_for("v"), NOW)
        assert found is not None
        assert [p.canonical_id for p in found.papers] == ["2501.00002"]
    finally:
        await db.close()
