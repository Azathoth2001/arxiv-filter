"""Health, browser heartbeat, and graceful shutdown endpoints."""

from __future__ import annotations

import asyncio
from secrets import compare_digest

from fastapi import APIRouter, Depends, Query

from arxiv_filter.api.context import ApiServices
from arxiv_filter.api.schemas import (
    HeartbeatRequest,
    HeartbeatResponse,
    ShutdownRequest,
    ShutdownResponse,
    SystemHealthResponse,
    ErrorEnvelope,
    SettingsResponse,
    SettingsUpdateRequest,
)
from arxiv_filter.api.security import token_dependency
from arxiv_filter.errors import ArxivFilterError, ErrorCode


def build_system_router(services: ApiServices) -> APIRouter:
    error_responses = {
        status: {"model": ErrorEnvelope}
        for status in (400, 401, 403, 404, 405, 409, 422, 500, 501, 502, 503, 504)
    }
    router = APIRouter(
        prefix="/api/v1/system",
        tags=["system"],
        responses=error_responses,
    )
    protected = [Depends(token_dependency(services))]

    @router.get("/health", response_model=SystemHealthResponse)
    async def health(
        challenge: str | None = Query(default=None, min_length=32, max_length=128),
    ) -> SystemHealthResponse:
        matches = bool(
            challenge
            and services.launcher_challenge
            and compare_digest(challenge, services.launcher_challenge)
        )
        return SystemHealthResponse(
            app_version=services.app_version,
            schema_version=services.schema_version,
            challenge_valid=matches,
            session_token=services.auth_token if matches else None,
        )

    @router.post(
        "/heartbeat",
        response_model=HeartbeatResponse,
        dependencies=protected,
    )
    async def heartbeat(body: HeartbeatRequest) -> HeartbeatResponse:
        services.update_browser_presence(body.client_id, active=body.active)
        return HeartbeatResponse()

    @router.get("/settings", response_model=SettingsResponse, dependencies=protected)
    async def get_settings() -> SettingsResponse:
        if services.settings_store is None:
            return SettingsResponse(proxy_mode="system", proxy_configured=False)
        return SettingsResponse.model_validate(services.settings_store.settings.public())

    @router.put("/settings", response_model=SettingsResponse, dependencies=protected)
    async def update_settings(body: SettingsUpdateRequest) -> SettingsResponse:
        if services.settings_store is None:
            raise ArxivFilterError(ErrorCode.SETTINGS_UPDATE_FAILED)
        active = await services.jobs.active_job()
        if active is not None:
            raise ArxivFilterError(ErrorCode.JOB_LIMIT_REACHED)
        try:
            updated = services.settings_store.update(
                proxy_mode=body.proxy_mode,
                proxy_url=body.proxy_url,
                open_marks_read=body.open_marks_read,
            )
            if services.provider is not None:
                await services.provider.reconfigure_proxy(  # type: ignore[attr-defined]
                    updated.effective_proxy,
                    trust_env=updated.trust_env,
                )
        except (OSError, ValueError) as exc:
            raise ArxivFilterError(ErrorCode.SETTINGS_UPDATE_FAILED) from exc
        return SettingsResponse.model_validate(updated.public())

    @router.post(
        "/shutdown",
        response_model=ShutdownResponse,
        status_code=202,
        dependencies=protected,
    )
    async def shutdown(body: ShutdownRequest) -> ShutdownResponse:
        active = await services.jobs.active_job()
        if active is not None:
            if not body.confirm_active:
                raise ArxivFilterError(
                    ErrorCode.SHUTDOWN_BLOCKED,
                    details={"active_job_id": str(active.job_id)},
                )
            await services.jobs.cancel(active.job_id)

        # Let the 202 response flush before uvicorn observes its exit signal.
        loop = asyncio.get_running_loop()
        loop.call_later(0.1, services.shutdown_signal.set)
        return ShutdownResponse()

    return router


__all__ = ["build_system_router"]
