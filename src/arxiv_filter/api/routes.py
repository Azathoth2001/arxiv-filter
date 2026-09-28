"""Versioned HTTP endpoints for the local literature inbox (T4.2)."""

from __future__ import annotations

import logging
import re
from datetime import timezone
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from arxiv_filter.api.context import ApiServices
from arxiv_filter.api.ports import GeneratedFile
from arxiv_filter.api.security import token_dependency
from arxiv_filter.api.schemas import (
    ArchiveDto,
    CacheClearRequest,
    CacheClearResponse,
    CategoriesResponse,
    CategoryDto,
    ContinueRequest,
    ContinuedJobAccepted,
    ErrorEnvelope,
    ErrorSummary,
    ExportRequest,
    GroupDto,
    HistoryItem,
    HistoryPage,
    JobAccepted,
    JobStatusResponse,
    LogEvent,
    LogPage,
    ReadStateResponse,
    SeenRequest,
    SeenResponse,
    SetReadRequest,
    StaleCacheSummary,
)
from arxiv_filter.arxiv.ids import InvalidArxivId, canonicalize_arxiv_id
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.models.job import SearchPhase, SearchResultPage
from arxiv_filter.models.query import ReadFilter, SearchRequest
from arxiv_filter.redaction import redact

logger = logging.getLogger(__name__)
_SECRET_KEY = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)"
)
_ERROR_RESPONSES = {
    status: {"model": ErrorEnvelope}
    for status in (400, 401, 403, 404, 405, 409, 422, 500, 501, 502, 503, 504)
}


def build_api_router(services: ApiServices) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        dependencies=[Depends(token_dependency(services))],
        responses=_ERROR_RESPONSES,
    )

    @router.get("/categories", response_model=CategoriesResponse)
    async def categories() -> CategoriesResponse:
        groups: list[GroupDto] = []
        for group in services.taxonomy.groups:
            archives: list[ArchiveDto] = []
            for archive in services.taxonomy.archives_of_group(group.id):
                children = [
                    CategoryDto(
                        id=category.id,
                        name_en=category.name_en,
                        name_zh=category.name_zh,
                        active=category.active,
                        alias_of=category.alias_of,
                    )
                    for category in services.taxonomy.categories_of_archive(archive.id)
                ]
                archives.append(
                    ArchiveDto(
                        id=archive.id,
                        name_en=archive.name_en,
                        name_zh=archive.name_zh,
                        categories=children,
                    )
                )
            groups.append(
                GroupDto(
                    id=group.id,
                    name_en=group.name_en,
                    name_zh=group.name_zh,
                    archives=archives,
                )
            )
        return CategoriesResponse(
            taxonomy_version=services.taxonomy.taxonomy_version,
            source=dict(services.taxonomy.source),
            groups=groups,
        )

    @router.post(
        "/search-jobs",
        response_model=JobAccepted,
        status_code=202,
    )
    async def create_search_job(request: SearchRequest) -> JobAccepted:
        job = await services.jobs.create(request)
        return JobAccepted(
            job_id=job.job_id,
            status=job.status,
            cache_status=job.cache_status,
        )

    @router.get("/search-jobs/{job_id}", response_model=JobStatusResponse)
    async def get_search_job(job_id: UUID) -> JobStatusResponse:
        job = await services.jobs.snapshot(job_id)
        error = await services.jobs.get_error(job_id)
        execution = None
        try:
            execution = await services.jobs.get_execution(job_id)
        except ArxivFilterError:
            # A failed job still has a useful status/error snapshot; its results
            # endpoint separately returns the original failure.
            pass
        stale = None
        if execution is not None and execution.stale_confirmation_required:
            if execution.stale_cache_created_at and execution.stale_cache_expires_at:
                stale = StaleCacheSummary(
                    created_at=execution.stale_cache_created_at,
                    expires_at=execution.stale_cache_expires_at,
                    result_count=execution.stale_cache_result_count or 0,
                )
        summary = (
            ErrorSummary(
                code=str(error.code),
                message_zh=error.message_zh,
                retryable=error.retryable,
            )
            if error is not None
            else None
        )
        return JobStatusResponse(job=job, error=summary, stale_cache=stale)

    @router.post(
        "/search-jobs/{job_id}/continue",
        response_model=ContinuedJobAccepted,
        status_code=202,
    )
    async def continue_search_job(
        job_id: UUID,
        body: ContinueRequest,
    ) -> ContinuedJobAccepted:
        job = await services.jobs.continue_search(job_id, body.additional_results)
        return ContinuedJobAccepted(
            source_job_id=job_id,
            job_id=job.job_id,
            status=job.status,
            cache_status=job.cache_status,
        )

    @router.post("/search-jobs/{job_id}/cancel", response_model=JobAccepted, status_code=202)
    async def cancel_search_job(job_id: UUID) -> JobAccepted:
        job = await services.jobs.cancel(job_id)
        return JobAccepted(
            job_id=job.job_id,
            status=job.status,
            cache_status=job.cache_status,
        )

    @router.get("/search-jobs/{job_id}/results", response_model=SearchResultPage)
    async def get_search_results(
        job_id: UUID,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=100),
        read_filter: ReadFilter = Query(default="all"),
    ) -> SearchResultPage:
        job = await services.jobs.snapshot(job_id)
        execution = await services.jobs.get_execution(job_id)
        if job.status.phase is SearchPhase.FAILED:
            error = await services.jobs.get_error(job_id)
            if error is not None:
                raise error
        views = await services.jobs.get_paper_views(job_id)
        if read_filter == "new":
            views = tuple(paper for paper in views if paper.is_new)
        elif read_filter == "unread":
            current_state = await services.read_repository.snapshot_state()
            unread_views = []
            for paper in views:
                state = current_state.get(paper.canonical_id)
                if state is None or not state.is_read:
                    unread_views.append(
                        paper.model_copy(
                            update={
                                "read_at": state.read_at if state else None,
                                "is_read": bool(state and state.is_read),
                            }
                        )
                    )
            views = tuple(unread_views)
        return SearchResultPage(
            items=list(views[offset : offset + limit]),
            total=len(views),
            partial=job.partial,
            truncated=job.truncated,
            query=execution.query.expression if execution is not None else None,
        )

    @router.post("/seen", response_model=SeenResponse)
    async def mark_seen(body: SeenRequest) -> SeenResponse:
        views = await services.jobs.get_paper_views(body.job_id)
        allowed = {paper.canonical_id for paper in views}
        canonical_ids = [_canonical_id(value) for value in body.paper_ids]
        unknown = sorted(set(canonical_ids) - allowed)
        if unknown:
            raise ArxivFilterError(
                ErrorCode.PAPER_NOT_IN_JOB,
                details={"count": len(unknown)},
            )
        updated = await services.read_repository.mark_seen(
            list(dict.fromkeys(canonical_ids)),
            _utc_now(services),
        )
        return SeenResponse(updated=updated)

    @router.put(
        "/read-state/{paper_id:path}",
        response_model=ReadStateResponse,
    )
    async def set_read_state(paper_id: str, body: SetReadRequest) -> ReadStateResponse:
        canonical_id = _canonical_id(paper_id)
        state = await services.read_repository.set_read(
            canonical_id,
            body.read,
            _utc_now(services),
        )
        return ReadStateResponse.model_validate(state.as_dict())

    @router.get("/history", response_model=HistoryPage)
    async def list_history(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=36, max_length=36),
    ) -> HistoryPage:
        if cursor is not None:
            try:
                UUID(cursor)
            except ValueError as exc:
                raise ArxivFilterError(ErrorCode.INVALID_SEARCH_REQUEST) from exc
        entries = await services.history_repository.list(
            limit=limit + 1,
            cursor=cursor,
        )
        has_more = len(entries) > limit
        page = entries[:limit]
        return HistoryPage(
            items=[HistoryItem.model_validate(item.as_dict()) for item in page],
            next_cursor=page[-1].id if has_more and page else None,
        )

    @router.get("/logs", response_model=LogPage)
    async def list_logs(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, min_length=1, max_length=256),
        job_id: UUID | None = Query(default=None),
    ) -> LogPage:
        page = await services.log_reader.list_events(
            job_id=job_id,
            cursor=cursor,
            limit=limit,
        )
        return LogPage(
            items=[_redact_log(item) for item in page.items],
            next_cursor=page.next_cursor,
        )

    @router.post(
        "/exports",
        status_code=200,
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "导出文件流",
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            }
        },
    )
    async def export_results(body: ExportRequest) -> StreamingResponse:
        job = await services.jobs.snapshot(body.job_id)
        if job.status.phase not in {SearchPhase.COMPLETED, SearchPhase.CANCELLED}:
            raise ArxivFilterError(
                ErrorCode.JOB_INVALID_TRANSITION,
                details={"phase": job.status.phase.value, "reason": "not_exportable"},
            )
        if job.status.phase is SearchPhase.CANCELLED and not body.confirm_partial:
            raise ArxivFilterError(ErrorCode.EXPORT_PARTIAL_CONFIRMATION_REQUIRED)
        execution = await services.jobs.get_execution(body.job_id)
        if execution is None:
            raise ArxivFilterError(ErrorCode.EXPORT_INVALID_SUBSET)
        request = await services.jobs.get_request(body.job_id)
        views = await services.jobs.get_paper_views(body.job_id)
        if body.paper_ids is not None:
            selected_ids = [_canonical_id(value) for value in body.paper_ids]
            unique_ids = set(selected_ids)
            by_id = {paper.canonical_id: paper for paper in views}
            if not unique_ids.issubset(by_id):
                raise ArxivFilterError(ErrorCode.EXPORT_INVALID_SUBSET)
            views = tuple(paper for paper in views if paper.canonical_id in unique_ids)
        if services.exporter is None:
            raise ArxivFilterError(ErrorCode.EXPORT_NOT_AVAILABLE)
        try:
            artifact: GeneratedFile = await services.exporter.export(
                views,
                file_format=body.format,
                query=execution.query.expression,
                include_state=body.include_state,
                exported_at=_utc_now(services),
                request=request,
            )
        except ArxivFilterError:
            raise
        except Exception as exc:  # noqa: BLE001 - exporter details stay out of response
            logger.exception("导出器失败，job_id=%s", body.job_id, exc_info=exc)
            raise ArxivFilterError(ErrorCode.EXPORT_FAILED) from exc
        if (
            not artifact.filename
            or any(char in artifact.filename for char in ("/", "\\", "\r", "\n", '"'))
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in artifact.filename)
        ):
            raise ArxivFilterError(ErrorCode.EXPORT_FAILED)
        filename = quote(artifact.filename, safe="")
        return StreamingResponse(
            iter([artifact.content]),
            media_type=artifact.media_type,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )

    @router.delete("/cache", response_model=CacheClearResponse)
    async def clear_cache(body: CacheClearRequest) -> CacheClearResponse:
        if not body.confirm:
            raise ArxivFilterError(ErrorCode.CACHE_CLEAR_CONFIRMATION_REQUIRED)
        active = await services.jobs.active_job()
        if active is not None:
            raise ArxivFilterError(
                ErrorCode.JOB_LIMIT_REACHED,
                details={"active_job_id": str(active.job_id)},
            )
        report = await services.cache_repository.clear()
        return CacheClearResponse(
            removed_entries=report.removed_entries,
            removed_bytes=report.removed_bytes,
            remaining_bytes=report.remaining_bytes,
            removed_papers=report.removed_papers,
        )

    return router


def _canonical_id(value: str) -> str:
    try:
        canonical_id, _version = canonicalize_arxiv_id(value)
    except (InvalidArxivId, AttributeError) as exc:
        raise ArxivFilterError(ErrorCode.INVALID_ARXIV_ID) from exc
    return canonical_id


def _utc_now(services: ApiServices):
    value = services.clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ArxivFilterError(ErrorCode.INTERNAL_ERROR)
    return value.astimezone(timezone.utc)


def _redact_value(value):  # type: ignore[no-untyped-def]
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            name = str(key)
            safe[name] = "***" if _SECRET_KEY.search(name) else _redact_value(item)
        return safe
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def _redact_log(event: LogEvent) -> LogEvent:
    payload = event.model_dump(mode="python")
    payload["event"] = redact(payload["event"])
    payload["level"] = redact(payload["level"])
    payload["details"] = _redact_value(payload["details"])
    return LogEvent.model_validate(payload)


__all__ = ["build_api_router"]
