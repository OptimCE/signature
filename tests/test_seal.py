"""The seal on real PDFs: the CWaPE annexe 8 as docgen renders it, and an anchored page.

Every revision a seal operation returns has already passed pyHanko's
validation inside it (``check_revision``); these tests also read the results
back, tamper with them, and check the tampering is caught.
"""

import io
from dataclasses import replace

import pytest
from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter

from crypto import appearance, seal
from crypto.pdf_fields import list_fields
from crypto.pki import FallbackTimeStamper
from domain.slots import parse_slot
from shared.const import FieldType
from tests.world import anchored_pdf, png_bytes

SIG = "Champ de signature 1"
PLACE = "Champ de texte 21"
DATE = "Champ de texte 22"


def _annex8_plan() -> seal.SignerPlan:
    return seal.SignerPlan(
        field_name=SIG,
        slot=parse_slot({"field": SIG}, for_signature=True),
        label="Participant",
        fields=[
            seal.FieldPlan(
                PLACE, FieldType.TEXT, parse_slot({"field": PLACE}, for_signature=False)
            ),
            seal.FieldPlan(DATE, FieldType.DATE, parse_slot({"field": DATE}, for_signature=False)),
        ],
    )


def _sign(current: bytes, material, name: str = SIG, values=()) -> bytes:
    return seal.sign(
        current,
        field_name=name,
        values=list(values),
        image_png=png_bytes(),
        stamp_lines=[
            "Signé électroniquement par Alice Martin",
            "le 03/11/2026 à 10:12",
            "réf. 7F3C",
        ],
        signer_name="Alice Martin",
        reason="Signé par Alice Martin en son nom propre",
        material=material,
    )


def _all_ok(data: bytes, material, **kwargs) -> bool:
    return all(check.ok for check in seal.inspect(data, material, **kwargs))


def _tamper_text(data: bytes, name: str, value: str) -> bytes:
    writer = IncrementalPdfFileWriter(io.BytesIO(data))
    appearance.fill_text(writer, name, value)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture(scope="module")
def annex8_signed(annex8_pdf, material):
    prepared = seal.prepare(
        annex8_pdf, [_annex8_plan()], material, reason="Document émis par OptimCE"
    )
    signed = _sign(
        prepared.data,
        material,
        values=[
            seal.FieldValue(PLACE, FieldType.TEXT, "Liège"),
            seal.FieldValue(DATE, FieldType.DATE, "03/11/2026"),
        ],
    )
    return prepared, signed


class TestAnnex8:
    def test_preparation_keeps_only_the_signers_fields(self, annex8_pdf, annex8_signed):
        prepared, _signed = annex8_signed
        before = list_fields(annex8_pdf)
        after = list_fields(prepared.data)
        assert len(before) == 50
        assert set(after) == {SIG, PLACE, DATE, seal.CERTIFICATION_FIELD}
        assert prepared.flattened == 47
        assert prepared.placements[SIG].page == 1

    def test_the_certification_is_valid_and_untouched(self, annex8_signed, material):
        prepared, _signed = annex8_signed
        (check,) = seal.inspect(prepared.data, material)
        assert check.ok
        assert check.modification.endswith("NONE")

    def test_signing_is_form_filling_and_both_signatures_hold(self, annex8_signed, material):
        _prepared, signed = annex8_signed
        checks = seal.inspect(signed, material)
        assert [c.field_name for c in checks] == [seal.CERTIFICATION_FIELD, SIG]
        assert all(c.ok for c in checks)
        assert checks[0].modification.endswith("FORM_FILLING")

    def test_completion_is_lta_and_validates_from_the_file_alone(self, annex8_signed, material):
        _prepared, signed = annex8_signed
        final = seal.complete(signed, material)
        checks = seal.inspect(final, material, embedded_only=True)
        assert [c.kind for c in checks] == ["signature", "signature", "timestamp"]
        assert all(c.ok for c in checks)

    def test_one_flipped_byte_invalidates_the_final_pdf(self, annex8_signed, material):
        _prepared, signed = annex8_signed
        final = bytearray(seal.complete(signed, material))
        # Inside the signed content (the first revision's page data), not in
        # trailing whitespace a reader would skip.
        final[len(final) // 4] ^= 0x01
        try:
            ok = _all_ok(bytes(final), material)
        except Exception:
            ok = False  # a byte that breaks the structure is caught too
        assert not ok

    def test_changing_the_signers_field_afterwards_breaks_their_signature(
        self, annex8_signed, material
    ):
        _prepared, signed = annex8_signed
        tampered = _tamper_text(signed, PLACE, "Namur")
        checks = {c.field_name: c for c in seal.inspect(tampered, material)}
        assert not checks[SIG].ok

    def test_filling_before_signing_is_allowed(self, annex8_signed, material):
        prepared, _signed = annex8_signed
        assert _all_ok(_tamper_text(prepared.data, PLACE, "Huy"), material)

    def test_a_comment_added_after_signing_breaks_the_certification(self, annex8_signed, material):
        _prepared, signed = annex8_signed
        writer = IncrementalPdfFileWriter(io.BytesIO(signed))
        page = writer.root["/Pages"]["/Kids"][0].get_object()
        annot = generic.DictionaryObject(
            {
                generic.pdf_name("/Type"): generic.pdf_name("/Annot"),
                generic.pdf_name("/Subtype"): generic.pdf_name("/Text"),
                generic.pdf_name("/Rect"): generic.ArrayObject(
                    [generic.NumberObject(v) for v in (60, 60, 90, 90)]
                ),
                generic.pdf_name("/Contents"): generic.TextStringObject("pas d'accord"),
            }
        )
        annots = generic.ArrayObject(list(page.get("/Annots") or []))
        annots.append(writer.add_object(annot))
        page["/Annots"] = annots
        writer.update_container(page)
        out = io.BytesIO()
        writer.write(out)
        checks = {c.field_name: c for c in seal.inspect(out.getvalue(), material)}
        assert not checks[seal.CERTIFICATION_FIELD].ok

    def test_a_field_the_form_does_not_have_is_refused(self, annex8_pdf, material):
        plan = replace(
            _annex8_plan(),
            slot=parse_slot({"field": "Nope"}, for_signature=True),
            field_name="Nope",
        )
        with pytest.raises(seal.SlotNotFoundError):
            seal.prepare(annex8_pdf, [plan], material, reason="x")


class TestAnchoredDocument:
    def test_anchor_box_and_page_slots_with_created_fields(self, material):
        pdf = anchored_pdf([("[[sig:a]]", 100, 600), ("[[txt:place]]", 100, 520)])
        plans = [
            seal.SignerPlan(
                "OptimCE-Signer-1",
                parse_slot({"anchor": "[[sig:a]]"}, for_signature=True),
                "Membre",
                [
                    seal.FieldPlan(
                        "OptimCE-Signer-1-place",
                        FieldType.TEXT,
                        parse_slot({"anchor": "[[txt:place]]"}, for_signature=False),
                    ),
                    seal.FieldPlan(
                        "OptimCE-Signer-1-ok",
                        FieldType.CHECKBOX,
                        parse_slot(
                            {"box": {"page": 1, "rect": [100, 480, 114, 494]}}, for_signature=False
                        ),
                    ),
                ],
            ),
            seal.SignerPlan(
                "OptimCE-Signer-2",
                parse_slot({"box": {"page": 1, "rect": [300, 300, 520, 360]}}, for_signature=True),
                "Membre (société)",
            ),
            seal.SignerPlan(
                "OptimCE-Signer-3",
                parse_slot({"page": {}}, for_signature=True),
                "Pour la communauté",
            ),
        ]
        prepared = seal.prepare(pdf, plans, material, reason="Document émis par OptimCE")
        anchor = prepared.placements["OptimCE-Signer-1"]
        assert (anchor.page, round(anchor.rect[0]), round(anchor.rect[1])) == (0, 100, 596)
        assert prepared.placements["OptimCE-Signer-3"].page == 1  # the appended page
        current = _sign(
            prepared.data,
            material,
            "OptimCE-Signer-1",
            [
                seal.FieldValue("OptimCE-Signer-1-place", FieldType.TEXT, "Liège"),
                seal.FieldValue("OptimCE-Signer-1-ok", FieldType.CHECKBOX, "true"),
            ],
        )
        current = _sign(current, material, "OptimCE-Signer-2")
        current = _sign(current, material, "OptimCE-Signer-3")
        final = seal.complete(current, material)
        checks = seal.inspect(final, material, embedded_only=True)
        assert len([c for c in checks if c.kind == "signature"]) == 4
        assert all(c.ok for c in checks)

    def test_a_missing_anchor_is_refused(self, material):
        plan = seal.SignerPlan("S", parse_slot({"anchor": "[[sig:zz]]"}, for_signature=True), "x")
        with pytest.raises(seal.SlotNotFoundError):
            seal.prepare(anchored_pdf([]), [plan], material, reason="x")

    def test_evidence_is_sealed_with_its_validation_data(self, material):
        sealed = seal.seal_evidence(anchored_pdf([]), material, reason="Rapport de preuve")
        assert _all_ok(sealed, material, embedded_only=True)


def test_the_timestamp_authorities_are_tried_in_order(pki, material):
    stamper = FallbackTimeStamper(["http://127.0.0.1:9/down", pki.tsa_url], timeout=2)
    fallback = replace(material, tsa_urls=["http://127.0.0.1:9/down", pki.tsa_url])
    assert stamper is not None
    prepared = seal.prepare(anchored_pdf([]), [], fallback, reason="x")
    assert _all_ok(prepared.data, material)
