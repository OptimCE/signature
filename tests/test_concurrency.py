"""Criteria 4 and 12, with real transactions on separate connections.

These tests commit for real (the row lock that serialises a request's seals
only exists between transactions), then TRUNCATE what they wrote.
"""

import asyncio
import datetime
from dataclasses import replace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from api.deps import get_bus, get_store
from core.database.database import crm_engine, local_engine
from crypto import seal
from main import app
from service import jobs
from shared.const import RequestStatus, RevisionKind, SignerStatus
from shared.models.local_models import SignatureRequestModel, SignatureRevisionModel
from tests import flow
from tests.world import anchored_pdf, make_world
from worker import dispatcher

_TABLES = (
    "signature_event, signature_revision, signature_field, signature_signer, signature_request, "
    "outbound_message, notification, notification_preference, user_member_link, community_user, "
    "individual, company, member, app_user, community_subscription, community"
)


@pytest_asyncio.fixture
async def committed(test_engine):
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    yield factory
    async with test_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))
    # The app's own engines pool connections on this test's event loop.
    await local_engine.dispose()
    await crm_engine.dispose()


@pytest_asyncio.fixture
async def real_client(store, bus):
    """The API on its own sessions (one connection per request), store and bus faked."""

    async def _bus():
        yield bus

    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_bus] = _bus
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_three_parallel_signers_in_the_same_second(
    committed, real_client, store, bus, material
):
    """Criterion 4: three valid, ordered signatures; no lost update."""
    async with committed() as session:
        world = await make_world(session)
        await session.commit()
        payload = flow.two_signer_payload(world, flow.doc_key(world), order="PARALLEL")
        payload["signers"][0]["fields"] = []
        payload["signers"].append(
            {
                "capacity": "COMMUNITY_ADMIN",
                "user": world.carol.sub,
                "role_label": "Pour la communauté",
                "slot": {"page": {}},
            }
        )
        request = await flow.create(session, store, bus, payload, anchored_pdf(flow.ANCHORS))
        await flow.prepare(session, store, bus, material, request.id)
        slots = await flow.signers(session, request.id)
        assert {s.status for s in slots} == {SignerStatus.INVITED}

    people = [world.alice, world.bob, world.carol]
    shas = [await flow.displayed_sha(real_client, user, request.id) for user in people]
    responses = await asyncio.gather(
        *(
            flow.sign(real_client, user, request.id, slot.id, sha=sha)
            for user, slot, sha in zip(people, slots, shas, strict=True)
        )
    )
    assert [r.status_code for r in responses] == [202, 202, 202], [r.text for r in responses]

    async def seal_one(signer_id):
        async with committed() as own:
            await jobs.run_sign(
                request.id,
                signer_id,
                local=own,
                crm=own,
                store=store,
                bus=bus,
                material=lambda: material,
            )

    await asyncio.gather(*(seal_one(slot.id) for slot in slots))

    async with committed() as session:
        slots = await flow.signers(session, request.id)
        assert {s.status for s in slots} == {SignerStatus.SIGNED}
        revisions = list(
            (
                await session.execute(
                    select(SignatureRevisionModel)
                    .where(SignatureRevisionModel.request_id == request.id)
                    .order_by(SignatureRevisionModel.number)
                )
            ).scalars()
        )
        assert [r.number for r in revisions] == [1, 2, 3, 4]
        assert [r.kind for r in revisions[1:]] == [RevisionKind.SIGNED] * 3
        assert len({r.signer_id for r in revisions[1:]}) == 3
        assert sorted(s.signed_revision for s in slots) == [2, 3, 4]
        latest = store.objects[revisions[-1].key]
    checks = await asyncio.to_thread(seal.inspect, latest, material)
    assert len(checks) == 4
    assert all(c.ok for c in checks)


async def test_an_unreachable_tsa_retries_then_fails_and_parks(
    committed, store, bus, material, annex8_pdf
):
    """Criterion 12: four deliveries, then FAILED and the dead-letter stream."""
    async with committed() as session:
        world = await make_world(session)
        await session.commit()
        request = await flow.create(
            session, store, bus, flow.annex8_payload(world, flow.doc_key(world)), annex8_pdf
        )
    dead = replace(material, tsa_urls=["http://127.0.0.1:9/down"], tsa_timeout=1)
    acks, naks, parked = [], [], []

    async def ack():
        acks.append(True)

    async def nak(delay=None):
        naks.append(delay)

    async def park():
        parked.append(True)

    outcomes = []
    for delivered in range(1, dispatcher.JOB_MAX_DELIVER + 1):
        outcomes.append(
            await dispatcher.run_job(
                "signature.job.prepare",
                request.id,
                None,
                bus=bus,
                store=store,
                delivered=delivered,
                ack=ack,
                nak=nak,
                park=park,
                material=lambda: dead,
            )
        )
    assert outcomes == ["retry", "retry", "retry", "dead_lettered"]
    assert naks == [15, 30, 45]  # a growing delay
    assert parked == [True] and acks == [True]
    async with committed() as session:
        stored = await session.get(SignatureRequestModel, request.id)
        assert stored.status == RequestStatus.FAILED
        assert stored.failure_code == "seal_failed"
    assert flow.events_of(bus, flow.DomainEvent.FAILED)[-1]["code"] == "seal_failed"


@pytest.mark.parametrize("lost", ["prepare", "sign"])
async def test_the_requeue_pass_finds_what_a_lost_publish_left(
    committed, store, bus, material, annex8_pdf, lost
):
    from service.sweep import requeue_stuck

    async with committed() as session:
        world = await make_world(session)
        await session.commit()
        request = await flow.create(
            session, store, bus, flow.annex8_payload(world, flow.doc_key(world)), annex8_pdf
        )
        request_id = request.id
        if lost == "sign":
            await flow.prepare(session, store, bus, material, request.id)
            (slot,) = await flow.signers(session, request.id)
            await session.execute(
                text(
                    "UPDATE signature_signer SET status = 4, method = 1, image_key = 'k', "
                    "image_sha256 = repeat('0', 64), signed_by_sub = 'x', "
                    "signed_by_name = 'x' WHERE id = :s"
                ),
                {"s": slot.id},
            )
        await session.commit()
        # An hour later (updated_at is the trigger's, so move the clock instead).
        later = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        sent = await requeue_stuck(local=session, bus=bus, now=later)
    assert (f"signature.job.{lost}", str(request_id)) in sent
