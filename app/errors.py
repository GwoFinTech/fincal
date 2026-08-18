"""Unified error handling middleware (Issue #9).

Provides:
- Consistent error response format: {"error": {"code": "...", "details": "..."}}
- Maps business exceptions to proper HTTP status codes
- Language-neutral error codes
- Sanitized error messages (no credentials)
"""
from __future__ import annotations

import logging
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Application error with stable code and HTTP status."""
    def __init__(self, code: str, details: str = "", status_code: int = 400):
        self.code = code
        self.details = details
        self.status_code = status_code
        super().__init__(details)


class NotFoundError(AppError):
    def __init__(self, resource: str = "resource"):
        super().__init__(f"{resource}_not_found", f"{resource} not found", 404)


class ConflictError(AppError):
    def __init__(self, code: str = "conflict", details: str = ""):
        super().__init__(code, details, 409)


class UnauthorizedError(AppError):
    def __init__(self):
        super().__init__("unauthorized", "authentication required", 401)


class ForbiddenError(AppError):
    def __init__(self):
        super().__init__("forbidden", "admin required", 403)


class UpstreamError(AppError):
    def __init__(self, provider: str, details: str = ""):
        super().__init__("upstream_unavailable", f"{provider}: {details}", 502)


async def error_middleware(request: Request, call_next):
    """Catch AppError and return structured JSON error responses."""
    try:
        response = await call_next(request)
        return response
    except AppError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "details": exc.details}},
        )
    except Exception as exc:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "details": ""}},
        )
