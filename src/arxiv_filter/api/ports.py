"""T4.2 routes 与尚未实现的日志/导出适配器之间的最小接口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, Sequence
from uuid import UUID

from arxiv_filter.api.schemas import ExportFormat, LogPage
from arxiv_filter.models.paper import PaperView
from arxiv_filter.models.query import SearchRequest


@dataclass(frozen=True, slots=True)
class GeneratedFile:
    content: bytes
    filename: str
    media_type: str


class Exporter(Protocol):
    async def export(
        self,
        papers: Sequence[PaperView],
        *,
        file_format: ExportFormat,
        query: str,
        include_state: bool,
        exported_at: datetime,
        request: SearchRequest | None = None,
    ) -> GeneratedFile: ...


class LogReader(Protocol):
    async def list_events(
        self,
        *,
        job_id: UUID | None,
        cursor: str | None,
        limit: int,
    ) -> LogPage: ...


class EmptyLogReader:
    """T4.2 的默认只读适配器；持久化脱敏日志由后续日志服务接入。"""

    async def list_events(
        self,
        *,
        job_id: UUID | None,
        cursor: str | None,
        limit: int,
    ) -> LogPage:
        return LogPage(items=[])


__all__ = ["EmptyLogReader", "Exporter", "GeneratedFile", "LogReader"]
