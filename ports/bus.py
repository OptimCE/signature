"""Publishing on NATS: the seal jobs, the domains' events, the evidence render.

A Protocol so the service layer runs in tests with ``RecordingBus`` and in
the processes with ``NatsBus``. Every publish happens AFTER the transaction
that decided it commits; a publish lost to a broker outage is recovered by the
worker's requeue pass (``service/sweep.py``), which republishes what a request
in PREPARING, SIGNING or COMPLETING still waits for.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from nats.js import JetStreamContext

from core.queue.helper import Event, send_event
from shared.const import (
    EVENT_JOB,
    SUBJECT_JOB_COMPLETE,
    SUBJECT_JOB_PREPARE,
    SUBJECT_JOB_SIGN,
    DomainEvent,
    domain_event_subject,
)


class Bus(Protocol):
    async def job(self, subject: str, data: dict[str, Any]) -> None: ...

    async def domain_event(
        self, source_service: str, event: DomainEvent, data: dict[str, Any]
    ) -> None: ...

    async def docgen(self, subject: str, body: dict[str, Any]) -> None: ...


def prepare_job(request_id: uuid.UUID) -> tuple[str, dict[str, Any]]:
    return SUBJECT_JOB_PREPARE, {"request_id": str(request_id)}


def sign_job(request_id: uuid.UUID, signer_id: uuid.UUID) -> tuple[str, dict[str, Any]]:
    return SUBJECT_JOB_SIGN, {"request_id": str(request_id), "signer_id": str(signer_id)}


def complete_job(request_id: uuid.UUID) -> tuple[str, dict[str, Any]]:
    return SUBJECT_JOB_COMPLETE, {"request_id": str(request_id)}


class NatsBus:
    def __init__(self, js: JetStreamContext) -> None:
        self._js = js

    async def job(self, subject: str, data: dict[str, Any]) -> None:
        await send_event(self._js, subject, Event(type=EVENT_JOB, data=data))

    async def domain_event(
        self, source_service: str, event: DomainEvent, data: dict[str, Any]
    ) -> None:
        await send_event(
            self._js,
            domain_event_subject(source_service, event),
            Event(type=f"signature.{event.value}", data=data),
        )

    async def docgen(self, subject: str, body: dict[str, Any]) -> None:
        # document-generation speaks a flat body, no Event envelope.
        await self._js.publish(subject, json.dumps(body).encode(), timeout=5.0)


@dataclass
class RecordingBus:
    """In-memory Bus for tests: what would have been published, in order."""

    jobs: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    events: list[tuple[str, DomainEvent, dict[str, Any]]] = field(default_factory=list)
    docgen_requests: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail: bool = False

    async def job(self, subject: str, data: dict[str, Any]) -> None:
        if self.fail:
            raise OSError("broker down")
        self.jobs.append((subject, data))

    async def domain_event(
        self, source_service: str, event: DomainEvent, data: dict[str, Any]
    ) -> None:
        if self.fail:
            raise OSError("broker down")
        self.events.append((source_service, event, data))

    async def docgen(self, subject: str, body: dict[str, Any]) -> None:
        if self.fail:
            raise OSError("broker down")
        self.docgen_requests.append((subject, body))
