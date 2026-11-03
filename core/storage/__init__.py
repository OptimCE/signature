"""Object storage facade.

Wraps an S3-compatible client (MinIO in dev/staging/production) behind a tiny
function-shaped API so the API and the seal worker
don't need to know about aiobotocore directly.

Exposed surface:
- ``upload(key, content, content_type=None, bucket=None)`` — put_object
- ``download(key, bucket=None) -> bytes`` — get_object
- ``delete(key, bucket=None)`` — best-effort delete_object
- ``build_s3_uri(bucket, key)`` / ``parse_s3_uri(uri)`` — the ``file_ref`` form
- ``ObjectNotFound`` — raised by ``download`` when the key does not exist
- ``TransientStorageError`` — raised by ``download`` for retryable errors
"""

from core.storage.client import (
    ObjectNotFound,
    TransientStorageError,
    build_s3_uri,
    delete,
    download,
    parse_s3_uri,
    upload,
)

__all__ = [
    "ObjectNotFound",
    "TransientStorageError",
    "build_s3_uri",
    "delete",
    "download",
    "parse_s3_uri",
    "upload",
]
