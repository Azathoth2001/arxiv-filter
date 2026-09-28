"""FastAPI application factory 与统一 request/error envelope。"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from arxiv_filter import __version__
from arxiv_filter.api.context import ApiServices
from arxiv_filter.api.routes import build_api_router
from arxiv_filter.api.system_routes import build_system_router
from arxiv_filter.errors import ArxivFilterError, ErrorCode
from arxiv_filter.identifiers import request_id as make_request_id
from arxiv_filter.paths import static_dir

logger = logging.getLogger(__name__)


def create_api_app(services: ApiServices) -> FastAPI:
    """构建纯 API 应用；loopback、Origin、CSP 与生命周期留给 T4.3。"""
    app = FastAPI(
        title="arXiv Filter API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.services = services

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        value = make_request_id()
        request.state.request_id = value
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - keep IDs on unhandled 500 responses
            logger.exception("未处理 API 异常，request_id=%s", value, exc_info=exc)
            error = ArxivFilterError(ErrorCode.INTERNAL_ERROR)
            response = JSONResponse(
                status_code=500,
                content=error.to_envelope(value),
            )
        response.headers["X-Request-ID"] = value
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        return response

    @app.exception_handler(ArxivFilterError)
    async def arxiv_filter_error_handler(
        request: Request, exc: ArxivFilterError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=exc.to_envelope(_request_id(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = {
            "errors": [
                {
                    "loc": [str(part) for part in item.get("loc", ())],
                    "type": str(item.get("type", "value_error")),
                }
                for item in exc.errors()
            ]
        }
        error = ArxivFilterError(ErrorCode.INVALID_SEARCH_REQUEST, details=details)
        return JSONResponse(
            status_code=422,
            content=error.to_envelope(_request_id(request)),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if exc.status_code == 404:
            code = ErrorCode.API_NOT_FOUND
        elif exc.status_code == 405:
            code = ErrorCode.API_METHOD_NOT_ALLOWED
        else:
            code = ErrorCode.INTERNAL_ERROR
        error = ArxivFilterError(code)
        return JSONResponse(
            status_code=exc.status_code,
            content=error.to_envelope(_request_id(request)),
        )

    app.include_router(build_system_router(services))
    app.include_router(build_api_router(services))

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str | int]:
        return {
            "status": "ok",
            "app_version": services.app_version,
            "schema_version": services.schema_version,
        }

    assets = static_dir()
    index_path = assets / "index.html"
    if index_path.is_file():
        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(index_path, media_type="text/html; charset=utf-8")

        app.mount("/static", StaticFiles(directory=assets), name="static")
    return app


def _request_id(request: Request) -> str:
    value: Any = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) and value else make_request_id()


__all__ = ["create_api_app"]
