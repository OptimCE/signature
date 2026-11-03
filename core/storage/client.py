"""aiobotocore-backed S3 client used for document storage.

Stored document versions live in ``OUTPUT_BUCKET`` (shared with the platform's
rendered documents; this service writes under its own key prefix). Every
operation takes an optional ``bucket`` so callers can be explicit; it defaults
to ``STORAGE_BUCKET`` for general-purpose objects.

The shape is deliberately function-based — there's no client singleton to wire
into the FastAPI lifespan. aiobotocore clients are async-context managers;
opening one per operation is cheap because the underlying aiohttp connector
pool is shared across the asyncio loop.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from aiobotocore.session import get_session
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.exceptions import (
    ConnectionError as BotoConnectionError,
)

from core.config import settings

logger = logging.getLogger(__name__)


class ObjectNotFound(Exception):  # noqa: N818  # public exception name; raised in dispatcher, caught in worker
    """Raised by ``download`` when the requested key is absent from the bucket.

    Treat as a deterministic failure: the file is gone, no amount of retry
    will bring it back. Callers should mark the generation FAILED and ack.
    """


class TransientStorageError(Exception):
    """Raised by ``download`` for retryable errors (5xx, timeouts, transport).

    Callers should let JetStream redeliver the message rather than write a
    terminal status.
    """


# The set of botocore error codes we treat as "the object isn't there".
# NoSuchKey is the canonical S3 code; AWS sometimes returns 404 without one.
_NOT_FOUND_CODES = {"NoSuchKey", "NoSuchBucket", "404"}


@asynccontextmanager
async def _client() -> AsyncIterator:
    """Yield a fresh aiobotocore S3 client bound to the configured endpoint.

    ``aiobotocore`` requires its clients to be used as async context managers
    so the underlying aiohttp connector is closed deterministically.
    """
    session = get_session()
    async with session.create_client(
        "s3",
        endpoint_url=settings.STORAGE_ENDPOINT or None,
        region_name=settings.STORAGE_REGION,
        aws_access_key_id=settings.STORAGE_ACCESS_KEY,
        aws_secret_access_key=settings.STORAGE_SECRET_KEY,
    ) as client:
        yield client


def build_s3_uri(bucket: str, key: str) -> str:
    """Render the canonical ``s3://bucket/key`` form stored in ``file_ref``."""
    return f"s3://{bucket}/{key}"


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split ``s3://bucket/key`` back into ``(bucket, key)``.

    Raises ``ValueError`` for anything that isn't an s3 URI, so a corrupted
    ``file_ref`` surfaces as a clear error instead of a mystery 404.
    """
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3 uri: {uri!r}")
    remainder = uri[len("s3://") :]
    bucket, separator, key = remainder.partition("/")
    if not separator or not bucket or not key:
        raise ValueError(f"malformed s3 uri: {uri!r}")
    return bucket, key


async def upload(
    key: str,
    content: bytes,
    content_type: str | None = None,
    bucket: str | None = None,
) -> None:
    """Upload ``content`` as the object at ``key``.

    Raises whatever ``put_object`` raises — the service layer is responsible
    for translating those into HTTP errors and rolling back. No retries: the
    caller decides whether to surface the failure or swallow it.
    """
    extra: dict[str, str] = {}
    if content_type:
        extra["ContentType"] = content_type
    async with _client() as s3:
        await s3.put_object(
            Bucket=bucket or settings.STORAGE_BUCKET,
            Key=key,
            Body=content,
            **extra,
        )


async def download(key: str, bucket: str | None = None) -> bytes:
    """Fetch the object at ``key`` and return its bytes.

    Classifies errors into the two buckets the worker dispatcher cares about:
    ``ObjectNotFound`` for missing keys (deterministic) and
    ``TransientStorageError`` for everything that smells like a network /
    server hiccup (retryable). Unexpected errors propagate so the dispatcher's
    catch-all path can mark the row as ``unhandled_worker_error``.
    """
    try:
        async with _client() as s3:
            response = await s3.get_object(Bucket=bucket or settings.STORAGE_BUCKET, Key=key)
            async with response["Body"] as stream:
                return cast(bytes, await stream.read())
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if error_code in _NOT_FOUND_CODES or status == 404:
            raise ObjectNotFound(key) from exc
        if status is not None and 500 <= status < 600:
            raise TransientStorageError(f"storage {status} for {key}") from exc
        raise
    except (TimeoutError, EndpointConnectionError, BotoConnectionError, ReadTimeoutError) as exc:
        raise TransientStorageError(f"storage transport: {exc}") from exc


async def delete(key: str, bucket: str | None = None) -> None:
    """Delete the object at ``key``. Idempotent and best-effort.

    Never raises: a leaked file is a soft failure compared to a stuck row.
    Missing keys are logged at DEBUG; other errors at WARN.

    Note: stored document versions are evidentiary and are never deleted by the
    service — this exists for incidental objects only.
    """
    try:
        async with _client() as s3:
            await s3.delete_object(Bucket=bucket or settings.STORAGE_BUCKET, Key=key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in _NOT_FOUND_CODES:
            logger.debug("delete: object already gone key=%s", key)
            return
        logger.warning("delete failed for key=%s code=%s; leaking object", key, error_code)
    except Exception:
        logger.warning("delete failed for key=%s; leaking object", key, exc_info=True)
