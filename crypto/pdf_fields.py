"""The preparation's pypdf stage: everything done BEFORE the first signature.

Nothing here touches a signed file. Preparation may rewrite the document
freely (the original stays in the bucket, unmodified, with its digest on the
request); once certified, only incremental updates follow (``appearance.py``,
``seal.py``).

Why the flattening (spike, 03/11/2026): a DocMDP 2 certification allows
filling ANY form field afterwards, so the values docgen pre-filled (the
declarant's name on annexe 8) would stay editable after certification and
the change would validate as "form filling". A FieldMDP lock on the
certification instead gave pyHanko false positives on the CWaPE form. So
every widget no signer fills is drawn into its page and removed: the
pre-filled data becomes certified content.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, cast

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    IndirectObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from domain.slots import normalise_rect

A4 = (595.276, 841.89)


def _dict(value: Any) -> DictionaryObject:
    """pypdf types every resolved object as ``PdfObject | None``; these are dictionaries."""
    return cast(DictionaryObject, value.get_object() if hasattr(value, "get_object") else value)


_HIDDEN = 2
_PRINT = 4
_NO_VIEW = 32
_FF_READ_ONLY = 1


@dataclass(frozen=True)
class FieldInfo:
    name: str
    kind: str  # "/Tx", "/Btn", "/Sig", ...
    page: int  # 0-based
    rect: tuple[float, float, float, float]


def widget_name(widget: DictionaryObject) -> str:
    """The fully qualified name of the field a widget belongs to."""
    parts: list[str] = []
    node: DictionaryObject | None = widget
    while node is not None:
        if "/T" in node:
            parts.append(str(node["/T"]))
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
    return ".".join(reversed(parts))


def _field_type(widget: DictionaryObject) -> str:
    node: DictionaryObject | None = widget
    while node is not None:
        if "/FT" in node:
            return str(node["/FT"])
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
    return ""


def _widgets(pages: Iterable[Any]) -> Iterator[tuple[int, DictionaryObject]]:
    for index, page in enumerate(pages):
        for ref in page.get("/Annots") or []:
            annot = ref.get_object()
            if annot.get("/Subtype") == "/Widget":
                yield index, annot


def list_fields(data: bytes) -> dict[str, FieldInfo]:
    """Every terminal field with a widget, by fully qualified name (first widget)."""
    reader = PdfReader(io.BytesIO(data))
    found: dict[str, FieldInfo] = {}
    for index, widget in _widgets(reader.pages):
        name = widget_name(widget)
        if name and name not in found:
            found[name] = FieldInfo(
                name, _field_type(widget), index, normalise_rect(widget["/Rect"])
            )
    return found


def find_anchors(data: bytes) -> dict[str, tuple[int, float, float]]:
    """Each ``[[kind:label]]`` tag printed in the document: (0-based page, x, y).

    The position is the start of the tag's text on its baseline, in PDF user
    space. A tag found twice keeps its first occurrence.
    """
    reader = PdfReader(io.BytesIO(data))
    found: dict[str, tuple[int, float, float]] = {}
    for index, page in enumerate(reader.pages):

        def visit(text: str, cm: Any, tm: Any, _font: Any, _size: Any, index: int = index) -> None:
            tag = text.strip()
            if tag.startswith("[[") and tag.endswith("]]") and tag not in found:
                x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
                y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
                found[tag] = (index, float(x), float(y))

        page.extract_text(visitor_text=visit)
    return found


class PreparationWriter:
    """One pypdf rewrite of the original: fields added, others flattened."""

    def __init__(self, original: bytes) -> None:
        self._writer = PdfWriter(clone_from=PdfReader(io.BytesIO(original)))
        root = self._writer._root_object
        if "/AcroForm" not in root:
            root[NameObject("/AcroForm")] = DictionaryObject({NameObject("/Fields"): ArrayObject()})
        self._acro = _dict(root["/AcroForm"])
        # Every widget must carry its own appearance: a signed file whose
        # viewer regenerates appearances shows "modified" warnings.
        self._acro[NameObject("/NeedAppearances")] = BooleanObject(False)
        self._helv = self._ensure_helvetica()

    @property
    def page_count(self) -> int:
        return len(self._writer.pages)

    def page_size(self, page: int) -> tuple[float, float]:
        box = self._writer.pages[page].mediabox
        return float(box.width), float(box.height)

    # ---- fonts ---------------------------------------------------------------
    def _ensure_helvetica(self) -> IndirectObject:
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
        ref = self._writer._add_object(font)
        resources = self._acro.get("/DR")
        if resources is None:
            resources = DictionaryObject()
            self._acro[NameObject("/DR")] = resources
        resources = _dict(resources)
        fonts = resources.get("/Font")
        if fonts is None:
            fonts = DictionaryObject()
            resources[NameObject("/Font")] = fonts
        _dict(fonts)[NameObject("/OptHelv")] = ref
        return ref

    # ---- flattening ------------------------------------------------------------
    def flatten_except(self, keep: set[str]) -> int:
        """Draw every other widget's normal appearance into its page; drop it and its field.

        Returns how many widgets were flattened.
        """
        dropped = 0
        for page in self._writer.pages:
            annots = page.get("/Annots")
            if not annots:
                continue
            kept = ArrayObject()
            drawing: list[str] = []
            resources = _dict(page["/Resources"])
            xobjects = resources.get("/XObject")
            if xobjects is None:
                xobjects = DictionaryObject()
                resources[NameObject("/XObject")] = xobjects
            xobjects = _dict(xobjects)
            for ref in annots:
                annot = ref.get_object()
                if annot.get("/Subtype") != "/Widget" or widget_name(annot) in keep:
                    kept.append(ref)
                    continue
                dropped += 1
                drawn = self._appearance_xobject(annot)
                if drawn is None:
                    continue
                normal, bbox = drawn
                x1, y1, x2, y2 = normalise_rect(annot["/Rect"])
                bx1, by1, bx2, by2 = bbox
                sx = (x2 - x1) / (bx2 - bx1) if bx2 != bx1 else 1.0
                sy = (y2 - y1) / (by2 - by1) if by2 != by1 else 1.0
                label = f"/OptFlat{len(xobjects)}"
                xobjects[NameObject(label)] = normal
                drawing.append(
                    f"q {sx:.5f} 0 0 {sy:.5f} {x1 - bx1 * sx:.4f} {y1 - by1 * sy:.4f} cm "
                    f"{label} Do Q"
                )
            page[NameObject("/Annots")] = kept
            if drawing:
                self._append_content(page, "\n".join(drawing))
        self._acro[NameObject("/Fields")] = self._prune(self._acro.get("/Fields") or [], keep)
        return dropped

    def _appearance_xobject(
        self, annot: DictionaryObject
    ) -> tuple[Any, tuple[float, float, float, float]] | None:
        flags = int(annot.get("/F", 0))
        if flags & (_HIDDEN | _NO_VIEW):
            return None
        appearance = annot.get("/AP")
        if appearance is None or "/N" not in appearance:
            return None
        raw = appearance.get_object().raw_get("/N")
        normal = raw.get_object()
        if "/BBox" not in normal:  # a dictionary of states: the one /AS selects
            state = annot.get("/AS")
            if state is None or state not in normal:
                return None
            raw = normal.raw_get(state)
            normal = raw.get_object()
        if not isinstance(raw, IndirectObject):
            raw = self._writer._add_object(normal)
        bbox = tuple(float(v) for v in normal["/BBox"])
        return raw, (bbox[0], bbox[1], bbox[2], bbox[3])

    def _prune(self, refs: Iterable[Any], keep: set[str]) -> ArrayObject:
        out = ArrayObject()
        for ref in refs:
            node = ref.get_object()
            kids = node.get("/Kids")
            if kids and any("/T" in kid.get_object() for kid in kids):
                remaining = self._prune(kids, keep)
                if remaining:
                    node[NameObject("/Kids")] = remaining
                    out.append(ref)
            elif widget_name(node) in keep or (
                kids and any(widget_name(kid.get_object()) in keep for kid in kids)
            ):
                out.append(ref)
        return out

    def _append_content(self, page: Any, content: str) -> None:
        stream = DecodedStreamObject()
        # Isolate the page's own content from ours: its graphics state may be
        # left changed at its end.
        stream.set_data(("Q\n" + content + "\n").encode("latin-1"))
        opening = DecodedStreamObject()
        opening.set_data(b"q\n")
        existing = page.raw_get("/Contents")
        current = existing.get_object() if existing is not None else None
        parts = list(current) if isinstance(current, ArrayObject) else [existing]
        page[NameObject("/Contents")] = ArrayObject(
            [
                self._writer._add_object(opening),
                *[p for p in parts if p is not None],
                self._writer._add_object(stream),
            ]
        )

    # ---- fields a signer fills -----------------------------------------------------
    def lock_signature_field(self, name: str, own_fields: list[str]) -> None:
        """Give an existing signature field a /Lock on the signer's own fields.

        Signing it then locks them (FieldMDP): a later change to « Fait à »
        breaks this signer's signature.
        """
        for _index, widget in _widgets(self._writer.pages):
            if widget_name(widget) != name:
                continue
            target = widget if "/FT" in widget else _dict(widget["/Parent"])
            target[NameObject("/Lock")] = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/SigFieldLock"),
                    NameObject("/Action"): NameObject("/Include"),
                    NameObject("/Fields"): ArrayObject(TextStringObject(n) for n in own_fields),
                }
            )
            return
        raise KeyError(name)

    def add_text_field(self, page: int, rect: tuple[float, float, float, float], name: str) -> None:
        self._add_widget(page, rect, name, NameObject("/Tx"), extra={})

    def add_checkbox(self, page: int, rect: tuple[float, float, float, float], name: str) -> None:
        x1, y1, x2, y2 = rect
        width, height = x2 - x1, y2 - y1
        on = DecodedStreamObject()
        on.set_data(
            (
                f"0 g 0.8 w 0.5 0.5 {width - 1:.2f} {height - 1:.2f} re S "
                f"2 2 m {width - 2:.2f} {height - 2:.2f} l S "
                f"2 {height - 2:.2f} m {width - 2:.2f} 2 l S"
            ).encode()
        )
        off = DecodedStreamObject()
        off.set_data(f"0 g 0.8 w 0.5 0.5 {width - 1:.2f} {height - 1:.2f} re S".encode())
        for stream in (on, off):
            stream.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Form"),
                    NameObject("/BBox"): ArrayObject(
                        [FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)]
                    ),
                }
            )
        appearance = DictionaryObject(
            {
                NameObject("/N"): DictionaryObject(
                    {
                        NameObject("/Yes"): self._writer._add_object(on),
                        NameObject("/Off"): self._writer._add_object(off),
                    }
                )
            }
        )
        self._add_widget(
            page,
            rect,
            name,
            NameObject("/Btn"),
            extra={
                NameObject("/V"): NameObject("/Off"),
                NameObject("/AS"): NameObject("/Off"),
                NameObject("/AP"): appearance,
            },
        )

    def _add_widget(
        self,
        page: int,
        rect: tuple[float, float, float, float],
        name: str,
        field_type: NameObject,
        *,
        extra: dict[NameObject, Any],
    ) -> None:
        page_obj = self._writer.pages[page]
        widget = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/FT"): field_type,
                NameObject("/T"): TextStringObject(name),
                NameObject("/Rect"): ArrayObject(FloatObject(v) for v in rect),
                NameObject("/F"): NumberObject(_PRINT),
                NameObject("/DA"): TextStringObject("/OptHelv 10 Tf 0 g"),
                NameObject("/P"): page_obj.indirect_reference,
                **extra,
            }
        )
        ref = self._writer._add_object(widget)
        annots = page_obj.get("/Annots")
        if annots is None:
            page_obj[NameObject("/Annots")] = ArrayObject([ref])
        else:
            annots.get_object().append(ref)
        fields = self._acro.get("/Fields")
        if fields is None:
            self._acro[NameObject("/Fields")] = ArrayObject([ref])
        else:
            fields.get_object().append(ref)

    # ---- the signature page ----------------------------------------------------------
    def append_signature_page(self, labels: list[str]) -> list[tuple[float, float, float, float]]:
        """Append an A4 page with one labelled box per signer; return the boxes (points).

        Pages can only be added now: after the certification, a new page is a
        modification DocMDP 2 forbids.
        """
        width, height = A4
        page = self._writer.add_blank_page(width=width, height=height)
        boxes: list[tuple[float, float, float, float]] = []
        lines = ["BT /OptHelv 14 Tf 56 780 Td (Signatures) Tj ET"]
        top = 740.0
        for label in labels:
            box = (56.0, top - 70.0, 300.0, top - 14.0)
            boxes.append(box)
            text = _pdf_text(label)
            lines.append(f"BT /OptHelv 10 Tf 56 {top:.1f} Td ({text}) Tj ET")
            lines.append(f"0.6 G 0.5 w 56 {top - 72:.1f} m 300 {top - 72:.1f} l S")
            top -= 100.0
            if top < 120:
                break
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject({NameObject("/OptHelv"): self._helv}),
            }
        )
        stream = DecodedStreamObject()
        stream.set_data("\n".join(lines).encode("latin-1"))
        page[NameObject("/Contents")] = self._writer._add_object(stream)
        return boxes

    def mark_read_only(self, names: set[str]) -> None:
        """Viewers must not let anyone type into a field the server fills (DATE)."""
        for _index, widget in _widgets(self._writer.pages):
            if widget_name(widget) in names:
                target = widget if "/FT" in widget else _dict(widget["/Parent"])
                flags = int(target.get("/Ff", 0))
                target[NameObject("/Ff")] = NumberObject(flags | _FF_READ_ONLY)

    def write(self) -> bytes:
        buffer = io.BytesIO()
        self._writer.write(buffer)
        return buffer.getvalue()


def _pdf_text(value: str) -> str:
    raw = value.encode("cp1252", errors="replace").decode("latin-1")
    return raw.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
