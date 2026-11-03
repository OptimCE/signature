"""The three seal operations, and the check every revision passes before it is kept.

1. **prepare** — fields placed, everything else flattened, then the platform's
   certification signature (DocMDP 2: form filling and signing allowed),
   PAdES-B-T. Revision 1: the document as issued by OptimCE.
2. **sign** — one signer's field values and signature in ONE incremental
   update, PAdES-B-T; the signer's /Lock covers their own fields.
3. **complete** — the validation data of every signature (DSS) and a document
   timestamp: PAdES-B-LTA. The FINAL revision.

The cryptographic signer is always the platform's seal; the person is named
in the signature's metadata and proven by the evidence log (a simple
electronic signature, see the spec's « Legal level »).

All functions are synchronous and CPU-bound: call them through
``asyncio.to_thread``. pyHanko's sync API starts its own event loop, which a
running loop forbids.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from PIL import Image
from pyhanko.pdf_utils.font import SimpleFontEngineFactory
from pyhanko.pdf_utils.images import PdfImage
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.layout import AxisAlignment, Margins, SimpleBoxLayoutRule
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.pdf_utils.text import TextBoxStyle
from pyhanko.sign import fields, signers
from pyhanko.sign.validation import (
    add_validation_info,
    validate_pdf_signature,
    validate_pdf_timestamp,
)
from pyhanko.sign.validation.dss import DocumentSecurityStore
from pyhanko.sign.validation.errors import NoDSSFoundError
from pyhanko.stamp import TextStampStyle

from crypto import appearance
from crypto.pdf_fields import FieldInfo, PreparationWriter, find_anchors, list_fields
from crypto.pki import SealMaterial
from domain.slots import DEFAULT_FIELD_SIZE, DEFAULT_SIGNATURE_SIZE, Slot
from shared.const import FieldType, SlotKind

logger = logging.getLogger(__name__)

CERTIFICATION_FIELD = "OptimCE-Certification"
EVIDENCE_SEAL_FIELD = "OptimCE-Evidence"
_TEXT_KINDS = {"/Tx"}
_CHECKBOX_KINDS = {"/Btn"}


class SlotNotFoundError(ValueError):
    """A slot names a field or an anchor the document does not have (permanent)."""


class SealValidationError(RuntimeError):
    """A revision we produced does not validate: never store it."""


@dataclass(frozen=True)
class FieldPlan:
    pdf_field_name: str
    type: FieldType
    slot: Slot


@dataclass(frozen=True)
class SignerPlan:
    field_name: str
    slot: Slot
    label: str
    fields: list[FieldPlan] = field(default_factory=list)


@dataclass(frozen=True)
class Placement:
    page: int  # 0-based
    rect: tuple[float, float, float, float]

    def as_json(self) -> dict[str, object]:
        return {"page": self.page, "rect": [round(v, 2) for v in self.rect]}


@dataclass(frozen=True)
class PreparedDocument:
    data: bytes
    placements: dict[str, Placement]
    flattened: int


@dataclass(frozen=True)
class SignatureCheck:
    field_name: str
    kind: str  # "signature" | "timestamp"
    intact: bool
    valid: bool
    trusted: bool
    modification: str
    ok: bool


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def prepare(
    original: bytes,
    signer_plans: Sequence[SignerPlan],
    material: SealMaterial,
    *,
    reason: str,
) -> PreparedDocument:
    existing = list_fields(original)
    anchors = (
        find_anchors(original)
        if any(
            plan.slot.kind is SlotKind.ANCHOR
            or any(f.slot.kind is SlotKind.ANCHOR for f in plan.fields)
            for plan in signer_plans
        )
        else {}
    )
    keep: set[str] = set()
    for plan in signer_plans:
        if plan.slot.kind is SlotKind.FIELD:
            _require_field(existing, str(plan.slot.target), {"/Sig"})
            keep.add(str(plan.slot.target))
        for spec in plan.fields:
            if spec.slot.kind is SlotKind.FIELD:
                kinds = _CHECKBOX_KINDS if spec.type is FieldType.CHECKBOX else _TEXT_KINDS
                _require_field(existing, str(spec.slot.target), kinds)
                keep.add(str(spec.slot.target))

    writer = PreparationWriter(original)
    flattened = writer.flatten_except(keep)

    page_signers = [p for p in signer_plans if p.slot.kind is SlotKind.PAGE]
    page_boxes = (
        writer.append_signature_page([p.label for p in page_signers]) if page_signers else []
    )
    if len(page_boxes) < len(page_signers):
        raise SlotNotFoundError("too many signers for one signature page")
    page_index = writer.page_count - 1

    placements: dict[str, Placement] = {}
    new_signature_fields: list[fields.SigFieldSpec] = []
    read_only: set[str] = set()
    for plan in signer_plans:
        own = [spec.pdf_field_name for spec in plan.fields]
        placement = _place(plan.slot, existing, anchors, writer, DEFAULT_SIGNATURE_SIZE)
        if plan.slot.kind is SlotKind.PAGE:
            placement = Placement(page_index, page_boxes[page_signers.index(plan)])
        placements[plan.field_name] = placement
        for spec in plan.fields:
            spot = _place(spec.slot, existing, anchors, writer, DEFAULT_FIELD_SIZE)
            placements[spec.pdf_field_name] = spot
            if spec.slot.kind is not SlotKind.FIELD:
                if spec.type is FieldType.CHECKBOX:
                    side = min(spot.rect[2] - spot.rect[0], spot.rect[3] - spot.rect[1], 14.0)
                    x1, y1 = spot.rect[0], spot.rect[1]
                    box = (x1, y1, x1 + side, y1 + side)
                    writer.add_checkbox(spot.page, box, spec.pdf_field_name)
                    placements[spec.pdf_field_name] = Placement(spot.page, box)
                else:
                    writer.add_text_field(spot.page, spot.rect, spec.pdf_field_name)
            if spec.type is FieldType.DATE:
                read_only.add(spec.pdf_field_name)
        if plan.slot.kind is SlotKind.FIELD:
            if own:
                writer.lock_signature_field(plan.field_name, own)
        else:
            new_signature_fields.append(
                fields.SigFieldSpec(
                    plan.field_name,
                    on_page=placement.page,
                    box=tuple(int(round(v)) for v in placement.rect),  # type: ignore[arg-type]
                    field_mdp_spec=(
                        fields.FieldMDPSpec(fields.FieldMDPAction.INCLUDE, fields=own)
                        if own
                        else None
                    ),
                )
            )
    writer.mark_read_only(read_only)
    staged = writer.write()

    pdf_writer = IncrementalPdfFileWriter(io.BytesIO(staged))
    for spec_field in new_signature_fields:
        fields.append_signature_field(pdf_writer, spec_field)
    meta = signers.PdfSignatureMetadata(
        field_name=CERTIFICATION_FIELD,
        certify=True,
        docmdp_permissions=fields.MDPPerm.FILL_FORMS,
        subfilter=fields.SigSeedSubFilter.PADES,
        md_algorithm="sha256",
        name="OptimCE",
        reason=reason,
        location="OptimCE",
    )
    out = signers.PdfSigner(
        meta,
        signer=material.signer,
        timestamper=material.timestamper(),
        new_field_spec=fields.SigFieldSpec(CERTIFICATION_FIELD),
    ).sign_pdf(pdf_writer)
    data: bytes = out.getvalue()
    check_revision(data, material)
    return PreparedDocument(data=data, placements=placements, flattened=flattened)


def _require_field(existing: dict[str, FieldInfo], name: str, kinds: set[str]) -> None:
    info = existing.get(name)
    if info is None:
        raise SlotNotFoundError(f"the document has no field {name!r}")
    if info.kind not in kinds:
        raise SlotNotFoundError(f"field {name!r} is a {info.kind}, expected {sorted(kinds)}")


def _place(
    slot: Slot,
    existing: dict[str, FieldInfo],
    anchors: dict[str, tuple[int, float, float]],
    writer: PreparationWriter,
    default_size: tuple[float, float],
) -> Placement:
    if slot.kind is SlotKind.FIELD:
        info = existing[str(slot.target)]
        return Placement(info.page, info.rect)
    if slot.kind is SlotKind.ANCHOR:
        found = anchors.get(str(slot.target))
        if found is None:
            raise SlotNotFoundError(f"the document does not print the anchor {slot.target}")
        page, x, y = found
        width = slot.width or default_size[0]
        height = slot.height or default_size[1]
        return Placement(page, (x, y - 4.0, x + width, y - 4.0 + height))
    if slot.kind is SlotKind.BOX:
        if slot.page is None or slot.rect is None:
            raise SlotNotFoundError("a box slot without its page and rectangle")
        if slot.page >= writer.page_count:
            raise SlotNotFoundError(f"the document has no page {slot.page + 1}")
        page_width, page_height = writer.page_size(slot.page)
        if slot.rect[2] > page_width + 1 or slot.rect[3] > page_height + 1:
            raise SlotNotFoundError("the box lies outside its page")
        return Placement(slot.page, slot.rect)
    # PAGE: placed by the caller, on the appended page.
    return Placement(0, (0.0, 0.0, 0.0, 0.0))


# ---------------------------------------------------------------------------
# sign
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldValue:
    pdf_field_name: str
    type: FieldType
    value: str


def sign(
    current: bytes,
    *,
    field_name: str,
    values: Sequence[FieldValue],
    image_png: bytes,
    stamp_lines: Sequence[str],
    signer_name: str,
    reason: str,
    material: SealMaterial,
) -> bytes:
    """Fill the signer's fields and seal their signature, in one increment."""
    writer = IncrementalPdfFileWriter(io.BytesIO(current))
    for item in values:
        if item.type is FieldType.CHECKBOX:
            appearance.set_checkbox(writer, item.pdf_field_name, item.value == "true")
        else:
            appearance.fill_text(writer, item.pdf_field_name, item.value)
    width = _field_width(current, field_name)
    image = Image.open(io.BytesIO(image_png))
    image.load()
    style = TextStampStyle(
        stamp_text="\n".join(f"%(line{i})s" for i in range(len(stamp_lines))),
        background=PdfImage(image),
        background_opacity=1.0,
        background_layout=SimpleBoxLayoutRule(
            x_align=AxisAlignment.ALIGN_MIN,
            y_align=AxisAlignment.ALIGN_MID,
            margins=Margins(left=2, right=int(width * 0.48), top=2, bottom=2),
        ),
        inner_content_layout=SimpleBoxLayoutRule(
            x_align=AxisAlignment.ALIGN_MAX,
            y_align=AxisAlignment.ALIGN_MID,
            margins=Margins(left=int(width * 0.53), right=2, top=1, bottom=1),
        ),
        text_box_style=TextBoxStyle(
            font=SimpleFontEngineFactory("Helvetica", 0.5), font_size=7, leading=8
        ),
        border_width=0,
    )
    meta = signers.PdfSignatureMetadata(
        field_name=field_name,
        subfilter=fields.SigSeedSubFilter.PADES,
        md_algorithm="sha256",
        name=signer_name,
        reason=reason,
        location="OptimCE",
    )
    out = signers.PdfSigner(
        meta, signer=material.signer, timestamper=material.timestamper(), stamp_style=style
    ).sign_pdf(
        writer,
        existing_fields_only=True,
        appearance_text_params={f"line{i}": line for i, line in enumerate(stamp_lines)},
    )
    data: bytes = out.getvalue()
    check_revision(data, material)
    return data


def _field_width(data: bytes, name: str) -> float:
    info = list_fields(data).get(name)
    if info is None:
        raise SlotNotFoundError(f"the document has no signature field {name!r}")
    return info.rect[2] - info.rect[0]


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


def complete(current: bytes, material: SealMaterial) -> bytes:
    """PAdES-B-LTA: every signature's validation data, then a document timestamp."""
    context = material.validation_context(allow_fetching=True)
    data = current
    count = len(PdfFileReader(io.BytesIO(data)).embedded_signatures)
    for index in range(count):
        signature = PdfFileReader(io.BytesIO(data)).embedded_signatures[index]
        data = add_validation_info(signature, context, output=io.BytesIO()).getvalue()
    out = signers.PdfTimeStamper(material.timestamper()).timestamp_pdf(
        IncrementalPdfFileWriter(io.BytesIO(data)),
        "sha256",
        validation_context=context,
        output=io.BytesIO(),
    )
    final: bytes = out.getvalue()
    check_revision(final, material, embedded_only=True)
    return final


def seal_evidence(pdf: bytes, material: SealMaterial, *, reason: str) -> bytes:
    """The evidence report, sealed PAdES-B-LT (validation data embedded)."""
    meta = signers.PdfSignatureMetadata(
        field_name=EVIDENCE_SEAL_FIELD,
        subfilter=fields.SigSeedSubFilter.PADES,
        md_algorithm="sha256",
        name="OptimCE",
        reason=reason,
        location="OptimCE",
        embed_validation_info=True,
        validation_context=material.validation_context(allow_fetching=True),
    )
    out = signers.PdfSigner(
        meta,
        signer=material.signer,
        timestamper=material.timestamper(),
        new_field_spec=fields.SigFieldSpec(EVIDENCE_SEAL_FIELD),
    ).sign_pdf(IncrementalPdfFileWriter(io.BytesIO(pdf)))
    data: bytes = out.getvalue()
    check_revision(data, material)
    return data


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def inspect(
    data: bytes, material: SealMaterial, *, embedded_only: bool = False
) -> list[SignatureCheck]:
    """Validate every signature and document timestamp of ``data``.

    ``embedded_only``: no network, revocation data from the file's DSS only
    (hard-fail). That is what proves a FINAL revision carries everything it
    needs to validate long after the TSA and the OCSP responder are gone.
    """
    reader = PdfFileReader(io.BytesIO(data))
    dss = None
    if embedded_only:
        try:
            dss = DocumentSecurityStore.read_dss(reader)
        except NoDSSFoundError:
            dss = None
    checks: list[SignatureCheck] = []
    for signature in reader.embedded_signatures:
        if dss is not None:
            context = dss.as_validation_context(
                {
                    "trust_roots": material.trust_roots,
                    "other_certs": material.other_certs,
                    "allow_fetching": False,
                    "revocation_mode": "hard-fail",
                }
            )
        else:
            context = material.validation_context(allow_fetching=not embedded_only)
        if signature.sig_object.get("/Type") == "/DocTimeStamp":
            ts = validate_pdf_timestamp(signature, validation_context=context)
            ok = bool(ts.valid and ts.intact and ts.trusted)
            checks.append(
                SignatureCheck(
                    signature.field_name,
                    "timestamp",
                    ts.intact,
                    ts.valid,
                    ts.trusted,
                    str(getattr(ts, "modification_level", "")),
                    ok,
                )
            )
            continue
        status = validate_pdf_signature(signature, context)
        checks.append(
            SignatureCheck(
                signature.field_name,
                "signature",
                status.intact,
                status.valid,
                status.trusted,
                str(status.modification_level),
                bool(status.bottom_line),
            )
        )
    return checks


def check_revision(data: bytes, material: SealMaterial, *, embedded_only: bool = False) -> None:
    failing = [c for c in inspect(data, material, embedded_only=embedded_only) if not c.ok]
    if failing:
        raise SealValidationError(
            "the revision does not validate: "
            + ", ".join(f"{c.field_name} ({c.modification})" for c in failing)
        )
