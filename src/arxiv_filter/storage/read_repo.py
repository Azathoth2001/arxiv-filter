"""已读 / 已展示状态（方案 §4.5 T2.3、``docs/data-schema.md`` §1）。

这是「新增 vs 已读」分离（决策 D2）的落地点。不变式：

1. ``mark_seen`` **绝不覆盖**已存在的 ``read_at``，只推进 ``last_seen_at``；
2. 撤销已读只把 ``read_at`` 置 NULL，**不动** ``seen_at``；
3. ``first_seen_at`` 只取更早的值，``last_seen_at`` 只取更晚的值（导入合并同规则）;
4. 并发 upsert 不得丢更新。

导出/导入走 ``docs/data-schema.md`` §7 的 JSON 形状，按 ``canonical_id`` 合并：
``first_seen=min``、``last_seen=max``、``read_at=max(non-null)``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from arxiv_filter.models.base import format_utc, parse_utc
from arxiv_filter.storage.database import Database, format_ts

#: 导出 JSON 的 schema 版本（``docs/data-schema.md`` §7）。
EXPORT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ReadState:
    """一条阅读状态。"""

    canonical_id: str
    first_seen_at: datetime
    last_seen_at: datetime
    read_at: datetime | None = None

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "canonical_id": self.canonical_id,
            "first_seen_at": format_utc(self.first_seen_at),
            "last_seen_at": format_utc(self.last_seen_at),
            "read_at": format_utc(self.read_at) if self.read_at else None,
        }


def _min_ts(a: datetime, b: datetime) -> datetime:
    return a if a <= b else b


def _max_ts(a: datetime, b: datetime) -> datetime:
    return a if a >= b else b


class ReadRepository:
    """``read_state`` 表读写。"""

    def __init__(self, database: Database) -> None:
        self._db = database

    # —— 写入 ——

    async def mark_seen(self, canonical_ids: Sequence[str], at: datetime) -> int:
        """批量记录「曾展示」。**不会**覆盖已存在的 ``read_at``。

        返回受影响的条目数。卡片进入 viewport 时按 50 个一批调用（方案 §4.5）。
        """
        if not canonical_ids:
            return 0
        stamp = format_ts(at)
        unique = list(dict.fromkeys(canonical_ids))

        def write(connection) -> int:  # type: ignore[no-untyped-def]
            before = connection.execute("SELECT COUNT(*) FROM read_state").fetchone()[0]
            connection.executemany(
                "INSERT INTO read_state (canonical_id, first_seen_at, last_seen_at, read_at)"
                " VALUES (?, ?, ?, NULL)"
                " ON CONFLICT(canonical_id) DO UPDATE SET"
                "  last_seen_at = MAX(read_state.last_seen_at, excluded.last_seen_at),"
                "  first_seen_at = MIN(read_state.first_seen_at, excluded.first_seen_at)",
                [(cid, stamp, stamp) for cid in unique],
            )
            after = connection.execute("SELECT COUNT(*) FROM read_state").fetchone()[0]
            return len(unique) if after != before else len(unique)

        return await self._db.write(write)

    async def set_read(self, canonical_id: str, read: bool, at: datetime) -> ReadState:
        """标记 / 撤销已读。

        撤销只清 ``read_at``，**不删行**——``seen_at`` 必须保留（不变式 2）。
        """
        stamp = format_ts(at)

        def write(connection) -> None:  # type: ignore[no-untyped-def]
            if read:
                connection.execute(
                    "INSERT INTO read_state (canonical_id, first_seen_at, last_seen_at, read_at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(canonical_id) DO UPDATE SET read_at = excluded.read_at",
                    (canonical_id, stamp, stamp, stamp),
                )
            else:
                # 先确保行存在，否则撤销一个从未 seen 的 ID 会静默无效果
                connection.execute(
                    "INSERT INTO read_state (canonical_id, first_seen_at, last_seen_at, read_at)"
                    " VALUES (?, ?, ?, NULL)"
                    " ON CONFLICT(canonical_id) DO UPDATE SET read_at = NULL",
                    (canonical_id, stamp, stamp),
                )

        await self._db.write(write)
        state = await self.get(canonical_id)
        assert state is not None  # 刚写过，必然存在
        return state

    async def merge(self, states: Iterable[ReadState]) -> int:
        """按 ``canonical_id`` 合并（导入用）。

        ``first_seen=min``、``last_seen=max``、``read_at=max(non-null)``。
        """
        payload = [
            (
                state.canonical_id,
                format_ts(state.first_seen_at),
                format_ts(state.last_seen_at),
                format_ts(state.read_at) if state.read_at else None,
            )
            for state in states
        ]
        if not payload:
            return 0

        def write(connection) -> int:  # type: ignore[no-untyped-def]
            connection.executemany(
                "INSERT INTO read_state (canonical_id, first_seen_at, last_seen_at, read_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(canonical_id) DO UPDATE SET"
                "  first_seen_at = MIN(read_state.first_seen_at, excluded.first_seen_at),"
                "  last_seen_at  = MAX(read_state.last_seen_at, excluded.last_seen_at),"
                "  read_at = CASE"
                "    WHEN read_state.read_at IS NULL THEN excluded.read_at"
                "    WHEN excluded.read_at IS NULL THEN read_state.read_at"
                "    WHEN excluded.read_at > read_state.read_at THEN excluded.read_at"
                "    ELSE read_state.read_at END",
                payload,
            )
            return len(payload)

        return await self._db.write(write)

    async def clear_read_flags(self) -> int:
        """清空所有已读标记（「清空已读」二次确认后调用）。``seen_at`` 保留。"""

        def write(connection) -> int:  # type: ignore[no-untyped-def]
            cursor = connection.execute(
                "UPDATE read_state SET read_at = NULL WHERE read_at IS NOT NULL"
            )
            return int(getattr(cursor, "rowcount", 0) or 0)

        return await self._db.write(write)

    async def delete(self, canonical_ids: Sequence[str]) -> int:
        if not canonical_ids:
            return 0
        unique = list(dict.fromkeys(canonical_ids))

        def write(connection) -> int:  # type: ignore[no-untyped-def]
            cursor = connection.executemany(
                "DELETE FROM read_state WHERE canonical_id = ?", [(cid,) for cid in unique]
            )
            return int(getattr(cursor, "rowcount", 0) or 0)

        return await self._db.write(write)

    # —— 读取 ——

    async def get(self, canonical_id: str) -> ReadState | None:
        row = await self._db.read(
            lambda c: c.execute(
                "SELECT canonical_id, first_seen_at, last_seen_at, read_at"
                " FROM read_state WHERE canonical_id = ?",
                (canonical_id,),
            ).fetchone()
        )
        return _row_to_state(row) if row is not None else None

    async def get_many(self, canonical_ids: Sequence[str]) -> dict[str, ReadState]:
        """批量读取；缺失的 ID 不出现在结果里。"""
        if not canonical_ids:
            return {}
        unique = list(dict.fromkeys(canonical_ids))
        marks = ",".join("?" * len(unique))
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT canonical_id, first_seen_at, last_seen_at, read_at"
                f" FROM read_state WHERE canonical_id IN ({marks})",
                tuple(unique),
            ).fetchall()
        )
        return {row["canonical_id"]: _row_to_state(row) for row in rows}

    async def snapshot_state(self) -> dict[str, ReadState]:
        """读取完整已读/已展示快照，供一个搜索 job 冻结其展示语义。"""
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT canonical_id, first_seen_at, last_seen_at, read_at FROM read_state"
            ).fetchall()
        )
        return {row["canonical_id"]: _row_to_state(row) for row in rows}

    async def read_ids(self) -> set[str]:
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT canonical_id FROM read_state WHERE read_at IS NOT NULL"
            ).fetchall()
        )
        return {row[0] for row in rows}

    async def seen_ids(self) -> set[str]:
        rows = await self._db.read(
            lambda c: c.execute("SELECT canonical_id FROM read_state").fetchall()
        )
        return {row[0] for row in rows}

    async def count(self) -> tuple[int, int]:
        """``(seen 总数, 已读总数)``。"""
        row = await self._db.read(
            lambda c: c.execute(
                "SELECT COUNT(*), COUNT(read_at) FROM read_state"
            ).fetchone()
        )
        return int(row[0]), int(row[1])

    # —— 导出 / 导入 ——

    async def export_state(self, *, app_version: str = "") -> dict[str, Any]:
        """导出为 ``docs/data-schema.md`` §7 的 JSON 形状。"""
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT canonical_id, first_seen_at, last_seen_at, read_at"
                " FROM read_state ORDER BY canonical_id"
            ).fetchall()
        )
        return {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "exported_at": format_ts(_utcnow()),
            "app_version": app_version,
            "entries": [_row_to_state(row).as_dict() for row in rows],
        }

    async def export_json(self, *, app_version: str = "") -> str:
        return json.dumps(
            await self.export_state(app_version=app_version), ensure_ascii=False, indent=2
        )

    async def import_json(self, payload: str | bytes | Mapping[str, Any]) -> int:
        """从导出的 JSON 合并回来；返回处理的条目数。"""
        from arxiv_filter.errors import ArxivFilterError, ErrorCode

        if isinstance(payload, (str, bytes)):
            try:
                document = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ArxivFilterError(
                    ErrorCode.INVALID_SEARCH_REQUEST,
                    details={"reason": "read_state_json_invalid", "detail": str(exc)[:120]},
                    message_zh="导入文件不是合法的 JSON，请确认选的是本程序导出的 read_state 文件。",
                ) from exc
        else:
            document = dict(payload)

        version = document.get("schema_version")
        if version != EXPORT_SCHEMA_VERSION:
            raise ArxivFilterError(
                ErrorCode.INVALID_SEARCH_REQUEST,
                details={"reason": "unsupported_schema_version", "found": version},
                message_zh=(
                    f"导入文件的 schema_version 是 {version!r}，"
                    f"本程序只支持 {EXPORT_SCHEMA_VERSION}。"
                ),
            )

        states: list[ReadState] = []
        for entry in document.get("entries", []):
            parsed = _state_from_dict(entry)
            if parsed is not None:
                states.append(parsed)
        return await self.merge(states)


def _row_to_state(row) -> ReadState:  # type: ignore[no-untyped-def]
    return ReadState(
        canonical_id=row["canonical_id"],
        first_seen_at=parse_utc(row["first_seen_at"]),
        last_seen_at=parse_utc(row["last_seen_at"]),
        read_at=parse_utc(row["read_at"]) if row["read_at"] else None,
    )


def _state_from_dict(entry: Mapping[str, Any]) -> ReadState | None:
    """容错解析一条导入记录；缺字段或时间非法就跳过。"""
    canonical_id = entry.get("canonical_id")
    if not canonical_id or not isinstance(canonical_id, str):
        return None
    try:
        first = parse_utc(str(entry["first_seen_at"]))
        last = parse_utc(str(entry["last_seen_at"]))
        read_at = parse_utc(str(entry["read_at"])) if entry.get("read_at") else None
    except (KeyError, ValueError, TypeError):
        return None
    return ReadState(
        canonical_id=canonical_id,
        first_seen_at=_min_ts(first, last),
        last_seen_at=_max_ts(first, last),
        read_at=read_at,
    )


def _utcnow() -> datetime:
    from arxiv_filter.storage.database import utc_now

    return utc_now()


__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "ReadRepository",
    "ReadState",
]
