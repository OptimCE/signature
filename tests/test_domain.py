"""The pure parts: the hash chain, slots, the NATS payload, keys, wording."""

import datetime
import hashlib
import uuid

import pytest
from pydantic import ValidationError

from domain import wording
from domain.evidence import canonical_json, event_hash, verify_chain
from domain.payloads import CreateRequestIn
from domain.slots import SlotError, normalise_rect, parse_slot
from service.keys import document_allowed
from shared.const import CONSENT_VERSION, Capacity, SlotKind

ORIGINAL = hashlib.sha256(b"%PDF-1.7 original").hexdigest()
AT = datetime.datetime(2026, 11, 3, 9, 30, 0, 123456, tzinfo=datetime.UTC)


def _chain(n: int) -> list[dict]:
    rows: list[dict] = []
    prev: str = ORIGINAL
    request_id = uuid.uuid4()
    for seq in range(1, n + 1):
        row = {
            "request_id": request_id,
            "seq": seq,
            "signer_id": None,
            "type": "signer.viewed",
            "at": AT + datetime.timedelta(seconds=seq),
            "actor_sub": "sub",
            "actor_user_id": 7,
            "ip": "203.0.113.7",
            "user_agent": "ua",
            "keycloak_sid": "sid",
            "auth_time": AT,
            "acr": "1",
            "document_sha256": ORIGINAL,
            "consent_version": None,
            "payload": {"revision": 1, "é": "Liège"},
            "prev_hash": prev,
        }
        row["hash"] = event_hash(row)
        prev = str(row["hash"])
        rows.append(row)
    return rows


class TestTheChain:
    def test_an_intact_chain_verifies_from_the_original_digest(self):
        rows = _chain(4)
        check = verify_chain(ORIGINAL, list(reversed(rows)))
        assert check.ok
        assert check.events == 4
        assert check.head == rows[-1]["hash"]

    def test_editing_one_field_of_one_row_breaks_it_there(self):
        rows = _chain(4)
        rows[1]["ip"] = "198.51.100.1"
        check = verify_chain(ORIGINAL, rows)
        assert not check.ok
        assert (check.broken_at, check.reason) == (2, "hash")

    def test_rehashing_an_edited_row_breaks_the_next_link(self):
        rows = _chain(3)
        rows[0]["payload"] = {"revision": 9}
        rows[0]["hash"] = event_hash(rows[0])
        check = verify_chain(ORIGINAL, rows)
        assert (check.broken_at, check.reason) == (2, "prev_hash")

    def test_a_removed_row_is_a_gap(self):
        rows = _chain(3)
        check = verify_chain(ORIGINAL, [rows[0], rows[2]])
        assert (check.broken_at, check.reason) == (3, "sequence")

    def test_a_chain_from_another_document_does_not_verify(self):
        check = verify_chain(hashlib.sha256(b"other").hexdigest(), _chain(2))
        assert (check.broken_at, check.reason) == (1, "prev_hash")

    def test_canonical_json_sorts_keys_and_keeps_utf8(self):
        assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode()

    def test_naive_timestamps_and_floats_are_refused(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            canonical_json({"at": datetime.datetime(2026, 1, 1)})
        with pytest.raises(ValueError, match="no floats"):
            canonical_json({"x": 1.5})


class TestSlots:
    @pytest.mark.parametrize(
        ("raw", "kind"),
        [
            ({"field": "Champ de signature 1"}, SlotKind.FIELD),
            ({"anchor": "[[sig:member]]"}, SlotKind.ANCHOR),
            ({"box": {"page": 2, "rect": [10, 20, 200, 80]}}, SlotKind.BOX),
            ({"page": {}}, SlotKind.PAGE),
        ],
    )
    def test_the_four_kinds(self, raw, kind):
        assert parse_slot(raw, for_signature=True).kind is kind

    def test_a_box_page_is_one_based_in_the_contract(self):
        assert parse_slot({"box": {"page": 1, "rect": [1, 2, 3, 4]}}, for_signature=True).page == 0

    @pytest.mark.parametrize(
        "raw",
        [
            {},
            {"field": ""},
            {"anchor": "sig:member"},
            {"box": {"page": 0, "rect": [1, 2, 3, 4]}},
            {"box": {"page": 1, "rect": [10, 20, 5, 80]}},
            {"field": "a", "anchor": "[[sig:a]]"},
            {"anchor": "[[sig:a]]", "width": 2000},
        ],
    )
    def test_malformed_slots_are_refused(self, raw):
        with pytest.raises(SlotError):
            parse_slot(raw, for_signature=True)

    def test_only_a_signature_goes_on_an_appended_page(self):
        with pytest.raises(SlotError):
            parse_slot({"page": {}}, for_signature=False)

    def test_rectangles_are_normalised(self):
        assert normalise_rect([200, 80, 10, 20]) == (10, 20, 200, 80)


def _payload(**overrides):
    base = {
        "idempotency_key": "admin-doc-1-v1",
        "source_service": "administrative_document",
        "source_ref": "document:1",
        "community_id": 3,
        "title": "Annexe 8",
        "document_key": "administrative-document/3/1/v1/x.pdf",
        "signers": [{"member_id": 5, "role_label": "Participant", "slot": {"field": "Sig"}}],
    }
    base.update(overrides)
    return base


class TestPayload:
    def test_a_minimal_request(self):
        parsed = CreateRequestIn.model_validate(_payload())
        assert parsed.signing_order == "PARALLEL"
        assert parsed.locale == "fr"

    def test_an_admin_slot_names_a_user_not_a_member(self):
        with pytest.raises(ValidationError):
            CreateRequestIn.model_validate(
                _payload(
                    signers=[
                        {
                            "capacity": "COMMUNITY_ADMIN",
                            "member_id": 5,
                            "role_label": "x",
                            "slot": {"page": {}},
                        }
                    ]
                )
            )
        CreateRequestIn.model_validate(
            _payload(
                signers=[
                    {
                        "capacity": "COMMUNITY_ADMIN",
                        "user": "sub",
                        "role_label": "x",
                        "slot": {"page": {}},
                    }
                ]
            )
        )

    def test_unknown_keys_and_naive_expiry_are_refused(self):
        with pytest.raises(ValidationError):
            CreateRequestIn.model_validate(_payload(surprise=True))
        with pytest.raises(ValidationError):
            CreateRequestIn.model_validate(_payload(expires_at="2027-02-01T00:00:00"))

    def test_a_document_key_may_not_climb(self):
        with pytest.raises(ValidationError):
            CreateRequestIn.model_validate(
                _payload(document_key="administrative-document/3/../../x.pdf")
            )


class TestPrefixes:
    def test_a_domain_names_only_its_own_prefix_in_its_community(self):
        assert document_allowed(
            "administrative_document", 3, "administrative-document/3/1/v1/x.pdf"
        )
        assert not document_allowed(
            "administrative_document", 3, "administrative-document/4/1/v1/x.pdf"
        )
        assert not document_allowed("administrative_document", 3, "billing/3/x.pdf")
        assert not document_allowed("billing", 3, "billing/3/x.pdf")


class TestWording:
    def test_the_consent_names_the_member_and_has_a_stable_digest(self):
        first = wording.consent(
            "v1", "fr", Capacity.LEGAL_REPRESENTATIVE, member="ACME SRL", community=None
        )
        again = wording.consent("v1", "fr", Capacity.SELF, member=None, community=None)
        assert "ACME SRL" in first.text
        assert first.sha256 == again.sha256
        assert len(first.sha256) == 64

    @pytest.mark.parametrize("locale", ["fr", "en", "nl", "de"])
    def test_every_locale_has_every_capacity(self, locale):
        for capacity in Capacity:
            for version in ("v1", "v2"):
                assert wording.consent(version, locale, capacity, member="M", community="C").text
            assert wording.reason(locale, capacity, name="N", member="M", community="C")

    def test_new_requests_take_v2_whose_first_three_sentences_are_v1s(self):
        assert CONSENT_VERSION == "v2"
        for locale in ("fr", "en", "nl", "de"):
            for capacity in Capacity:
                kwargs = {"member": None, "community": "C"}
                if capacity is Capacity.LEGAL_REPRESENTATIVE:
                    kwargs["member"] = "ACME SRL"
                v1 = wording.consent("v1", locale, capacity, **kwargs)
                v2 = wording.consent("v2", locale, capacity, **kwargs)
                assert v1.text == v2.text
                assert v1.sha256 != v2.sha256  # the evidence tells the two files apart

    @pytest.mark.parametrize("locale", ["fr", "en", "nl", "de"])
    def test_v2_says_when_someone_signs_for_the_member(self, locale):
        own = wording.consent("v2", locale, Capacity.SELF, member=None, community=None)
        other = wording.consent("v2", locale, Capacity.SELF, member="Alice Martin", community=None)
        assert "Alice Martin" in other.text and "Alice Martin" not in own.text
        assert other.text != own.text

    def test_the_on_behalf_sentence_in_french(self):
        assert wording.consent(
            "v2", "fr", Capacity.SELF, member="Alice Martin", community=None
        ).text == (
            "J'ai lu ce document et je le signe électroniquement au nom et pour le compte de "
            "Alice Martin, qui m'en a donné l'autorisation."
        )

    def test_a_v1_request_keeps_its_own_sentence(self):
        """v1 has no ON_BEHALF: a request created before v2 shows what it always did."""
        assert wording.consent(
            "v1", "fr", Capacity.SELF, member="Alice Martin", community=None
        ).text == ("J'ai lu ce document et je le signe électroniquement en mon nom propre.")

    @pytest.mark.parametrize(
        ("user_name", "user_email", "same"),
        [
            ("Alice Martin", "other@example.test", True),
            ("martin  ALICE", None, True),  # order, case and spacing aside
            ("Hélène Lefèvre-Dubois", None, False),  # another person
            ("Eve Outsider", "ALICE@example.test", True),  # the same e-mail
            ("", None, False),
        ],
    )
    def test_same_person_is_the_same_email_or_the_same_name_words(
        self, user_name, user_email, same
    ):
        assert (
            wording.same_person(user_name, user_email, "Alice Martin", "alice@example.test") is same
        )

    def test_accents_and_hyphens_do_not_make_another_person(self):
        assert wording.same_person("Helene Lefevre Dubois", None, "Hélène Lefèvre-Dubois", None)

    def test_the_pdf_names_the_person_and_the_member_they_sign_for(self):
        def who(capacity, user, slot="Alice Martin", label=None):
            person = wording.signed_by_name(
                capacity,
                user_name=user,
                user_email=None,
                slot_name=slot,
                slot_email="alice@example.test",
            )
            return person, wording.signed_for(
                capacity, person=person, slot_name=slot, member_label=label
            )

        # Her own account: the member's name, on her own behalf.
        assert who(Capacity.SELF, "alice MARTIN") == ("Alice Martin", None)
        # Another account linked to her: named, for her.
        assert who(Capacity.SELF, "Eve Outsider") == ("Eve Outsider", "Alice Martin")
        # A company's representative: the person, for the company (never « ACME pour ACME »).
        assert who(Capacity.LEGAL_REPRESENTATIVE, "Bob Dupont", "ACME SRL", "ACME SRL") == (
            "Bob Dupont",
            "ACME SRL",
        )
        assert who(Capacity.COMMUNITY_ADMIN, "Carol Admin", "Carol Admin") == ("Carol Admin", None)

    def test_reason_and_stamp_say_who_signed_for_whom(self):
        at = datetime.datetime(2026, 11, 3, 14, 26, tzinfo=datetime.UTC)
        assert (
            wording.reason("fr", Capacity.SELF, name="Alice Martin", member=None, community=None)
            == "Signé par Alice Martin en son nom propre"
        )
        assert (
            wording.reason(
                "fr", Capacity.SELF, name="Eve Outsider", member="Alice Martin", community=None
            )
            == "Signé par Eve Outsider pour Alice Martin"
        )
        assert (
            wording.reason(
                "fr",
                Capacity.LEGAL_REPRESENTATIVE,
                name="Bob Dupont",
                member="ACME SRL",
                community=None,
            )
            == "Signé par Bob Dupont en qualité de représentant légal de ACME SRL"
        )
        assert wording.stamp_lines(
            "fr", name="Bob Dupont", at=at, ref="E2A39C1D", for_member="ACME SRL"
        ) == [
            "Signé électroniquement par Bob Dupont",
            "pour ACME SRL",
            "le 03/11/2026 à 15:26",
            "réf. E2A39C1D",
        ]
        assert len(wording.stamp_lines("fr", name="Alice Martin", at=at, ref="X")) == 3

    @pytest.mark.parametrize("locale", ["fr", "en", "nl", "de"])
    def test_every_locale_says_for_whom(self, locale):
        at = datetime.datetime(2026, 11, 3, tzinfo=datetime.UTC)
        own = wording.reason(locale, Capacity.SELF, name="N", member=None, community=None)
        other = wording.reason(locale, Capacity.SELF, name="N", member="M", community=None)
        assert other != own and "M" in other
        assert "M" in wording.stamp_lines(locale, name="N", at=at, ref="R", for_member="M")[1]

    def test_the_stamp_and_the_date_are_in_brussels_time(self):
        late = datetime.datetime(2026, 11, 3, 23, 30, tzinfo=datetime.UTC)
        assert wording.signing_date("fr", late) == "04/11/2026"
        assert (
            wording.stamp_lines("fr", name="Alice", at=late, ref="AB12")[1]
            == "le 04/11/2026 à 00:30"
        )
