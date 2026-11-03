"""A request through its life, and the spec's acceptance criteria.

The NATS side is driven through the service functions the consumers call
(``handle_create``, ``run_prepare``, ``run_sign``, ``run_complete``,
``handle_docgen_result``) with a RecordingBus; the signer's side through the
HTTP API with the gateway's headers.
"""

import asyncio
import io
import json
import os
import pathlib
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from crypto import seal
from service import sweep
from shared.const import DomainEvent, EventType, RequestStatus, SignerStatus
from shared.models.local_models import (
    SignatureEventModel,
    SignatureFieldModel,
    SignatureRequestModel,
    SignatureRevisionModel,
)
from tests import flow
from tests.world import anchored_pdf, headers, make_world


@pytest_asyncio.fixture
async def world(db_session):
    built = await make_world(db_session)
    await db_session.commit()
    return built


async def _events(session, request_id) -> list[str]:
    rows = await session.execute(
        select(SignatureEventModel.type)
        .where(SignatureEventModel.request_id == request_id)
        .order_by(SignatureEventModel.seq)
    )
    return list(rows.scalars())


async def _notifications(session, user_id) -> list[str]:
    rows = await session.execute(
        text("SELECT type FROM notification WHERE id_user = :u ORDER BY id"), {"u": user_id}
    )
    return list(rows.scalars())


def _named(pdf: bytes) -> dict[str, tuple[str, str]]:
    """Each signer's signature in the PDF: its /Name and /Reason, by field."""
    from pyhanko.pdf_utils.reader import PdfFileReader

    return {
        sig.field_name: (str(sig.sig_object.get("/Name")), str(sig.sig_object.get("/Reason")))
        for sig in PdfFileReader(io.BytesIO(pdf)).embedded_signatures
        if sig.field_name != seal.CERTIFICATION_FIELD
        and sig.sig_object.get("/Type") != "/DocTimeStamp"
    }


class TestSequentialTwoSigners:
    """Criteria 1 and 2: a SEQUENTIAL two-signer request completes with its three artifacts."""

    async def test_from_nats_to_completed(self, db_session, client, store, bus, material, world):
        pdf = anchored_pdf(flow.ANCHORS)
        request = await flow.create(
            db_session, store, bus, flow.two_signer_payload(world, flow.doc_key(world)), pdf
        )
        assert request is not None
        assert flow.events_of(bus, DomainEvent.ACCEPTED)[0]["request_id"] == str(request.id)
        await flow.prepare(db_session, store, bus, material, request.id)
        alice_slot, acme_slot = await flow.signers(db_session, request.id)
        assert (alice_slot.status, acme_slot.status) == (SignerStatus.INVITED, SignerStatus.WAITING)
        assert "signature.requested" in await _notifications(db_session, world.alice.id)
        assert acme_slot.member_label == "ACME SRL"

        # Alice: her inbox, her screen, the fields, then the seal.
        inbox = (
            await client.get("/me/requests?status=pending", headers=headers(world.alice))
        ).json()["data"]
        assert [item["signer"]["id"] for item in inbox] == [str(alice_slot.id)]
        detail = (await client.get(f"/requests/{request.id}", headers=headers(world.alice))).json()[
            "data"
        ]
        assert detail["my_slots"][0]["consent_text"].startswith("J'ai lu ce document")
        response = await flow.sign(
            client,
            world.alice,
            request.id,
            alice_slot.id,
            fields={"place": "Liège", "bylaws": "true"},
        )
        assert response.status_code == 202, response.text
        assert bus.jobs[-1][1] == {"request_id": str(request.id), "signer_id": str(alice_slot.id)}
        await flow.seal(db_session, store, bus, material, request.id, alice_slot.id)
        alice_slot, acme_slot = await flow.signers(db_session, request.id)
        assert (alice_slot.status, acme_slot.status) == (SignerStatus.SIGNED, SignerStatus.INVITED)

        # Bob, for ACME.
        response = await flow.sign(client, world.bob, request.id, acme_slot.id)
        assert response.status_code == 202, response.text
        await flow.seal(db_session, store, bus, material, request.id, acme_slot.id)
        assert bus.jobs[-1][0] == "signature.job.complete"
        assert await flow.complete(db_session, store, bus, material, request.id) == "completed"

        request = await db_session.get(SignatureRequestModel, request.id, populate_existing=True)
        assert request.status == RequestStatus.COMPLETED
        (done,) = flow.events_of(bus, DomainEvent.COMPLETED)
        assert done["final_key"] == request.final_key
        assert done["evidence_pdf_key"] and done["evidence_json_key"]
        assert all(
            store.objects[done[k]] for k in ("final_key", "evidence_pdf_key", "evidence_json_key")
        )

        # Criterion 2: one certification, one signature per signer, one document timestamp.
        checks = await asyncio.to_thread(
            seal.inspect, store.objects[request.final_key], material, embedded_only=True
        )
        assert [c.kind for c in checks] == ["signature", "signature", "signature", "timestamp"]
        assert checks[0].field_name == seal.CERTIFICATION_FIELD
        assert all(c.ok for c in checks)
        # Each signature names the person, and the company Bob signs for.
        assert sorted(_named(store.objects[request.final_key]).values()) == [
            ("Alice Martin", "Signé par Alice Martin en son nom propre"),
            ("Bob Dupont", "Signé par Bob Dupont en qualité de représentant légal de ACME SRL"),
        ]
        signed = await db_session.execute(
            text(
                "SELECT data->>'signer' FROM notification "
                "WHERE id_user = :u AND type = 'signature.signed' ORDER BY id"
            ),
            {"u": world.mike.id},
        )
        assert list(signed.scalars()) == ["Alice Martin", "Bob Dupont"]

        types = await _events(db_session, request.id)
        assert types[0] == EventType.REQUEST_CREATED
        assert types[-1] == EventType.REQUEST_COMPLETED
        assert types.count(EventType.SIGNER_SIGNED) == 2
        assert "signature.completed" in await _notifications(db_session, world.mike.id)

        # The evidence: the values and the chain.
        evidence = json.loads(store.objects[request.evidence_json_key])
        if dump := os.getenv("SIGNATURE_EVIDENCE_DUMP"):  # to render the template by hand
            pathlib.Path(dump).write_bytes(store.objects[request.evidence_json_key])
        assert evidence["chain"]["verified"] is True
        assert evidence["document"]["final_sha256"] == request.final_sha256
        assert evidence["signers"][0]["fields"]["place"] == "Liège"
        assert evidence["signers"][0]["capacity"] == "SELF"
        assert evidence["signers"][1]["capacity"] == "LEGAL_REPRESENTATIVE"
        assert [s["signed_by_name"] for s in evidence["signers"]] == ["Alice Martin", "Bob Dupont"]
        assert [s["display_name"] for s in evidence["signers"]] == ["Alice Martin", "ACME SRL"]

        # The artifacts, for a signer and for a manager.
        final = await client.get(
            f"/requests/{request.id}/artifacts/final", headers=headers(world.alice)
        )
        assert final.status_code == 200
        assert final.content == store.objects[request.final_key]
        timeline = await client.get(f"/requests/{request.id}/events", headers=headers(world.mike))
        assert timeline.json()["data"]["chain"]["ok"] is True


class TestOneSigner:
    async def _open(self, db_session, store, bus, material, world, **kwargs):
        payload = flow.annex8_payload(world, flow.doc_key(world), **kwargs)
        request = await flow.create(
            db_session, store, bus, payload, kwargs.pop("pdf", None) or self.pdf
        )
        await flow.prepare(db_session, store, bus, material, request.id)
        (slot,) = await flow.signers(db_session, request.id)
        return request, slot

    @pytest.fixture(autouse=True)
    def _pdf(self, annex8_pdf):
        self.pdf = annex8_pdf

    async def test_required_fields_block_then_land_in_the_pdf_and_the_evidence(
        self, db_session, client, store, bus, material, world
    ):
        """Criterion 7."""
        request, slot = await self._open(db_session, store, bus, material, world)
        refused = await flow.sign(client, world.alice, request.id, slot.id, fields={})
        assert refused.status_code == 422
        assert refused.json()["error_code"] == 2515
        accepted = await flow.sign(
            client, world.alice, request.id, slot.id, fields={"place": "Liège"}
        )
        assert accepted.status_code == 202
        await flow.seal(db_session, store, bus, material, request.id, slot.id)
        fields = {
            f.name: f.value
            for f in (
                await db_session.execute(
                    select(SignatureFieldModel).where(SignatureFieldModel.signer_id == slot.id)
                )
            ).scalars()
        }
        assert fields["place"] == "Liège"
        assert len(fields["date"]) == 10  # the server's signing date, dd/mm/yyyy
        await flow.complete(db_session, store, bus, material, request.id)
        request = await db_session.get(SignatureRequestModel, request.id, populate_existing=True)
        import io

        from pypdf import PdfReader

        values = {
            k: v.get("/V")
            for k, v in (
                PdfReader(io.BytesIO(store.objects[request.final_key])).get_fields() or {}
            ).items()
        }
        assert values["Champ de texte 21"] == "Liège"
        assert values["Champ de texte 22"] == fields["date"]
        evidence = json.loads(store.objects[request.evidence_json_key])
        assert evidence["signers"][0]["fields"] == {"date": fields["date"], "place": "Liège"}

    async def test_a_stale_login_must_step_up(
        self, db_session, client, store, bus, material, world
    ):
        """Criterion 5 (server side): an old auth_time answers step_up_required."""
        request, slot = await self._open(db_session, store, bus, material, world)
        stale = await flow.sign(
            client,
            world.alice,
            request.id,
            slot.id,
            fields={"place": "Liège"},
            auth_age_seconds=900,
        )
        assert stale.status_code == 401
        assert stale.json()["error_code"] == 2510
        missing = await client.post(
            f"/requests/{request.id}/signers/{slot.id}/sign",
            json=flow.sign_body("0" * 64, fields={"place": "x"}),
            headers={k: v for k, v in headers(world.alice).items() if k != "x-auth-time"},
        )
        assert missing.status_code == 401

    async def test_a_hash_that_is_not_a_revision_is_409(
        self, db_session, client, store, bus, material, world
    ):
        """Criterion 6."""
        request, slot = await self._open(db_session, store, bus, material, world)
        response = await flow.sign(
            client, world.alice, request.id, slot.id, fields={"place": "x"}, sha="ab" * 32
        )
        assert response.status_code == 409
        assert response.json()["error_code"] == 2511

    async def test_a_second_sign_call_is_409(self, db_session, client, store, bus, material, world):
        request, slot = await self._open(db_session, store, bus, material, world)
        assert (
            await flow.sign(client, world.alice, request.id, slot.id, fields={"place": "x"})
        ).status_code == 202
        again = await flow.sign(client, world.alice, request.id, slot.id, fields={"place": "x"})
        assert again.status_code == 409
        assert again.json()["error_code"] == 2512

    async def test_only_a_linked_account_signs(
        self, db_session, client, store, bus, material, world
    ):
        request, slot = await self._open(db_session, store, bus, material, world)
        assert (
            await flow.sign(
                client, world.eve, request.id, slot.id, fields={"place": "x"}, sha="ab" * 32
            )
        ).status_code in (403, 404)
        # Eve cannot even read it.
        assert (
            await client.get(f"/requests/{request.id}", headers=headers(world.eve))
        ).status_code == 404
        # A second account linked to the same member may (decided 03/11/2026).
        await db_session.execute(
            text("INSERT INTO user_member_link (id_user, id_member) VALUES (:u, :m)"),
            {"u": world.eve.id, "m": world.alice_member},
        )
        await db_session.commit()
        response = await flow.sign(
            client, world.eve, request.id, slot.id, fields={"place": "Namur"}
        )
        assert response.status_code == 202
        (slot,) = await flow.signers(db_session, request.id)
        assert slot.signed_by_sub == world.eve.sub
        # The PDF names Eve, for Alice: not Alice, and not « en son nom propre ».
        assert slot.signed_by_name == "Eve Outsider"
        await flow.seal(db_session, store, bus, material, request.id, slot.id)
        request = await db_session.get(SignatureRequestModel, request.id, populate_existing=True)
        revision = (
            await db_session.execute(
                select(SignatureRevisionModel).where(
                    SignatureRevisionModel.request_id == request.id,
                    SignatureRevisionModel.number == request.current_revision,
                )
            )
        ).scalar_one()
        if dump := os.getenv("SIGNATURE_SIGNED_DUMP"):  # to look at the four-line stamp
            pathlib.Path(dump).write_bytes(store.objects[revision.key])
        assert _named(store.objects[revision.key]) == {
            slot.field_name: ("Eve Outsider", "Signé par Eve Outsider pour Alice Martin")
        }

    async def test_view_is_recorded_once_per_revision(
        self, db_session, client, store, bus, material, world
    ):
        request, slot = await self._open(db_session, store, bus, material, world)
        sha = await flow.displayed_sha(client, world.alice, request.id)
        for _ in range(2):
            response = await client.post(
                f"/requests/{request.id}/signers/{slot.id}/view",
                json={"document_sha256": sha},
                headers=headers(world.alice),
            )
            assert response.status_code == 204
        types = await _events(db_session, request.id)
        assert types.count(EventType.SIGNER_VIEWED) == 1

    async def test_decline_stops_the_flow_and_tells_the_requester(
        self, db_session, client, store, bus, material, world
    ):
        """Criterion 10."""
        request, slot = await self._open(db_session, store, bus, material, world)
        empty = await client.post(
            f"/requests/{request.id}/signers/{slot.id}/decline",
            json={"reason": " "},
            headers=headers(world.alice),
        )
        assert empty.status_code == 422
        response = await client.post(
            f"/requests/{request.id}/signers/{slot.id}/decline",
            json={"reason": "L'EAN est erroné"},
            headers=headers(world.alice),
        )
        assert response.status_code == 204
        request = await db_session.get(SignatureRequestModel, request.id, populate_existing=True)
        assert request.status == RequestStatus.DECLINED
        (declined,) = flow.events_of(bus, DomainEvent.DECLINED)
        assert declined["reason"] == "L'EAN est erroné"
        assert "signature.declined" in await _notifications(db_session, world.mike.id)
        late = await flow.sign(
            client, world.alice, request.id, slot.id, fields={"place": "x"}, sha="ab" * 32
        )
        assert late.status_code == 409

    async def test_expiry(self, db_session, store, bus, material, world):
        """Criterion 11."""
        request, _slot = await self._open(db_session, store, bus, material, world)
        later = request.expires_at.replace(year=request.expires_at.year + 1)
        expired = await sweep.expire_due(local=db_session, crm=db_session, bus=bus, now=later)
        assert request.id in expired
        request = await db_session.get(SignatureRequestModel, request.id, populate_existing=True)
        assert request.status == RequestStatus.EXPIRED
        assert flow.events_of(bus, DomainEvent.EXPIRED)[0]["request_id"] == str(request.id)
        assert (await _events(db_session, request.id))[-1] == EventType.REQUEST_EXPIRED

    async def test_editing_an_event_row_breaks_verify_chain(
        self, db_session, client, store, bus, material, world
    ):
        """Criterion 3 (the log): only a superuser past the trigger edits a row; the chain sees."""
        request, _slot = await self._open(db_session, store, bus, material, world)
        with pytest.raises(Exception, match="append-only"):
            async with db_session.begin_nested():
                await db_session.execute(
                    text(
                        "UPDATE signature_event SET ip = '198.51.100.9' "
                        "WHERE request_id = :r AND seq = 1"
                    ),
                    {"r": request.id},
                )
        await db_session.execute(
            text("ALTER TABLE signature_event DISABLE TRIGGER trg_signature_event_immutable")
        )
        await db_session.execute(
            text(
                "UPDATE signature_event SET ip = '198.51.100.9' WHERE request_id = :r AND seq = 1"
            ),
            {"r": request.id},
        )
        await db_session.execute(
            text("ALTER TABLE signature_event ENABLE TRIGGER trg_signature_event_immutable")
        )
        timeline = (
            await client.get(f"/requests/{request.id}/events", headers=headers(world.mike))
        ).json()["data"]
        assert timeline["chain"]["ok"] is False
        assert timeline["chain"]["broken_at"] == 1


class TestRefusals:
    async def test_module_disabled_answers_failed(self, db_session, store, bus):
        """Criterion 8."""
        world = await make_world(db_session, module=False)
        await db_session.commit()
        payload = flow.annex8_payload(world, flow.doc_key(world))
        assert await flow.create(db_session, store, bus, payload, anchored_pdf([])) is None
        (failed,) = flow.events_of(bus, DomainEvent.FAILED)
        assert failed["code"] == "module_disabled"
        assert failed["source_ref"] == payload["source_ref"]

    async def test_the_same_key_twice_is_one_request(
        self, db_session, store, bus, world, annex8_pdf
    ):
        """Criterion 9."""
        payload = flow.annex8_payload(world, flow.doc_key(world), idempotency_key="admin-doc-42-v1")
        first = await flow.create(db_session, store, bus, payload, annex8_pdf)
        again = await flow.create(db_session, store, bus, payload, annex8_pdf)
        assert first is not None and again is not None and first.id == again.id
        count = (
            await db_session.execute(
                text(
                    "SELECT count(*) FROM signature_request "
                    "WHERE idempotency_key = 'admin-doc-42-v1'"
                )
            )
        ).scalar_one()
        assert count == 1
        accepted = flow.events_of(bus, DomainEvent.ACCEPTED)
        assert [a["request_id"] for a in accepted] == [str(first.id)] * 2

    @pytest.mark.parametrize(
        ("change", "code"),
        [
            (
                lambda w, p: p.update(document_key=f"billing/{w.community}/x.pdf"),
                "document_outside_prefix",
            ),
            (
                lambda w, p: p["signers"][0].update(member_id=w.orphan_member),
                "member_without_account",
            ),
            (lambda w, p: p["signers"][0].update(member_id=999999), "member_not_found"),
            (lambda w, p: p.update(source_service="nobody"), "unknown_source"),
            (lambda w, p: p["signers"][0].update(slot={"anchor": "not a tag"}), "invalid_request"),
            (
                lambda w, p: p.update(
                    signers=[
                        {
                            "capacity": "COMMUNITY_ADMIN",
                            "user": w.mike.sub,
                            "role_label": "x",
                            "slot": {"page": {}},
                        }
                    ]
                ),
                "user_not_admin",
            ),
        ],
    )
    async def test_what_a_domain_can_get_wrong(self, db_session, store, bus, world, change, code):
        payload = flow.annex8_payload(world, flow.doc_key(world))
        change(world, payload)
        store.objects[payload["document_key"]] = anchored_pdf([])
        assert await flow.create(db_session, store, bus, payload, anchored_pdf([])) is None
        assert flow.events_of(bus, DomainEvent.FAILED)[0]["code"] == code

    async def test_a_missing_document_answers_failed(self, db_session, store, bus, world):
        payload = flow.annex8_payload(world, flow.doc_key(world))
        from service import requests

        assert (
            await requests.handle_create(
                payload, local=db_session, crm=db_session, store=store, bus=bus
            )
            is None
        )
        assert flow.events_of(bus, DomainEvent.FAILED)[0]["code"] == "document_not_found"

    async def test_a_slot_the_document_lacks_fails_at_preparation(
        self, db_session, store, bus, material, world
    ):
        from service.failure import PermanentJobError

        payload = flow.annex8_payload(world, flow.doc_key(world))
        request = await flow.create(db_session, store, bus, payload, anchored_pdf([]))
        with pytest.raises(PermanentJobError) as caught:
            await flow.prepare(db_session, store, bus, material, request.id)
        assert caught.value.code.value == "slot_not_found"


class TestFollowUp:
    async def test_managers_list_remind_and_cancel(
        self, db_session, client, store, bus, material, world, annex8_pdf
    ):
        request = await flow.create(
            db_session, store, bus, flow.annex8_payload(world, flow.doc_key(world)), annex8_pdf
        )
        await flow.prepare(db_session, store, bus, material, request.id)
        manager = headers(world.mike, community_auth=world.community_auth, role="MANAGER")
        response = await client.get("/admin/requests", headers=manager)
        assert response.status_code == 200, response.text
        listed = response.json()["data"]
        assert listed["total"] == 1
        assert listed["items"][0]["progress"] == {"signed": 0, "total": 1}
        member = headers(world.alice, community_auth=world.community_auth, role="MEMBER")
        assert (await client.get("/admin/requests", headers=member)).status_code == 403
        reminded = await client.post(f"/requests/{request.id}/remind", headers=headers(world.mike))
        assert reminded.json()["data"] == {"reminded": 1}
        assert (
            await client.post(f"/requests/{request.id}/remind", headers=headers(world.mike))
        ).status_code == 409
        assert "signature.reminder" in await _notifications(db_session, world.alice.id)
        assert (
            await client.post(
                f"/requests/{request.id}/cancel", json={}, headers=headers(world.alice)
            )
        ).status_code == 403
        cancelled = await client.post(
            f"/requests/{request.id}/cancel", json={"reason": "erreur"}, headers=headers(world.mike)
        )
        assert cancelled.status_code == 204
        request = await db_session.get(SignatureRequestModel, request.id, populate_existing=True)
        assert request.status == RequestStatus.CANCELLED
        assert flow.events_of(bus, DomainEvent.CANCELLED)[0]["reason"] == "erreur"

    async def test_the_module_off_is_an_explicit_error(
        self, db_session, client, store, bus, material, world, annex8_pdf
    ):
        request = await flow.create(
            db_session, store, bus, flow.annex8_payload(world, flow.doc_key(world)), annex8_pdf
        )
        await db_session.execute(
            text("UPDATE community_subscription SET is_active = FALSE WHERE id_community = :c"),
            {"c": world.community},
        )
        await db_session.commit()
        response = await client.get(f"/requests/{request.id}", headers=headers(world.alice))
        assert response.status_code == 403
        assert response.json()["error_code"] == 2503


class TestRevisions:
    async def test_revisions_and_the_log_are_append_only(
        self, db_session, store, bus, material, world, annex8_pdf
    ):
        request = await flow.create(
            db_session, store, bus, flow.annex8_payload(world, flow.doc_key(world)), annex8_pdf
        )
        await flow.prepare(db_session, store, bus, material, request.id)
        for statement in (
            "UPDATE signature_revision SET sha256 = repeat('0', 64) WHERE request_id = :r",
            "DELETE FROM signature_revision WHERE request_id = :r",
            "DELETE FROM signature_event WHERE request_id = :r",
        ):
            with pytest.raises(Exception, match="append-only"):
                async with db_session.begin_nested():
                    await db_session.execute(text(statement), {"r": request.id})
        revision = (
            await db_session.execute(
                select(SignatureRevisionModel).where(
                    SignatureRevisionModel.request_id == request.id
                )
            )
        ).scalar_one()
        assert revision.number == 1

    async def test_an_event_that_skips_or_chains_wrong_is_refused(
        self, db_session, store, bus, world, annex8_pdf
    ):
        request = await flow.create(
            db_session, store, bus, flow.annex8_payload(world, flow.doc_key(world)), annex8_pdf
        )
        with pytest.raises(Exception, match="expects seq"):
            async with db_session.begin_nested():
                await db_session.execute(
                    text(
                        "INSERT INTO signature_event (request_id, seq, type, at, prev_hash, hash) "
                        "VALUES (:r, 5, 'request.reminded', now(), "
                        "repeat('a', 64), repeat('b', 64))"
                    ),
                    {"r": request.id},
                )


async def test_unknown_request_is_404(client, db_session):
    world = await make_world(db_session)
    await db_session.commit()
    response = await client.get(f"/requests/{uuid.uuid4()}", headers=headers(world.alice))
    assert response.status_code == 404
