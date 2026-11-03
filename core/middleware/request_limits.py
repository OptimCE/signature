"""
Request limits middleware — body size cap + request timeout.

Protects against oversized payloads and slow-loris / hung-request attacks.

Body size check:
    Reads Content-Length before the request reaches FastAPI's body parser.
    If the declared length exceeds the per-route cap (see ``_max_body_for``),
    the request is rejected immediately with 413 Payload Too Large, before any
    body is read into memory.

    This is a cheap up-front gate, not a complete one. A client using chunked
    transfer-encoding sends no Content-Length and so slips past this check; the
    middleware cannot bound such a body without first buffering it, which would
    allocate the very memory the cap exists to prevent. The memory bound is
    therefore enforced again where the body is actually loaded: the sign
    call bounds the decoded image (shared.const.MAX_DRAWN_PNG_BYTES).

Request timeout:
    Wraps the entire downstream handler in asyncio.wait_for().
    If the handler does not complete within TIMEOUT_SECONDS,
    the client receives a 504 Gateway Timeout.

Usage:
    app.add_middleware(RequestLimitsMiddleware)
"""

import asyncio
import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

# Larger cap for the routes that take a file (multipart).
# Defined in shared/const.py, not here: the service enforces the same number on
# the bytes it actually reads, and this module imports starlette, which the
# worker image does not install. Re-exported so callers and tests can keep
# reading it off the middleware that applies it.
from shared.const import UPLOAD_MAX_BODY_BYTES

logger = logging.getLogger(__name__)

__all__ = ["UPLOAD_MAX_BODY_BYTES", "RequestLimitsMiddleware"]

# 2 MB — generous for a JSON API, covers PDF upload metadata but blocks abuse
MAX_BODY_BYTES = 2 * 1024 * 1024

# Method + path-suffix pairs that get the upload cap: the sign call, which
# carries the signature image (base64). Its path is parameterised, so match on
# the suffix rather than the exact path.
_UPLOAD_ROUTE_SUFFIXES: tuple[tuple[str, str], ...] = (("POST", "/sign"),)

# 30 seconds — covers complex DB queries / PDF generation
TIMEOUT_SECONDS = 30


def _max_body_for(request: Request) -> int:
    """Return the body-size cap that applies to this request."""
    path = request.url.path.rstrip("/")
    method = request.method.upper()
    for upload_method, upload_suffix in _UPLOAD_ROUTE_SUFFIXES:
        if method == upload_method and path.endswith(upload_suffix):
            return UPLOAD_MAX_BODY_BYTES
    return MAX_BODY_BYTES


class RequestLimitsMiddleware(BaseHTTPMiddleware):
    """
    Enforces request body size limits and per-request timeout.
    """

    async def dispatch(self, request: Request, call_next):
        # --- Body size gate ---
        max_bytes = _max_body_for(request)
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    logger.warning(
                        "Request rejected: body too large",
                        extra={
                            "content_length": content_length,
                            "max_allowed": max_bytes,
                            "path": request.url.path,
                        },
                    )
                    return JSONResponse(
                        status_code=413,
                        content={
                            "data": "Payload too large",
                            "error_code": 0,
                        },
                    )
            except ValueError:
                pass

        # --- Request timeout ---
        try:
            response = await asyncio.wait_for(
                call_next(request),
                timeout=TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.error(
                "Request timed out",
                extra={
                    "timeout_seconds": TIMEOUT_SECONDS,
                    "path": request.url.path,
                    "method": request.method,
                },
            )
            return JSONResponse(
                status_code=504,
                content={
                    "data": "Request timeout",
                    "error_code": 0,
                },
            )

        return response
