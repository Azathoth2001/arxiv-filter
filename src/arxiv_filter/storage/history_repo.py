"""查询历史（方案 §4.5 T2.3）。

保留策略：**500 条或 90 天，以先到者为准**（``docs/decisions.md``）。
历史与缓存是两回事——清缓存**不**清历史，`read_state` 更不会被动。

``id`` 用 UUIDv7（按时间单调递增），因此可以直接当有序游标用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from arxiv_filter.identifiers import uuid7_str
from arxiv_filter.models.base import format_utc, parse_utc
from arxiv_filter.storage.database import Database, format_ts

#: 历史保留的条数上限。
MAX_HISTORY_ENTRIES = 500

#: 历史保留的天数上限。
MAX_HISTORY_AGE_DAYS = 90

#: 允许的任务终态。
HISTORY_STATUSES = ("completed", "cancelled", "failed")


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """一条查询历史。"""

    id: str
    raw_request_json: str
    cache_key: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    result_count: int | None
    cache_hit: bool

    @property
    def request(self) -> dict[str, Any]:
        """解析后的请求体；损坏时返回空字典而不是抛错（列表渲染路径）。"""
        try:
            parsed = json.loads(self.raw_request_json)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @property
    def duration_ms(self) -> int | None:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "request": self.request,
            "cache_key": self.cache_key,
            "started_at": format_utc(self.started_at),
            "finished_at": format_utc(self.finished_at) if self.finished_at else None,
            "status": self.status,
            "result_count": self.result_count,
            "cache_hit": self.cache_hit,
            "duration_ms": self.duration_ms,
        }


class HistoryRepository:
    """``query_history`` 表读写。"""

    def __init__(
        self,
        database: Database,
        *,
        max_entries: int = MAX_HISTORY_ENTRIES,
        max_age_days: int = MAX_HISTORY_AGE_DAYS,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries 必须为正")
        if max_age_days < 1:
            raise ValueError("max_age_days 必须为正")
        self._db = database
        self.max_entries = max_entries
        self.max_age_days = max_age_days

    # —— 写入 ——

    async def start(
        self,
        request: Mapping[str, Any] | str,
        cache_key: str,
        *,
        now: datetime | None = None,
    ) -> str:
        """登记一次查询的开始，返回历史 ID（UUIDv7）。

        ``request`` 是**用户原始请求**（含完整关键词）——方案 §4.2 要求磁盘日志
        默认只记 query hash，但历史是用户自己的本地数据、且要能「重新执行」，
        因此这里保存完整请求；诊断导出时会另行脱敏。
        """
        entry_id = uuid7_str()
        raw = request if isinstance(request, str) else json.dumps(request, ensure_ascii=False)
        stamp = format_ts(now or _utcnow())

        await self._db.write(
            lambda c: c.execute(
                "INSERT INTO query_history"
                " (id, raw_request_json, cache_key, started_at, finished_at, status,"
                "  result_count, cache_hit)"
                " VALUES (?, ?, ?, ?, NULL, 'failed', NULL, 0)",
                (entry_id, raw, cache_key, stamp),
            )
        )
        return entry_id

    async def finish(
        self,
        entry_id: str,
        status: str,
        *,
        result_count: int | None = None,
        cache_hit: bool = False,
        now: datetime | None = None,
    ) -> bool:
        """收尾一次查询；返回是否命中已有记录。"""
        if status not in HISTORY_STATUSES:
            raise ValueError(f"未知的历史状态：{status!r}")
        stamp = format_ts(now or _utcnow())

        def write(connection) -> bool:  # type: ignore[no-untyped-def]
            cursor = connection.execute(
                "UPDATE query_history SET finished_at = ?, status = ?,"
                " result_count = ?, cache_hit = ? WHERE id = ?",
                (stamp, status, result_count, 1 if cache_hit else 0, entry_id),
            )
            return bool(getattr(cursor, "rowcount", 0))

        return await self._db.write(write)

    async def record(
        self,
        request: Mapping[str, Any] | str,
        cache_key: str,
        *,
        status: str,
        result_count: int | None = None,
        cache_hit: bool = False,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
    ) -> str:
        """一步写入一条已完成的历史（测试与缓存命中路径用）。"""
        start = started_at or _utcnow()
        entry_id = await self.start(request, cache_key, now=start)
        await self.finish(
            entry_id,
            status,
            result_count=result_count,
            cache_hit=cache_hit,
            now=finished_at or start,
        )
        return entry_id

    async def delete(self, entry_id: str) -> bool:
        def write(connection) -> bool:  # type: ignore[no-untyped-def]
            cursor = connection.execute("DELETE FROM query_history WHERE id = ?", (entry_id,))
            return bool(getattr(cursor, "rowcount", 0))

        return await self._db.write(write)

    async def clear(self) -> int:
        """清空历史。**不影响**缓存与 ``read_state``。"""

        def write(connection) -> int:  # type: ignore[no-untyped-def]
            cursor = connection.execute("DELETE FROM query_history")
            return int(getattr(cursor, "rowcount", 0) or 0)

        return await self._db.write(write)

    # —— 读取 ——

    async def list(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> list[HistoryEntry]:
        """按开始时间倒序列出历史。

        ``cursor`` 是上一页最后一条的 ``id``（UUIDv7 单调，可直接比较）。
        """
        if limit < 1:
            raise ValueError("limit 必须为正")
        if cursor:
            rows = await self._db.read(
                lambda c: c.execute(
                    "SELECT * FROM query_history WHERE id < ? ORDER BY id DESC LIMIT ?",
                    (cursor, limit),
                ).fetchall()
            )
        else:
            rows = await self._db.read(
                lambda c: c.execute(
                    "SELECT * FROM query_history ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            )
        return [_row_to_entry(row) for row in rows]

    async def get(self, entry_id: str) -> HistoryEntry | None:
        row = await self._db.read(
            lambda c: c.execute(
                "SELECT * FROM query_history WHERE id = ?", (entry_id,)
            ).fetchone()
        )
        return _row_to_entry(row) if row is not None else None

    async def count(self) -> int:
        value = await self._db.read(
            lambda c: c.execute("SELECT COUNT(*) FROM query_history").fetchone()[0]
        )
        return int(value)

    # —— 保留策略 ——

    async def prune(self, *, now: datetime | None = None) -> int:
        """按「500 条或 90 天，先到者为准」清理，返回删除条数。"""
        moment = now or _utcnow()
        cutoff = format_ts(moment - timedelta(days=self.max_age_days))

        def prune_sync(connection) -> int:  # type: ignore[no-untyped-def]
            removed = 0
            cursor = connection.execute("DELETE FROM query_history WHERE started_at < ?", (cutoff,))
            removed += int(getattr(cursor, "rowcount", 0) or 0)

            over = connection.execute(
                "SELECT COUNT(*) FROM query_history"
            ).fetchone()[0] - self.max_entries
            if over > 0:
                cursor = connection.execute(
                    "DELETE FROM query_history WHERE id IN ("
                    "  SELECT id FROM query_history ORDER BY id ASC LIMIT ?)",
                    (over,),
                )
                removed += int(getattr(cursor, "rowcount", 0) or 0)
            return removed

        return await self._db.write(prune_sync)


def _row_to_entry(row) -> HistoryEntry:  # type: ignore[no-untyped-def]
    return HistoryEntry(
        id=row["id"],
        raw_request_json=row["raw_request_json"],
        cache_key=row["cache_key"],
        started_at=parse_utc(row["started_at"]),
        finished_at=parse_utc(row["finished_at"]) if row["finished_at"] else None,
        status=row["status"],
        result_count=row["result_count"],
        cache_hit=bool(row["cache_hit"]),
    )


def _utcnow() -> datetime:
    from arxiv_filter.storage.database import utc_now

    return utc_now()


__all__ = [
    "HISTORY_STATUSES",
    "MAX_HISTORY_AGE_DAYS",
    "MAX_HISTORY_ENTRIES",
    "HistoryEntry",
    "HistoryRepository",
]
