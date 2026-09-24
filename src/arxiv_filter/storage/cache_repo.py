"""查询缓存：fresh / stale / complete 与 LRU 容量控制（方案 §4.5 T2.2）。

产品默认值（**不是** arXiv 限额，见 ``docs/decisions.md``）：

* fresh TTL **6 小时**；
* stale 保留 **30 天**；
* 缓存上限 **500 MiB**，按 ``last_accessed_at`` 做 LRU 淘汰；
* **绝不清理 ``read_state``**。

硬约束：

* ``partial`` / ``cancelled`` 的结果**绝不**写 ``complete=1``——
  否则用户下次会拿到一份看似完整、实际被截断的缓存；
* cache key 的语义版本由 :mod:`arxiv_filter.arxiv.query_builder` 提供，
  本模块只管存取，不重新计算 key。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.paper import Paper
from arxiv_filter.storage.database import Database, format_ts, parse_ts, utc_now

logger = logging.getLogger(__name__)

#: fresh 有效期（方案 §4.5）。
FRESH_TTL_S = 6 * 60 * 60

#: stale 保留期（方案 §4.5）。
STALE_TTL_S = 30 * 24 * 60 * 60

#: 缓存容量上限：500 MiB（方案 §4.5）。
DEFAULT_MAX_BYTES = 500 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CachedResult:
    """一份缓存内容。"""

    cache_key: str
    canonical_query_json: str
    created_at: datetime
    expires_at: datetime
    stale_expires_at: datetime
    complete: bool
    papers: tuple[Paper, ...]
    byte_size: int
    last_accessed_at: datetime

    def is_fresh(self, now: datetime) -> bool:
        return self.complete and now <= self.expires_at

    def is_stale(self, now: datetime) -> bool:
        """已过期但仍在保留期内。"""
        return self.complete and self.expires_at < now <= self.stale_expires_at

    @property
    def age_s(self) -> float:
        return (self.stale_expires_at - self.created_at).total_seconds()


@dataclass(frozen=True, slots=True)
class PruneReport:
    """一次清理的结果，直接用于 ``DELETE /cache`` 的响应。"""

    removed_entries: int = 0
    removed_bytes: int = 0
    remaining_bytes: int = 0
    removed_papers: int = 0

    @property
    def freed_mib(self) -> float:
        return self.removed_bytes / (1024 * 1024)


class CacheRepository:
    """``query_cache`` / ``query_results`` / ``papers`` 的读写入口。"""

    def __init__(
        self,
        database: Database,
        *,
        fresh_ttl_s: int = FRESH_TTL_S,
        stale_ttl_s: int = STALE_TTL_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        if fresh_ttl_s <= 0:
            raise ValueError("fresh_ttl_s 必须为正")
        if stale_ttl_s < fresh_ttl_s:
            raise ValueError("stale_ttl_s 不能小于 fresh_ttl_s")
        self._db = database
        self.fresh_ttl_s = fresh_ttl_s
        self.stale_ttl_s = stale_ttl_s
        self.max_bytes = max_bytes

    # —— 读取 ——

    async def get_fresh(self, cache_key: str, now: datetime | None = None) -> CachedResult | None:
        """取未过期的完整缓存；命中时更新 ``last_accessed_at``。"""
        moment = now or utc_now()
        return await self._load(cache_key, moment, want="fresh")

    async def get_stale(self, cache_key: str, now: datetime | None = None) -> CachedResult | None:
        """取已过期但仍在保留期内的完整缓存（需用户确认后才展示）。"""
        moment = now or utc_now()
        return await self._load(cache_key, moment, want="stale")

    async def get_any(self, cache_key: str, now: datetime | None = None) -> CachedResult | None:
        """不论新鲜与否都取回来，由调用方判断。"""
        moment = now or utc_now()
        return await self._load(cache_key, moment, want="any")

    async def _load(self, cache_key: str, now: datetime, *, want: str) -> CachedResult | None:
        row = await self._db.read(
            lambda c: c.execute(
                "SELECT cache_key, canonical_query_json, created_at, expires_at,"
                " complete, byte_size, last_accessed_at"
                " FROM query_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        )
        if row is None or not row["complete"]:
            # 未完成的行一律视为未命中——partial 的结果不能当成可用缓存
            return None

        created_at = parse_ts(row["created_at"])
        expires_at = parse_ts(row["expires_at"])
        stale_expires_at = created_at + timedelta(seconds=self.stale_ttl_s)

        if want == "fresh" and now > expires_at:
            return None
        if want == "stale" and not (expires_at < now <= stale_expires_at):
            return None
        if want == "any" and now > stale_expires_at:
            return None

        papers = await self._load_papers(cache_key)
        await self._touch(cache_key, now)

        return CachedResult(
            cache_key=row["cache_key"],
            canonical_query_json=row["canonical_query_json"],
            created_at=created_at,
            expires_at=expires_at,
            stale_expires_at=stale_expires_at,
            complete=True,
            papers=papers,
            byte_size=int(row["byte_size"]),
            last_accessed_at=now,
        )

    async def _load_papers(self, cache_key: str) -> tuple[Paper, ...]:
        rows = await self._db.read(
            lambda c: c.execute(
                "SELECT p.metadata_json FROM query_results q"
                " JOIN papers p ON p.canonical_id = q.canonical_id"
                " WHERE q.cache_key = ? ORDER BY q.ordinal",
                (cache_key,),
            ).fetchall()
        )
        papers: list[Paper] = []
        for row in rows:
            try:
                papers.append(Paper.model_validate_json(row["metadata_json"]))
            except Exception as exc:  # noqa: BLE001 - 单条坏数据不该毁掉整份缓存
                logger.warning("缓存里有无法解析的论文记录，已跳过：%s", type(exc).__name__)
        return tuple(papers)

    async def _touch(self, cache_key: str, now: datetime) -> None:
        stamp = format_ts(now)
        await self._db.write(
            lambda c: c.execute(
                "UPDATE query_cache SET last_accessed_at = ? WHERE cache_key = ?",
                (stamp, cache_key),
            )
        )

    # —— 写入 ——

    async def put_complete(
        self,
        cache_key: str,
        canonical_query_json: str,
        papers: Sequence[Paper],
        *,
        now: datetime | None = None,
        fresh_ttl_s: int | None = None,
        complete: bool = True,
    ) -> int:
        """写入一份**完整**缓存，返回字节数。

        ``complete=False`` 用于「结果不完整」的场景（保留以备诊断），
        这种行**永远不会**被 :meth:`get_fresh` / :meth:`get_stale` 命中。
        """
        moment = now or utc_now()
        ttl = self.fresh_ttl_s if fresh_ttl_s is None else fresh_ttl_s
        created = format_ts(moment)
        expires = format_ts(moment + timedelta(seconds=ttl))
        stamp = format_ts(moment)

        payloads = [(paper.canonical_id, paper.model_dump_json(), format_ts(paper.updated_at)) for paper in papers]
        byte_size = len(canonical_query_json.encode("utf-8")) + sum(
            len(json.encode("utf-8")) for _, json, _ in payloads
        )

        def write(connection) -> None:  # type: ignore[no-untyped-def]
            connection.execute(
                "INSERT INTO query_cache"
                " (cache_key, canonical_query_json, created_at, expires_at,"
                "  complete, byte_size, last_accessed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(cache_key) DO UPDATE SET"
                "  canonical_query_json=excluded.canonical_query_json,"
                "  created_at=excluded.created_at,"
                "  expires_at=excluded.expires_at,"
                "  complete=excluded.complete,"
                "  byte_size=excluded.byte_size,"
                "  last_accessed_at=excluded.last_accessed_at",
                (cache_key, canonical_query_json, created, expires, 1 if complete else 0, byte_size, stamp),
            )
            # papers 必须先于 query_results 落库（外键约束）
            connection.executemany(
                "INSERT INTO papers (canonical_id, metadata_json, updated_at) VALUES (?, ?, ?)"
                " ON CONFLICT(canonical_id) DO UPDATE SET"
                "  metadata_json=excluded.metadata_json, updated_at=excluded.updated_at",
                payloads,
            )
            connection.execute("DELETE FROM query_results WHERE cache_key = ?", (cache_key,))
            connection.executemany(
                "INSERT INTO query_results (cache_key, ordinal, canonical_id, source_rank, score)"
                " VALUES (?, ?, ?, ?, NULL)",
                [(cache_key, index, canonical_id, index) for index, (canonical_id, _, _) in enumerate(payloads)],
            )

        await self._db.write(write)
        return byte_size

    async def put_partial(
        self,
        cache_key: str,
        canonical_query_json: str,
        papers: Sequence[Paper],
        *,
        now: datetime | None = None,
    ) -> int:
        """写入一份**不完整**的结果。

        按方案 §4.5，取消或失败的结果「只留在当前 job 内存中，不写 complete=1」。
        这里仍然落盘但标 ``complete=0``，好处是诊断时能看到当时抓到了什么，
        而它**绝不会**被当成可用缓存命中。
        """
        return await self.put_complete(
            cache_key,
            canonical_query_json,
            papers,
            now=now,
            complete=False,
        )

    async def delete(self, cache_key: str) -> bool:
        result = await self._db.write(
            lambda c: c.execute("DELETE FROM query_cache WHERE cache_key = ?", (cache_key,))
        )
        return getattr(result, "rowcount", 0) > 0

    # —— 维护 ——

    async def total_bytes(self) -> int:
        value = await self._db.read(
            lambda c: c.execute("SELECT COALESCE(SUM(byte_size), 0) FROM query_cache").fetchone()[0]
        )
        return int(value)

    async def entry_count(self) -> int:
        value = await self._db.read(
            lambda c: c.execute("SELECT COUNT(*) FROM query_cache").fetchone()[0]
        )
        return int(value)

    async def purge_expired(self, now: datetime | None = None) -> PruneReport:
        """删除超出 stale 保留期的条目。"""
        moment = now or utc_now()
        cutoff = format_ts(moment - timedelta(seconds=self.stale_ttl_s))

        def purge(connection) -> tuple[int, int]:  # type: ignore[no-untyped-def]
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(byte_size), 0) FROM query_cache WHERE created_at < ?",
                (cutoff,),
            ).fetchone()
            connection.execute("DELETE FROM query_cache WHERE created_at < ?", (cutoff,))
            return int(row[0]), int(row[1])

        removed, removed_bytes = await self._db.write(purge)
        await self._delete_orphan_papers()
        return PruneReport(
            removed_entries=removed,
            removed_bytes=removed_bytes,
            remaining_bytes=await self.total_bytes(),
        )

    async def prune(self, max_bytes: int | None = None, now: datetime | None = None) -> PruneReport:
        """按 ``last_accessed_at`` LRU 把缓存压到 ``max_bytes`` 以内。

        **绝不清理 ``read_state``**（方案 §4.5）。
        """
        limit = self.max_bytes if max_bytes is None else max_bytes
        await self.purge_expired(now)

        def current(connection) -> tuple[int, int]:  # type: ignore[no-untyped-def]
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(byte_size), 0) FROM query_cache"
            ).fetchone()
            return int(row[0]), int(row[1])

        count, total = await self._db.read(current)
        if total <= limit or count == 0:
            return PruneReport(remaining_bytes=total)

        removed_entries = 0
        removed_bytes = 0
        while total > limit:
            victim = await self._db.read(
                lambda c: c.execute(
                    "SELECT cache_key, byte_size FROM query_cache"
                    " ORDER BY last_accessed_at ASC, cache_key ASC LIMIT 1"
                ).fetchone()
            )
            if victim is None:
                break
            await self._db.write(
                lambda c, key=victim["cache_key"]: c.execute(  # type: ignore[misc]
                    "DELETE FROM query_cache WHERE cache_key = ?", (key,)
                )
            )
            removed_entries += 1
            removed_bytes += int(victim["byte_size"])
            total -= int(victim["byte_size"])

        removed_papers = await self._delete_orphan_papers()
        logger.info(
            "缓存 LRU 淘汰 %d 条，释放 %.1f MiB", removed_entries, removed_bytes / (1024 * 1024)
        )
        return PruneReport(
            removed_entries=removed_entries,
            removed_bytes=removed_bytes,
            remaining_bytes=await self.total_bytes(),
            removed_papers=removed_papers,
        )

    async def clear(self) -> PruneReport:
        """清空查询缓存（``DELETE /cache`` 带 ``confirm=true``）。

        ``read_state`` 与 ``query_history`` **不受影响**。
        """

        def wipe(connection) -> tuple[int, int]:  # type: ignore[no-untyped-def]
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(byte_size), 0) FROM query_cache"
            ).fetchone()
            connection.execute("DELETE FROM query_cache")
            return int(row[0]), int(row[1])

        removed, removed_bytes = await self._db.write(wipe)
        removed_papers = await self._delete_orphan_papers()
        return PruneReport(
            removed_entries=removed,
            removed_bytes=removed_bytes,
            remaining_bytes=0,
            removed_papers=removed_papers,
        )

    async def _delete_orphan_papers(self) -> int:
        """删除既不被任何缓存引用、也不在 ``read_state`` 里的论文记录。

        保留 ``read_state`` 引用到的论文，否则「已读列表」会只剩一串 ID、没有标题。
        """

        def cleanup(connection) -> int:  # type: ignore[no-untyped-def]
            cursor = connection.execute(
                "DELETE FROM papers WHERE canonical_id NOT IN"
                " (SELECT canonical_id FROM query_results)"
                " AND canonical_id NOT IN (SELECT canonical_id FROM read_state)"
            )
            return int(getattr(cursor, "rowcount", 0) or 0)

        return await self._db.write(cleanup)

    async def iter_keys(self) -> list[str]:
        rows = await self._db.read(
            lambda c: c.execute("SELECT cache_key FROM query_cache ORDER BY created_at").fetchall()
        )
        return [row[0] for row in rows]


__all__ = [
    "DEFAULT_MAX_BYTES",
    "FRESH_TTL_S",
    "STALE_TTL_S",
    "CacheRepository",
    "CachedResult",
    "PruneReport",
]
