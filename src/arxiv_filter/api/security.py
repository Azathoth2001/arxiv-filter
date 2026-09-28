"""Session token and same-origin checks for the localhost UI."""

from __future__ import annotations

from secrets import compare_digest

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader

from arxiv_filter.api.context import ApiServices
from arxiv_filter.errors import ArxivFilterError, ErrorCode


def token_dependency(services: ApiServices):
    """Build the shared authentication dependency for protected API routes."""
    token_header = APIKeyHeader(name="X-App-Token", auto_error=False)

    async def require_token(
        request: Request,
        x_app_token: str | None = Depends(token_header),
    ) -> None:
        origin = request.headers.get("origin")
        if (
            origin is not None
            and services.allowed_origin is not None
            and not compare_digest(origin, services.allowed_origin)
        ):
            raise ArxivFilterError(ErrorCode.ORIGIN_REJECTED)

        supplied = x_app_token or ""
        if not compare_digest(supplied, services.auth_token):
            raise ArxivFilterError(ErrorCode.INVALID_TOKEN)

    return require_token


__all__ = ["token_dependency"]
