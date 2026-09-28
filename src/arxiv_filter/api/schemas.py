"""HTTP DTO 与领域模型分离的 API 请求/响应契约（T4.2）。"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from arxiv_filter.models.base import DomainModel, UtcDatetime
from arxiv_filter.models.job import SearchJob, SearchProgress
from arxiv_filter.models.paper import PaperView
from arxiv_filter.models.query import ReadFilter

ExportFormat = Literal["csv", "markdown", "xlsx", "bibtex", "json"]


class ErrorPayload(DomainModel):
    code: str
    message_zh: str
    message_en: str
    retryable: bool
    request_id: str
    details: dict[str, Any]


class ErrorEnvelope(DomainModel):
    error: ErrorPayload


class ErrorSummary(DomainModel):
    code: str
    message_zh: str
    retryable: bool


class CategoryDto(DomainModel):
    id: str
    name_en: str
    name_zh: str
    active: bool
    alias_of: str | None = None


class ArchiveDto(DomainModel):
    id: str
    name_en: str
    name_zh: str
    categories: list[CategoryDto] = Field(default_factory=list)


class GroupDto(DomainModel):
    id: str
    name_en: str
    name_zh: str
    archives: list[ArchiveDto] = Field(default_factory=list)


class CategoriesResponse(DomainModel):
    taxonomy_version: str
    source: dict[str, Any]
    groups: list[GroupDto]


class JobAccepted(DomainModel):
    job_id: UUID
    status: SearchProgress
    cache_status: str
    poll_after_ms: int = 500


class JobStatusResponse(DomainModel):
    job: SearchJob
    error: ErrorSummary | None = None
    stale_cache: StaleCacheSummary | None = None


class StaleCacheSummary(DomainModel):
    created_at: UtcDatetime
    expires_at: UtcDatetime
    result_count: int = Field(ge=0)


class ContinueRequest(DomainModel):
    additional_results: int = Field(ge=1, le=2000)


class ContinuedJobAccepted(DomainModel):
    source_job_id: UUID
    job_id: UUID
    status: SearchProgress
    cache_status: str
    poll_after_ms: int = 500


class SeenRequest(DomainModel):
    job_id: UUID
    paper_ids: list[str] = Field(min_length=1, max_length=50)


class SeenResponse(DomainModel):
    updated: int = Field(ge=0)


class SetReadRequest(DomainModel):
    read: bool


class ReadStateResponse(DomainModel):
    canonical_id: str
    first_seen_at: UtcDatetime
    last_seen_at: UtcDatetime
    read_at: UtcDatetime | None = None


class HistoryItem(DomainModel):
    id: str
    request: dict[str, Any]
    cache_key: str
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    status: Literal["completed", "cancelled", "failed"]
    result_count: int | None = Field(default=None, ge=0)
    cache_hit: bool
    duration_ms: int | None = Field(default=None, ge=0)


class HistoryPage(DomainModel):
    items: list[HistoryItem]
    next_cursor: str | None = None


class LogEvent(DomainModel):
    timestamp: UtcDatetime
    level: str
    event: str
    job_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class LogPage(DomainModel):
    items: list[LogEvent]
    next_cursor: str | None = None


class ExportRequest(DomainModel):
    job_id: UUID
    format: ExportFormat
    paper_ids: list[str] | None = Field(default=None, max_length=9500)
    include_state: bool = False
    confirm_partial: bool = False


class CacheClearRequest(DomainModel):
    confirm: bool


class CacheClearResponse(DomainModel):
    removed_entries: int
    removed_bytes: int
    remaining_bytes: int
    removed_papers: int


class SystemHealthResponse(DomainModel):
    status: Literal["ok"] = "ok"
    app_version: str
    schema_version: int
    challenge_valid: bool = False
    session_token: str | None = None


class HeartbeatRequest(DomainModel):
    client_id: str = Field(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    active: bool = True


class HeartbeatResponse(DomainModel):
    accepted: bool = True


class ShutdownRequest(DomainModel):
    confirm_active: bool = False


class ShutdownResponse(DomainModel):
    status: Literal["shutting_down"] = "shutting_down"


class SettingsResponse(DomainModel):
    proxy_mode: Literal["system", "none", "custom"]
    proxy_configured: bool
    proxy_display: str = ""
    open_marks_read: bool = False


class SettingsUpdateRequest(DomainModel):
    proxy_mode: Literal["system", "none", "custom"]
    proxy_url: str | None = Field(default=None, max_length=2048)
    open_marks_read: bool = False


__all__ = [
    "ArchiveDto",
    "CacheClearRequest",
    "CacheClearResponse",
    "CategoriesResponse",
    "CategoryDto",
    "ContinueRequest",
    "ContinuedJobAccepted",
    "ErrorEnvelope",
    "ErrorPayload",
    "ErrorSummary",
    "ExportFormat",
    "ExportRequest",
    "GroupDto",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "HistoryItem",
    "HistoryPage",
    "JobAccepted",
    "JobStatusResponse",
    "LogEvent",
    "LogPage",
    "PaperView",
    "ReadStateResponse",
    "SeenRequest",
    "SeenResponse",
    "SettingsResponse",
    "SettingsUpdateRequest",
    "SetReadRequest",
    "ShutdownRequest",
    "ShutdownResponse",
    "StaleCacheSummary",
    "SystemHealthResponse",
]
