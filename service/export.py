"""The signature part of a data export (monorepo contracts/export/README.md, §7 « Annexes »).

User scope (the requester's own data):
- the requests where they, or a member linked to them, hold a slot: the inbox's own read
  (``views.slot_rows``) without its module gate, since an export is not a use of the module;
- in each, only their slots, with the fields they filled;
- the evidence log of those slots and of the request itself, with the network and session
  details of anyone else's action withheld;
- the three files the app lets them download once the request is completed: the signed PDF,
  the evidence report and evidence.json.

Community scope (an ADMIN, re-checked in crm_db by the kit):
- every request of the community (``views.community_requests``), every slot and field, the
  revisions;
- the full evidence log: the very rows the chain hashes, so the archive can be verified;
- the three files of every completed request.

Files are references to the documents bucket: the worker copies them, nothing is read here.
A file whose object is gone stays listed with ``file_missing: true`` and the ``files_missing``
counter, instead of failing every export of the community.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.export import (
    ExportHandler,
    ExportStorageSettings,
    PartContext,
    Requester,
    S3PartStore,
    SqlCrmLookup,
    resolve_requester,
)
from domain.evidence import verify_chain
from ports.crm_read import CrmRead, UserInfo
from ports.storage import DocumentStore
from service import views
from service.log import event_row
from service.signer_actions import on_behalf_of
from shared.const import FieldType, RequestStatus, RevisionKind, SignerStatus
from shared.models.local_models import (
    SignatureEventModel,
    SignatureFieldModel,
    SignatureRequestModel,
    SignatureRevisionModel,
    SignatureSignerModel,
)

logger = logging.getLogger(__name__)

SERVICE = "signature"
PAGE = 200

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

# The three files of a completed request: (attribute of the key, of its sha256, file, type).
ARTIFACTS: tuple[tuple[str, str, str, str, str], ...] = (
    ("final", "final_key", "final_sha256", "signed.pdf", "application/pdf"),
    ("evidence_pdf", "evidence_pdf_key", "evidence_pdf_sha256", "evidence.pdf", "application/pdf"),
    (
        "evidence_json",
        "evidence_json_key",
        "evidence_json_sha256",
        "evidence.json",
        "application/json",
    ),
)
# What an event says about the person who acted, withheld from a user export when it was
# someone else (another account linked to the member, the community's admin...).
IDENTITY_FIELDS = (
    "actor_sub",
    "actor_user_id",
    "ip",
    "user_agent",
    "keycloak_sid",
    "auth_time",
    "acr",
)


def _iso(value: datetime.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")


# The views write `+00:00`; every date of the archive ends with `Z` (the contract's form).
SUMMARY_DATES = ("created_at", "expires_at", "closed_at")
SIGNER_DATES = ("invited_at", "signed_at", "declined_at")


def _zulu(view: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    for key in keys:
        value = view.get(key)
        if isinstance(value, str) and value.endswith("+00:00"):
            view[key] = value[: -len("+00:00")] + "Z"
    return view


def _evidence_time(value: datetime.datetime | None) -> str | None:
    """The chain's own form (``domain.evidence``): the hashes recompute from the archive."""
    if value is None:
        return None
    return value.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def event_item(event: SignatureEventModel, *, identity: bool) -> dict[str, Any]:
    """One evidence-log row, as the chain hashes it (``service.log.event_row``)."""
    row = event_row(event)
    item: dict[str, Any] = {
        "request_id": str(row["request_id"]),
        "seq": row["seq"],
        "signer_id": str(row["signer_id"]) if row["signer_id"] else None,
        "type": row["type"],
        "at": _evidence_time(row["at"]),
        "actor_sub": row["actor_sub"],
        "actor_user_id": row["actor_user_id"],
        "ip": row["ip"],
        "user_agent": row["user_agent"],
        "keycloak_sid": row["keycloak_sid"],
        "auth_time": _evidence_time(row["auth_time"]),
        "acr": row["acr"],
        "document_sha256": row["document_sha256"],
        "consent_version": row["consent_version"],
        "payload": row["payload"] or {},
        "prev_hash": row["prev_hash"],
        "hash": row["hash"],
        "identity_withheld": not identity,
    }
    if not identity:
        for name in IDENTITY_FIELDS:
            item[name] = None
    return item


def field_item(field: SignatureFieldModel) -> dict[str, Any]:
    return {
        "name": field.name,
        "type": FieldType(field.type).name,
        "label": field.label,
        "required": field.required,
        "value": field.value,
        "filled_at": _iso(field.filled_at),
    }


def signer_item(
    signer: SignatureSignerModel,
    fields: Iterable[SignatureFieldModel],
    user: UserInfo | None,
) -> dict[str, Any]:
    return {
        **_zulu(views.signer_view(signer), SIGNER_DATES),
        "email": signer.email,
        "typed_name": signer.typed_name,
        "viewed_revision": signer.viewed_revision,
        "signed_revision": signer.signed_revision,
        "on_behalf_of": on_behalf_of(signer, user) if user is not None else None,
        "fields": [field_item(f) for f in sorted(fields, key=lambda f: f.name)],
    }


def revision_item(revision: SignatureRevisionModel) -> dict[str, Any]:
    return {
        "number": revision.number,
        "kind": RevisionKind(revision.kind).name,
        "sha256": revision.sha256,
        "signer_id": str(revision.signer_id) if revision.signer_id else None,
        "created_at": _iso(revision.created_at),
    }


class _Part:
    """One export part: the sessions, the store and the counters of what was listed."""

    def __init__(
        self,
        ctx: PartContext,
        *,
        local: AsyncSession,
        crm: AsyncSession,
        store: DocumentStore,
        bucket: str,
    ) -> None:
        self.ctx = ctx
        self.local = local
        self.crm = crm
        self.store = store
        self.bucket = bucket
        self.documents = 0
        self.evidence_files = 0
        self.missing = 0
        self.names: dict[int, str | None] = {}

    async def community_name(self, id_community: int) -> str | None:
        if id_community not in self.names:
            self.names[id_community] = await CrmRead(self.crm).community_name(id_community)
        return self.names[id_community]

    # ---- the reads -----------------------------------------------------------------------

    async def _many(self, query: Any) -> list[Any]:
        return list((await self.local.execute(query)).scalars())

    async def signers_of(
        self, request_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[SignatureSignerModel]]:
        out: dict[uuid.UUID, list[SignatureSignerModel]] = {i: [] for i in request_ids}
        if request_ids:
            for signer in await self._many(
                select(SignatureSignerModel)
                .where(SignatureSignerModel.request_id.in_(request_ids))
                .order_by(SignatureSignerModel.position)
            ):
                out[signer.request_id].append(signer)
        return out

    async def fields_of(
        self, signer_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[SignatureFieldModel]]:
        out: dict[uuid.UUID, list[SignatureFieldModel]] = {i: [] for i in signer_ids}
        if signer_ids:
            for field in await self._many(
                select(SignatureFieldModel).where(SignatureFieldModel.signer_id.in_(signer_ids))
            ):
                out[field.signer_id].append(field)
        return out

    async def revisions_of(
        self, request_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[SignatureRevisionModel]]:
        out: dict[uuid.UUID, list[SignatureRevisionModel]] = {i: [] for i in request_ids}
        if request_ids:
            for revision in await self._many(
                select(SignatureRevisionModel)
                .where(SignatureRevisionModel.request_id.in_(request_ids))
                .order_by(SignatureRevisionModel.number)
            ):
                out[revision.request_id].append(revision)
        return out

    async def events_of(self, request_id: uuid.UUID) -> list[SignatureEventModel]:
        return await self._many(
            select(SignatureEventModel)
            .where(SignatureEventModel.request_id == request_id)
            .order_by(SignatureEventModel.seq)
        )

    # ---- the files -----------------------------------------------------------------------

    async def files(self, request: SignatureRequestModel) -> dict[str, Any]:
        """The completed request's three files, listed by reference when their object exists."""
        out: dict[str, Any] = {}
        for name, key_attr, sha_attr, file_name, content_type in ARTIFACTS:
            key = getattr(request, key_attr)
            if request.status != RequestStatus.COMPLETED or not key:
                out[name] = None
                continue
            self.ctx.checkpoint()
            size = await self.store.size(key)
            if size is None:
                self.missing += 1
                out[name] = {
                    "path": None,
                    "sha256": getattr(request, sha_attr),
                    "file_missing": True,
                }
                continue
            relative = f"files/{request.id}/{file_name}"
            self.ctx.add_reference(
                bucket=self.bucket, key=key, path=relative, size=size, content_type=content_type
            )
            if name == "final":
                self.documents += 1
            else:
                self.evidence_files += 1
            out[name] = {
                "path": f"{SERVICE}/{relative}",
                "sha256": getattr(request, sha_attr),
                "file_missing": False,
            }
        return out

    async def request_item(
        self,
        request: SignatureRequestModel,
        *,
        signers: list[SignatureSignerModel],
        shown: list[SignatureSignerModel],
        fields: dict[uuid.UUID, list[SignatureFieldModel]],
        revisions: list[SignatureRevisionModel],
        chain: dict[str, Any] | None,
        user: UserInfo | None,
    ) -> dict[str, Any]:
        return {
            **_zulu(
                views.request_summary(request, await self.community_name(request.id_community)),
                SUMMARY_DATES,
            ),
            "consent_version": request.consent_version,
            "original_sha256": request.original_sha256,
            "last_reminded_at": _iso(request.last_reminded_at),
            "progress": {
                "signed": sum(1 for s in signers if s.status == SignerStatus.SIGNED),
                "total": len(signers),
            },
            "signers": [signer_item(s, fields.get(s.id, []), user) for s in shown],
            "revisions": [revision_item(r) for r in revisions],
            "chain": chain,
            "files": await self.files(request),
        }

    def finish(self, requests: int, events: int) -> None:
        self.ctx.count("requests", requests)
        self.ctx.count("events", events)
        self.ctx.count("documents", self.documents)
        self.ctx.count("evidence_files", self.evidence_files)
        if self.missing:
            self.ctx.count("files_missing", self.missing)

    # ---- user scope ------------------------------------------------------------------------

    async def user_export(self, requester: Requester) -> None:
        user = await CrmRead(self.crm).user_by_sub(requester.auth_user_id)
        rows = await views.slot_rows(user, local=self.local, crm=self.crm) if user else []
        mine: dict[uuid.UUID, list[SignatureSignerModel]] = {}
        requests: dict[uuid.UUID, SignatureRequestModel] = {}
        for signer, request in rows:
            requests.setdefault(request.id, request)
            mine.setdefault(request.id, []).append(signer)
        ids = list(requests)
        slot_ids = [s.id for slots in mine.values() for s in slots]
        signers = await self.signers_of(ids)
        fields = await self.fields_of(slot_ids)
        revisions = await self.revisions_of(ids)

        async def items() -> AsyncIterator[dict[str, Any]]:
            for request_id in ids:
                self.ctx.checkpoint()
                yield await self.request_item(
                    requests[request_id],
                    signers=signers[request_id],
                    shown=mine[request_id],
                    fields=fields,
                    revisions=revisions[request_id],
                    # Only part of the log is theirs: a partial chain cannot be verified.
                    chain=None,
                    user=user,
                )

        written = await self.ctx.write_json("requests", items())

        events = 0
        if ids and user is not None:
            # Their slots' rows and the request's own (no slot), in each request's order.
            query = (
                select(SignatureEventModel)
                .where(SignatureEventModel.request_id.in_(ids))
                .where(
                    or_(
                        SignatureEventModel.signer_id.in_(slot_ids),
                        SignatureEventModel.signer_id.is_(None),
                    )
                )
                .order_by(SignatureEventModel.request_id, SignatureEventModel.seq)
            )
            rank = {request_id: i for i, request_id in enumerate(ids)}
            found = sorted(await self._many(query), key=lambda e: (rank[e.request_id], e.seq))
            log = [event_item(e, identity=e.actor_sub in (None, user.sub)) for e in found]
            events = await self.ctx.write_json("events", log)
        else:
            await self.ctx.write_json("events", [])
        self.finish(written, events)

    # ---- community scope -------------------------------------------------------------------

    async def community_ids(self, id_community: int) -> list[uuid.UUID]:
        """Every request of the community, through the follow-up list's own read."""
        seen: dict[uuid.UUID, None] = {}
        offset = 0
        while True:
            self.ctx.checkpoint()
            page = await views.community_requests(
                id_community,
                local=self.local,
                crm=self.crm,
                status=None,
                source=None,
                limit=PAGE,
                offset=offset,
            )
            for item in page["items"]:
                seen.setdefault(uuid.UUID(item["id"]), None)
            offset += PAGE
            if not page["items"] or offset >= int(page["total"]):
                return list(seen)

    async def community_export(self, id_community: int) -> None:
        listed = await self.community_ids(id_community)
        chains: dict[uuid.UUID, dict[str, Any]] = {}
        originals: dict[uuid.UUID, str] = {}
        for start in range(0, len(listed), PAGE):
            batch = listed[start : start + PAGE]
            for request in await self._many(
                select(SignatureRequestModel).where(SignatureRequestModel.id.in_(batch))
            ):
                originals[request.id] = request.original_sha256

        async def log() -> AsyncIterator[dict[str, Any]]:
            for request_id in listed:
                self.ctx.checkpoint()
                events = await self.events_of(request_id)
                check = verify_chain(originals[request_id], [event_row(e) for e in events])
                chains[request_id] = {
                    "ok": check.ok,
                    "events": check.events,
                    "head": check.head,
                    "broken_at": check.broken_at,
                    "reason": check.reason,
                }
                for event in events:
                    yield event_item(event, identity=True)

        events = await self.ctx.write_json("events", log())

        async def items() -> AsyncIterator[dict[str, Any]]:
            for start in range(0, len(listed), PAGE):
                ids = listed[start : start + PAGE]
                models = {
                    r.id: r
                    for r in await self._many(
                        select(SignatureRequestModel).where(SignatureRequestModel.id.in_(ids))
                    )
                }
                signers = await self.signers_of(ids)
                fields = await self.fields_of([s.id for group in signers.values() for s in group])
                revisions = await self.revisions_of(ids)
                for request_id in ids:
                    self.ctx.checkpoint()
                    yield await self.request_item(
                        models[request_id],
                        signers=signers[request_id],
                        shown=signers[request_id],
                        fields=fields,
                        revisions=revisions[request_id],
                        chain=chains.get(request_id),
                        user=None,
                    )

        written = await self.ctx.write_json("requests", items())
        self.finish(written, events)


def export_handler(
    *,
    local: SessionFactory,
    crm: SessionFactory,
    store: DocumentStore,
    bucket: str,
) -> ExportHandler:
    """The handler the kit calls for each part of this service."""

    async def handle(ctx: PartContext) -> None:
        async with crm() as crm_session, local() as local_session:
            requester = await resolve_requester(SqlCrmLookup(crm_session), ctx.request)
            part = _Part(ctx, local=local_session, crm=crm_session, store=store, bucket=bucket)
            if requester.community_id is None:
                await part.user_export(requester)
            else:
                await part.community_export(requester.community_id)

    return handle


def start_export_consumer(stop: asyncio.Event) -> asyncio.Task[None] | None:
    """Starts this service's export consumer, or logs why it cannot (and the API runs on).

    Needs the export account (EXPORT_STORAGE_*), never the service's usual storage account.
    """
    from core.database.database import AsyncSessionCRMFactory, AsyncSessionLocalFactory
    from core.export.consumer import run_export_consumer
    from core.queue.init import get_jetstream
    from ports.storage import S3Store

    try:
        storage = ExportStorageSettings.from_env()
        js = get_jetstream()
    except RuntimeError as err:
        logger.warning(
            "Data export disabled: %s",
            err,
            extra={"operation": "export:consumer_disabled", "service": SERVICE},
        )
        return None
    handler = export_handler(
        local=AsyncSessionLocalFactory,
        crm=AsyncSessionCRMFactory,
        store=S3Store(settings.OUTPUT_BUCKET),
        bucket=settings.OUTPUT_BUCKET,
    )
    return asyncio.create_task(
        run_export_consumer(
            js,
            service=SERVICE,
            handler=handler,
            store=S3PartStore(storage),
            stop=stop,
            expected_bucket=storage.bucket,
        ),
        name="signature-export-consumer",
    )


async def stop_export_consumer(
    task: asyncio.Task[None] | None, stop: asyncio.Event, grace: float = 10.0
) -> None:
    """Stops pulling; a part in progress is aborted and nak'ed (redelivered elsewhere)."""
    stop.set()
    if task is None:
        return
    try:
        await asyncio.wait_for(task, timeout=grace)
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
