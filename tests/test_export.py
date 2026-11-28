"""The signature part of a data export (service/export.py), on the test world.

Through the kit's own contract check (`run_contract_check`): the answer matches the contract,
every file listed was written, and each JSON file is checked here against its item schema
(core/export/schemas/files/signature/).
"""

from __future__ import annotations

import json
import pathlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import pytest_asyncio
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.export import ExportHandler, run_contract_check
from domain.evidence import event_hash, verify_chain
from ports.storage import MemoryStore
from service.export import export_handler
from shared.const import SignerStatus
from shared.models.local_models import (
    SignatureEventModel,
    SignatureRequestModel,
)
from tests import flow
from tests.world import World, anchored_pdf, make_world

SCHEMAS = pathlib.Path(__file__).resolve().parents[1] / "core/export/schemas/files/signature"


@pytest_asyncio.fixture
async def world(db_session: AsyncSession) -> World:
    built = await make_world(db_session)
    await db_session.commit()
    return built


def handler_for(session: AsyncSession, store: MemoryStore) -> ExportHandler:
    @asynccontextmanager
    async def shared() -> AsyncIterator[AsyncSession]:
        yield session

    return export_handler(local=shared, crm=shared, store=store, bucket=settings.OUTPUT_BUCKET)


def _validators() -> dict[str, Draft202012Validator]:
    schemas = [json.loads(p.read_text(encoding="utf-8")) for p in SCHEMAS.glob("*.schema.json")]
    registry: Registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas
    )
    return {
        schema["title"].split("/")[1].removesuffix(".json item"): Draft202012Validator(
            schema, registry=registry, format_checker=FormatChecker()
        )
        for schema in schemas
        if "title" in schema and schema["title"].endswith(" item")
    }


async def export(
    session: AsyncSession, store: MemoryStore, *, scope: dict[str, str], requester: str
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Runs one part; returns part.done and each JSON file's items, checked against its schema."""
    result = await run_contract_check(
        handler_for(session, store), service="signature", scope=scope, requester_id=requester
    )
    assert result.outcome == "done", result.message
    validators = _validators()
    files: dict[str, list[dict[str, Any]]] = {}
    for file in result.message["files"]:
        document = json.loads(result.store.text(file["key"]))
        entity = document["entity"]
        assert document["service"] == "signature" and document["scope"] == scope["type"]
        errors = [
            f"{entity}[{i}]: {error.message}"
            for i, item in enumerate(document["items"])
            for error in validators[entity].iter_errors(item)
        ]
        assert not errors, errors
        files[entity] = document["items"]
    return result.message, files


def counters(done: dict[str, Any]) -> dict[str, int]:
    return {
        item["labelKey"].removeprefix("export.signature."): item["count"]
        for item in done["summary"]
    }


def user(world_user: Any) -> dict[str, Any]:
    return {"scope": {"type": "user", "id": world_user.sub}, "requester": world_user.sub}


def community(world: World, requester: Any) -> dict[str, Any]:
    return {"scope": {"type": "community", "id": world.community_auth}, "requester": requester.sub}


@pytest_asyncio.fixture
async def completed(db_session, client, store, bus, material, world):
    """Alice signs her slot, Bob signs ACME's, the request completes with its three files."""
    request = await flow.create(
        db_session,
        store,
        bus,
        flow.two_signer_payload(world, flow.doc_key(world)),
        anchored_pdf(flow.ANCHORS),
    )
    assert request is not None
    await flow.prepare(db_session, store, bus, material, request.id)
    alice_slot, acme_slot = await flow.signers(db_session, request.id)
    signed = await flow.sign(
        client, world.alice, request.id, alice_slot.id, fields={"place": "Liège", "bylaws": "true"}
    )
    assert signed.status_code == 202, signed.text
    await flow.seal(db_session, store, bus, material, request.id, alice_slot.id)
    signed = await flow.sign(client, world.bob, request.id, acme_slot.id)
    assert signed.status_code == 202, signed.text
    await flow.seal(db_session, store, bus, material, request.id, acme_slot.id)
    assert await flow.complete(db_session, store, bus, material, request.id) == "completed"
    request = await db_session.get(SignatureRequestModel, request.id, populate_existing=True)
    return request, alice_slot, acme_slot


class TestUserExport:
    async def test_alice_gets_her_slot_her_part_of_the_log_and_the_files(
        self, db_session, store, world, completed
    ):
        request, alice_slot, acme_slot = completed
        done, files = await export(db_session, store, **user(world.alice))

        (item,) = files["requests"]
        assert item["id"] == str(request.id) and item["status"] == "COMPLETED"
        assert item["progress"] == {"signed": 2, "total": 2}
        # Her slot only: ACME's, signed by Bob, is not hers.
        (slot,) = item["signers"]
        assert slot["id"] == str(alice_slot.id) and slot["status"] == "SIGNED"
        assert {f["name"]: f["value"] for f in slot["fields"]}["place"] == "Liège"
        assert item["chain"] is None  # part of a log cannot be verified

        # The three files the app lets her download, by reference to the documents bucket.
        targets = {r["targetPath"]: r for r in done["references"]}
        for name, key, file_name in (
            ("final", request.final_key, "signed.pdf"),
            ("evidence_pdf", request.evidence_pdf_key, "evidence.pdf"),
            ("evidence_json", request.evidence_json_key, "evidence.json"),
        ):
            path = f"signature/files/{request.id}/{file_name}"
            assert item["files"][name] == {
                "path": path,
                "sha256": getattr(request, f"{name}_sha256"),
                "file_missing": False,
            }
            assert targets[path]["key"] == key
            assert targets[path]["bucket"] == settings.OUTPUT_BUCKET
            assert targets[path]["size"] == len(store.objects[key])
        assert counters(done) == {
            "requests": 1,
            "events": len(files["events"]),
            "documents": 1,
            "evidence_files": 2,
        }

        # Her slot's rows and the request's own; nobody else's identity.
        assert {e["signer_id"] for e in files["events"]} <= {str(alice_slot.id), None}
        assert any(e["actor_sub"] == world.alice.sub and e["ip"] for e in files["events"])
        for event in files["events"]:
            if event["identity_withheld"]:
                assert event["actor_sub"] is None and event["ip"] is None
                assert event["user_agent"] is None and event["keycloak_sid"] is None
            else:
                assert event["actor_sub"] in (None, world.alice.sub)

    async def test_bob_gets_acme_s_slot_not_alice_s(self, db_session, store, world, completed):
        _request, alice_slot, acme_slot = completed
        _done, files = await export(db_session, store, **user(world.bob))
        (item,) = files["requests"]
        assert [s["id"] for s in item["signers"]] == [str(acme_slot.id)]
        assert str(alice_slot.id) not in {e["signer_id"] for e in files["events"]}

    async def test_someone_with_no_slot_gets_empty_files(self, db_session, store, world, completed):
        done, files = await export(db_session, store, **user(world.eve))
        assert files == {"requests": [], "events": []}
        assert done["references"] == []

    async def test_the_module_off_hides_nothing(self, db_session, store, bus, material, world):
        """The inbox needs the module; an export does not (contract §8)."""
        request = await flow.create(
            db_session,
            store,
            bus,
            flow.two_signer_payload(world, flow.doc_key(world)),
            anchored_pdf(flow.ANCHORS),
        )
        await flow.prepare(db_session, store, bus, material, request.id)
        await db_session.execute(
            text("UPDATE community_subscription SET is_active = false WHERE id_community = :c"),
            {"c": world.community},
        )
        _done, files = await export(db_session, store, **user(world.alice))
        (item,) = files["requests"]
        assert item["status"] == "OPEN" and item["signers"][0]["status"] == "INVITED"
        assert all(value is None for value in item["files"].values())


class TestCommunityExport:
    async def test_every_slot_and_a_log_that_verifies_from_the_archive(
        self, db_session, store, world, completed
    ):
        request, alice_slot, acme_slot = completed
        done, files = await export(db_session, store, **community(world, world.carol))

        (item,) = files["requests"]
        assert [s["id"] for s in item["signers"]] == [str(alice_slot.id), str(acme_slot.id)]
        assert all(s["status"] == SignerStatus.SIGNED.name for s in item["signers"])
        assert [r["kind"] for r in item["revisions"]][-1] == "FINAL"
        assert item["chain"]["ok"] is True

        stored = (
            await db_session.execute(
                select(func.count()).where(SignatureEventModel.request_id == request.id)
            )
        ).scalar_one()
        log = files["events"]
        assert len(log) == stored == item["chain"]["events"]
        assert not any(e["identity_withheld"] for e in log)
        assert {e["actor_sub"] for e in log} >= {world.alice.sub, world.bob.sub}
        # The archive alone proves the log: every hash recomputes from the exported rows.
        assert all(event_hash(e) == e["hash"] for e in log)
        assert verify_chain(item["original_sha256"], log).ok
        assert len(done["references"]) == 3

    async def test_a_file_gone_from_storage_is_listed_missing_not_fatal(
        self, db_session, store, world, completed
    ):
        request, _alice_slot, _acme_slot = completed
        del store.objects[request.final_key]
        done, files = await export(db_session, store, **community(world, world.carol))
        (item,) = files["requests"]
        assert item["files"]["final"] == {
            "path": None,
            "sha256": request.final_sha256,
            "file_missing": True,
        }
        assert len(done["references"]) == 2
        assert counters(done)["files_missing"] == 1

    async def test_an_empty_community_answers_empty_files(self, db_session, store, world):
        done, files = await export(db_session, store, **community(world, world.carol))
        assert files == {"events": [], "requests": []}
        assert counters(done)["requests"] == 0

    @pytest.mark.parametrize("who", ["mike", "alice"])
    async def test_only_an_admin_exports_the_community(self, db_session, store, world, who):
        result = await run_contract_check(
            handler_for(db_session, store),
            service="signature",
            **_args(community(world, getattr(world, who))),
        )
        assert result.outcome == "failed"
        assert result.message["errorCode"] == "forbidden"

    async def test_an_unknown_community_is_scope_not_found(self, db_session, store, world):
        scope = {"type": "community", "id": str(uuid.uuid4())}
        result = await run_contract_check(
            handler_for(db_session, store),
            service="signature",
            scope=scope,
            requester_id=world.carol.sub,
        )
        assert result.outcome == "failed"
        assert result.message["errorCode"] == "scope_not_found"


def _args(spec: dict[str, Any]) -> dict[str, Any]:
    return {"scope": spec["scope"], "requester_id": spec["requester"]}
