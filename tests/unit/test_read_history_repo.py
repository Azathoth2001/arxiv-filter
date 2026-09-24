"""T2.3 已读状态与查询历史（方案 §4.5、``docs/data-schema.md`` §1/§7）。

验收要求：
* 10 个并发写**无 lost update**；
* ``mark_seen`` **不覆盖** ``read_at``；
* 导入/导出往返一致。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.identifiers import uuid7, uuid7_str, uuid7_timestamp
from arxiv_filter.storage.database import Database
from arxiv_filter.storage.history_repo import (
    MAX_HISTORY_AGE_DAYS,
    MAX_HISTORY_ENTRIES,
    HistoryRepository,
)
from arxiv_filter.storage.read_repo import EXPORT_SCHEMA_VERSION, ReadRepository, ReadState

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


# —— UUIDv7 ——


def test_uuid7_is_version_7() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == "specified in RFC 4122"


def test_uuid7_encodes_the_timestamp() -> None:
    millis = 1_700_000_000_000
    assert uuid7_timestamp(uuid7(millis)) == millis


def test_uuid7_is_time_ordered() -> None:
    early = uuid7(1_000_000_000_000)
    late = uuid7(2_000_000_000_000)
    assert early < late


def test_uuid7_rejects_out_of_range_timestamp() -> None:
    with pytest.raises(ValueError):
        uuid7(1 << 48)


def test_uuid7_is_unique() -> None:
    assert len({uuid7_str() for _ in range(200)}) == 200


# —— 已展示 ——


@pytest.mark.asyncio
async def test_mark_seen_creates_entries(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        await reads.mark_seen(["a", "b", "c"], NOW)
        assert await reads.seen_ids() == {"a", "b", "c"}
        assert await reads.read_ids() == set()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_mark_seen_advances_last_seen_only(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        await reads.mark_seen(["a"], NOW)
        later = NOW + timedelta(hours=3)
        await reads.mark_seen(["a"], later)

        state = await reads.get("a")
        assert state is not None
        assert state.first_seen_at == NOW
        assert state.last_seen_at == later
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_mark_seen_does_not_clobber_first_seen_with_an_earlier_value(
    tmp_path: Path, migrations_dir: Path
) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        await reads.mark_seen(["a"], NOW)
        await reads.mark_seen(["a"], NOW - timedelta(days=1))
        state = await reads.get("a")
        assert state is not None
        assert state.first_seen_at == NOW - timedelta(days=1)
        assert state.last_seen_at == NOW
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_mark_seen_never_overwrites_read_at(tmp_path: Path, migrations_dir: Path) -> None:
    """⚠️ 决策 D2 的核心不变式：滚动一下就丢失未读是不可接受的。"""
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        await reads.set_read("a", True, NOW)
        await reads.mark_seen(["a"], NOW + timedelta(days=1))

        state = await reads.get("a")
        assert state is not None
        assert state.read_at == NOW, "mark_seen 绝不能覆盖 read_at"
        assert state.is_read is True
        assert state.last_seen_at == NOW + timedelta(days=1)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_mark_seen_deduplicates_input(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        await reads.mark_seen(["a", "a", "b", "a"], NOW)
        assert await reads.seen_ids() == {"a", "b"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_mark_seen_with_empty_input_is_a_noop(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        assert await reads.mark_seen([], NOW) == 0
        assert await reads.seen_ids() == set()
    finally:
        await db.close()


# —— 已读 ——


@pytest.mark.asyncio
async def test_set_read_marks_and_unmarks(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        state = await reads.set_read("a", True, NOW)
        assert state.is_read is True
        assert await reads.read_ids() == {"a"}

        state = await reads.set_read("a", False, NOW + timedelta(hours=1))
        assert state.is_read is False
        assert await reads.read_ids() == set()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unmarking_read_keeps_seen_timestamps(tmp_path: Path, migrations_dir: Path) -> None:
    """不变式 2：撤销已读只清 ``read_at``，不删行。"""
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        await reads.mark_seen(["a"], NOW)
        await reads.set_read("a", True, NOW + timedelta(minutes=5))
        await reads.set_read("a", False, NOW + timedelta(minutes=10))

        state = await reads.get("a")
        assert state is not None
        assert state.read_at is None
        assert state.first_seen_at == NOW, "seen_at 必须保留"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unmarking_a_never_seen_id_still_records_it(tmp_path: Path, migrations_dir: Path) -> None:
    """撤销一个从未 seen 的 ID 不能静默无效果。"""
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        state = await reads.set_read("ghost", False, NOW)
        assert state.canonical_id == "ghost"
        assert state.read_at is None
        assert await reads.get("ghost") is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_clear_read_flags_keeps_seen_rows(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        await reads.mark_seen(["a", "b"], NOW)
        await reads.set_read("a", True, NOW)
        await reads.set_read("b", True, NOW)

        removed = await reads.clear_read_flags()
        assert removed == 2
        seen, read = await reads.count()
        assert (seen, read) == (2, 0)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_many_omits_missing_ids(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        await reads.mark_seen(["a"], NOW)
        found = await reads.get_many(["a", "missing"])
        assert set(found) == {"a"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_get_many_with_empty_input(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        assert await ReadRepository(db).get_many([]) == {}
    finally:
        await db.close()


# —— 并发：无 lost update ——


@pytest.mark.asyncio
async def test_ten_concurrent_marks_lose_nothing(tmp_path: Path, migrations_dir: Path) -> None:
    """验收要求：10 个并发写无 lost update。"""
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        await asyncio.gather(*(reads.mark_seen([f"p{i:03d}"], NOW) for i in range(10)))
        assert await reads.seen_ids() == {f"p{i:03d}" for i in range(10)}

        seen, _ = await reads.count()
        assert seen == 10
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_marks_on_the_same_id_keep_extremes(
    tmp_path: Path, migrations_dir: Path
) -> None:
    """同一 ID 的并发写入不能互相覆盖，first/last 必须落在真实的极值上。"""
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        moments = [NOW + timedelta(hours=index) for index in range(10)]
        await asyncio.gather(*(reads.mark_seen(["same"], moment) for moment in moments))

        state = await reads.get("same")
        assert state is not None
        assert state.first_seen_at == min(moments)
        assert state.last_seen_at == max(moments)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_read_and_seen_writes_do_not_interfere(
    tmp_path: Path, migrations_dir: Path
) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        await asyncio.gather(
            reads.mark_seen(["a"], NOW),
            reads.set_read("a", True, NOW + timedelta(minutes=1)),
            reads.mark_seen(["a"], NOW + timedelta(minutes=2)),
        )
        state = await reads.get("a")
        assert state is not None
        assert state.read_at == NOW + timedelta(minutes=1)
    finally:
        await db.close()


# —— 导出 / 导入 ——


@pytest.mark.asyncio
async def test_export_import_round_trip(tmp_path: Path, migrations_dir: Path) -> None:
    """验收要求：导入导出往返一致。"""
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        await reads.mark_seen(["a", "b"], NOW)
        await reads.set_read("a", True, NOW + timedelta(hours=1))

        exported = await reads.export_json(app_version="0.1.0")
        document = json.loads(exported)
        assert document["schema_version"] == EXPORT_SCHEMA_VERSION
        assert document["app_version"] == "0.1.0"
        assert len(document["entries"]) == 2

        # 导进一个全新的库，内容必须一致
        other_db = await open_db(tmp_path / "other", migrations_dir)
        try:
            other = ReadRepository(other_db)
            assert await other.import_json(exported) == 2
            assert await other.export_state() | {"exported_at": ""} == (
                await reads.export_state() | {"exported_at": ""}
            )
        finally:
            await other_db.close()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_import_merges_by_min_max_and_max_read(tmp_path: Path, migrations_dir: Path) -> None:
    """``first_seen=min``、``last_seen=max``、``read_at=max(non-null)``。"""
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        await reads.mark_seen(["a"], NOW)
        await reads.set_read("a", True, NOW + timedelta(hours=1))

        incoming = [
            ReadState(
                canonical_id="a",
                first_seen_at=NOW - timedelta(days=2),
                last_seen_at=NOW + timedelta(days=2),
                read_at=NOW + timedelta(hours=5),
            )
        ]
        await reads.merge(incoming)

        state = await reads.get("a")
        assert state is not None
        assert state.first_seen_at == NOW - timedelta(days=2)
        assert state.last_seen_at == NOW + timedelta(days=2)
        assert state.read_at == NOW + timedelta(hours=5)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_import_keeps_existing_read_when_incoming_has_none(
    tmp_path: Path, migrations_dir: Path
) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        await reads.set_read("a", True, NOW)
        await reads.merge(
            [ReadState(canonical_id="a", first_seen_at=NOW, last_seen_at=NOW, read_at=None)]
        )
        state = await reads.get("a")
        assert state is not None
        assert state.read_at == NOW, "导入里 read_at 为空不能清掉已有的已读"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_import_keeps_newer_existing_read(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        await reads.set_read("a", True, NOW + timedelta(days=5))
        await reads.merge(
            [ReadState(canonical_id="a", first_seen_at=NOW, last_seen_at=NOW, read_at=NOW)]
        )
        state = await reads.get("a")
        assert state is not None
        assert state.read_at == NOW + timedelta(days=5)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_import_rejects_wrong_schema_version(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        with pytest.raises(ArxivFilterError) as excinfo:
            await reads.import_json(json.dumps({"schema_version": 99, "entries": []}))
        assert excinfo.value.details["reason"] == "unsupported_schema_version"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_import_rejects_invalid_json(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        with pytest.raises(ArxivFilterError) as excinfo:
            await reads.import_json("{ not json")
        assert excinfo.value.code is ErrorCode.INVALID_SEARCH_REQUEST
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_import_skips_malformed_entries(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        payload = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "entries": [
                {"canonical_id": "good", "first_seen_at": "2025-01-01T00:00:00Z",
                 "last_seen_at": "2025-01-02T00:00:00Z", "read_at": None},
                {"canonical_id": "bad-times", "first_seen_at": "nonsense",
                 "last_seen_at": "2025-01-02T00:00:00Z"},
                {"first_seen_at": "2025-01-01T00:00:00Z"},  # 缺 canonical_id
            ],
        }
        assert await reads.import_json(payload) == 1
        assert await reads.seen_ids() == {"good"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_import_normalises_reversed_timestamps(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        payload = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "entries": [
                {"canonical_id": "a", "first_seen_at": "2025-06-01T00:00:00Z",
                 "last_seen_at": "2025-01-01T00:00:00Z", "read_at": None},
            ],
        }
        await reads.import_json(payload)
        state = await reads.get("a")
        assert state is not None
        assert state.first_seen_at < state.last_seen_at, "逆序的时间戳必须被纠正"
    finally:
        await db.close()


# —— 删除 ——


@pytest.mark.asyncio
async def test_delete_removes_rows(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        reads = ReadRepository(db)
        await reads.mark_seen(["a", "b"], NOW)
        assert await reads.delete(["a"]) == 1
        assert await reads.seen_ids() == {"b"}
        assert await reads.delete([]) == 0
    finally:
        await db.close()


# —— 历史 ——


@pytest.mark.asyncio
async def test_history_record_and_list(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        history = HistoryRepository(db)
        await history.record(
            {"group_id": "cs", "keywords": "transformer"},
            "k" * 64,
            status="completed",
            result_count=42,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=30),
        )
        entries = await history.list()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.status == "completed"
        assert entry.result_count == 42
        assert entry.cache_hit is False
        assert entry.duration_ms == 30_000
        assert entry.request["keywords"] == "transformer"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_history_list_is_newest_first(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        history = HistoryRepository(db)
        ids = []
        for index in range(5):
            ids.append(
                await history.record(
                    {"n": index},
                    "k" * 64,
                    status="completed",
                    started_at=NOW + timedelta(minutes=index),
                    finished_at=NOW + timedelta(minutes=index, seconds=1),
                )
            )
        listed = [entry.id for entry in await history.list()]
        assert listed == list(reversed(ids))
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_history_cursor_pagination(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        history = HistoryRepository(db)
        for index in range(5):
            await history.record(
                {"n": index}, "k" * 64, status="completed",
                started_at=NOW + timedelta(minutes=index),
            )
        first = await history.list(limit=2)
        second = await history.list(limit=2, cursor=first[-1].id)
        assert len(first) == 2 and len(second) == 2
        assert {e.id for e in first} & {e.id for e in second} == set()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_history_rejects_unknown_status(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        history = HistoryRepository(db)
        entry_id = await history.start({}, "k" * 64, now=NOW)
        with pytest.raises(ValueError):
            await history.finish(entry_id, "exploded")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_history_start_then_finish(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        history = HistoryRepository(db)
        entry_id = await history.start({"q": 1}, "k" * 64, now=NOW)
        assert await history.finish(entry_id, "cancelled", result_count=7, now=NOW + timedelta(seconds=5))

        entry = await history.get(entry_id)
        assert entry is not None
        assert entry.status == "cancelled"
        assert entry.result_count == 7
        assert entry.finished_at == NOW + timedelta(seconds=5)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_history_cache_hit_flag(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        history = HistoryRepository(db)
        await history.record({}, "k" * 64, status="completed", cache_hit=True, started_at=NOW)
        assert (await history.list())[0].cache_hit is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_history_prune_by_age(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        history = HistoryRepository(db)
        await history.record(
            {"old": True}, "k" * 64, status="completed",
            started_at=NOW - timedelta(days=MAX_HISTORY_AGE_DAYS + 1),
        )
        await history.record({"new": True}, "k" * 64, status="completed", started_at=NOW)

        removed = await history.prune(now=NOW)
        assert removed == 1
        remaining = await history.list()
        assert len(remaining) == 1
        assert remaining[0].request == {"new": True}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_history_prune_by_count(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        history = HistoryRepository(db, max_entries=3)
        for index in range(6):
            await history.record(
                {"n": index}, "k" * 64, status="completed",
                started_at=NOW + timedelta(minutes=index),
            )
        removed = await history.prune(now=NOW + timedelta(hours=1))
        assert removed == 3
        assert await history.count() == 3
        # 保留的应当是最新的 3 条
        kept = {entry.request["n"] for entry in await history.list()}
        assert kept == {3, 4, 5}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_history_defaults_match_the_plan() -> None:
    assert MAX_HISTORY_ENTRIES == 500
    assert MAX_HISTORY_AGE_DAYS == 90


@pytest.mark.asyncio
async def test_history_tolerates_corrupt_request_json(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        history = HistoryRepository(db)
        entry_id = await history.record({}, "k" * 64, status="completed", started_at=NOW)
        await db.write(
            lambda c: c.execute(
                "UPDATE query_history SET raw_request_json = 'not json' WHERE id = ?", (entry_id,)
            )
        )
        entry = await history.get(entry_id)
        assert entry is not None
        assert entry.request == {}
        assert entry.as_dict()["request"] == {}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_history_clear_does_not_touch_cache_or_read_state(
    tmp_path: Path, migrations_dir: Path
) -> None:
    from arxiv_filter.storage.cache_repo import CacheRepository
    from arxiv_filter.models.paper import Author, Paper

    db = await open_db(tmp_path, migrations_dir)
    try:
        history = HistoryRepository(db)
        cache = CacheRepository(db)
        reads = ReadRepository(db)

        paper = Paper(
            canonical_id="2501.00001",
            versioned_id="2501.00001v1",
            title="T",
            authors=[Author(name="A")],
            published_at=NOW,
            updated_at=NOW,
            abs_url="https://arxiv.org/abs/2501.00001v1",
            retrieved_at=NOW,
        )
        await cache.put_complete("k" * 64, "{}", [paper], now=NOW)
        await reads.mark_seen([paper.canonical_id], NOW)
        await history.record({}, "k" * 64, status="completed", started_at=NOW)

        await history.clear()
        assert await history.count() == 0
        assert await cache.entry_count() == 1, "清历史不能清缓存"
        assert await reads.seen_ids() == {paper.canonical_id}, "清历史不能清已读状态"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_history_delete_single_entry(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        history = HistoryRepository(db)
        entry_id = await history.record({}, "k" * 64, status="completed", started_at=NOW)
        assert await history.delete(entry_id) is True
        assert await history.delete(entry_id) is False
        assert await history.count() == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_history_rejects_bad_constructor_args(tmp_path: Path, migrations_dir: Path) -> None:
    db = await open_db(tmp_path, migrations_dir)
    try:
        with pytest.raises(ValueError):
            HistoryRepository(db, max_entries=0)
        with pytest.raises(ValueError):
            HistoryRepository(db, max_age_days=0)
    finally:
        await db.close()
