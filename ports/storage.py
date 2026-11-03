"""The documents bucket, behind a Protocol (S3 in the processes, a dict in tests)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from core import storage
from core.config import settings
from core.storage import ObjectNotFound, TransientStorageError

__all__ = ["DocumentStore", "MemoryStore", "ObjectNotFound", "S3Store", "TransientStorageError"]


class DocumentStore(Protocol):
    async def get(self, key: str) -> bytes: ...

    async def put(self, key: str, content: bytes, content_type: str) -> None: ...


class S3Store:
    def __init__(self, bucket: str | None = None) -> None:
        self._bucket = bucket or settings.OUTPUT_BUCKET

    async def get(self, key: str) -> bytes:
        return await storage.download(key, bucket=self._bucket)

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        await storage.upload(key, content, content_type=content_type, bucket=self._bucket)


@dataclass
class MemoryStore:
    objects: dict[str, bytes] = field(default_factory=dict)
    fail_reads: bool = False

    async def get(self, key: str) -> bytes:
        if self.fail_reads:
            raise TransientStorageError("storage down")
        try:
            return self.objects[key]
        except KeyError:
            raise ObjectNotFound(key) from None

    async def put(self, key: str, content: bytes, content_type: str) -> None:
        self.objects[key] = content
