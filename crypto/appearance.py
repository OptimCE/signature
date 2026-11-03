"""Filling a signer's form fields inside the increment that signs them.

pyHanko's incremental writer: the new value (/V, /AS) and the widget's
appearance stream go into the same update as the signature, so the signature
covers them, and the signer's /Lock (set at preparation) makes any later
change to them break it.

Appearances are drawn with our own Helvetica (WinAnsi), never with the
form's font: a form's subset font may lack the glyphs of « Liège » or have a
custom encoding (WeasyPrint's do), and the value would then not show.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pyhanko.pdf_utils import generic
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter

from domain.slots import normalise_rect

_FONT_NAME = "/OptHelv"


def _walk(refs: Any, prefix: str = "") -> Iterator[tuple[str, Any, list[Any]]]:
    for ref in refs:
        node = ref.get_object()
        partial = node.get("/T")
        name = f"{prefix}.{partial}" if prefix and partial is not None else str(partial or prefix)
        kids = node.get("/Kids")
        if kids and any("/T" in kid.get_object() for kid in kids):
            yield from _walk(kids, name)
        else:
            yield name, ref, list(kids) if kids else [ref]


def find_field(writer: IncrementalPdfFileWriter, name: str) -> tuple[Any, list[Any]]:
    """(field reference, widget references) of a field, by fully qualified name."""
    for full_name, ref, widgets in _walk(writer.root["/AcroForm"]["/Fields"]):
        if full_name == name:
            return ref, widgets
    raise KeyError(name)


def _helvetica(writer: IncrementalPdfFileWriter) -> Any:
    return writer.add_object(
        generic.DictionaryObject(
            {
                generic.pdf_name("/Type"): generic.pdf_name("/Font"),
                generic.pdf_name("/Subtype"): generic.pdf_name("/Type1"),
                generic.pdf_name("/BaseFont"): generic.pdf_name("/Helvetica"),
                generic.pdf_name("/Encoding"): generic.pdf_name("/WinAnsiEncoding"),
            }
        )
    )


def _escape(value: str) -> bytes:
    raw = value.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def fill_text(writer: IncrementalPdfFileWriter, name: str, value: str) -> None:
    """Set a text field's value and draw it, single line, left-aligned, fitted."""
    ref, widgets = find_field(writer, name)
    field = ref.get_object()
    field["/V"] = generic.TextStringObject(value)
    writer.mark_update(ref)
    font_ref = _helvetica(writer)
    for widget_ref in widgets:
        widget = widget_ref.get_object()
        x1, y1, x2, y2 = normalise_rect(widget["/Rect"])
        width, height = x2 - x1, y2 - y1
        size = max(5.0, min(11.0, height - 4.0))
        # Helvetica averages ~0.5 em per character; shrink a long value to fit.
        if value:
            size = max(5.0, min(size, (width - 4.0) / (0.52 * len(value))))
        baseline = max(1.0, (height - size) / 2.0 + size * 0.22)
        content = (
            b"/Tx BMC q BT "
            + _FONT_NAME.encode()
            + f" {size:.2f} Tf 0 g 2 {baseline:.2f} Td (".encode()
            + _escape(value)
            + b") Tj ET Q EMC"
        )
        stream = generic.StreamObject(
            {
                generic.pdf_name("/Type"): generic.pdf_name("/XObject"),
                generic.pdf_name("/Subtype"): generic.pdf_name("/Form"),
                generic.pdf_name("/BBox"): generic.ArrayObject(
                    [
                        generic.FloatObject(0),
                        generic.FloatObject(0),
                        generic.FloatObject(width),
                        generic.FloatObject(height),
                    ]
                ),
                generic.pdf_name("/Resources"): generic.DictionaryObject(
                    {
                        generic.pdf_name("/Font"): generic.DictionaryObject(
                            {generic.pdf_name(_FONT_NAME): font_ref}
                        )
                    }
                ),
            },
            stream_data=content,
        )
        widget["/AP"] = generic.DictionaryObject(
            {generic.pdf_name("/N"): writer.add_object(stream)}
        )
        writer.mark_update(widget_ref)


def set_checkbox(writer: IncrementalPdfFileWriter, name: str, checked: bool) -> None:
    """Select a checkbox's own "on" state (whatever the form calls it) or /Off."""
    ref, widgets = find_field(writer, name)
    field = ref.get_object()
    on_state = None
    for widget_ref in widgets:
        normal = widget_ref.get_object().get("/AP", {}).get("/N")
        if normal is not None:
            states = [key for key in normal.get_object() if key != "/Off"]
            if states:
                on_state = states[0]
                break
    state = generic.pdf_name(on_state or "/Yes") if checked else generic.pdf_name("/Off")
    field["/V"] = state
    writer.mark_update(ref)
    for widget_ref in widgets:
        widget_ref.get_object()["/AS"] = state
        writer.mark_update(widget_ref)
