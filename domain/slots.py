"""Where a signer's signature (or one of their fields) goes: pure.

A slot is a one-key JSON object, in this order of preference:

- ``{"field": "Champ de signature 1"}`` — a field the PDF already has, by its
  fully qualified name (the regulators' own fillable forms);
- ``{"anchor": "[[sig:member]]", "width": 200, "height": 50}`` — an invisible
  tag the docgen template prints; the box is drawn at the tag (size optional);
- ``{"box": {"page": 1, "rect": [x1, y1, x2, y2]}}`` — explicit, 1-based page,
  PDF points from the bottom-left corner;
- ``{"page": {}}`` — on a signature page appended at preparation (signatures
  only; pages can be added before the certification, never after).

The resolved placement (``{"page": n, "rect": [...]}``, 0-based page) is
stored by preparation so the front can draw the slot over the page.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from shared.const import SlotKind

ANCHOR_PATTERN = re.compile(r"^\[\[[a-z]{1,16}:[A-Za-z0-9_.-]{1,48}\]\]$")
DEFAULT_SIGNATURE_SIZE = (200.0, 50.0)
DEFAULT_FIELD_SIZE = (150.0, 16.0)
# Max PDF user-space coordinate we accept in a box (A0 is 3370 points tall).
_MAX_COORD = 14400.0


class SlotError(ValueError):
    """The slot is malformed; the request is refused (permanent)."""


@dataclass(frozen=True)
class Slot:
    kind: SlotKind
    # field: the PDF field name; anchor: the tag.
    target: str | None = None
    page: int | None = None  # box: 0-based
    rect: tuple[float, float, float, float] | None = None
    width: float | None = None
    height: float | None = None


def parse_slot(raw: Any, *, for_signature: bool) -> Slot:
    if not isinstance(raw, Mapping) or len(raw) == 0:
        raise SlotError("a slot is a non-empty object")
    kinds = [key for key in raw if key in {k.value for k in SlotKind}]
    if len(kinds) != 1:
        raise SlotError("a slot names exactly one of field, anchor, box, page")
    kind = SlotKind(kinds[0])
    value = raw[kind.value]
    if kind is SlotKind.FIELD:
        if not isinstance(value, str) or not value.strip() or len(value) > 255:
            raise SlotError("field: the PDF field's name")
        return Slot(kind, target=value)
    if kind is SlotKind.ANCHOR:
        if not isinstance(value, str) or not ANCHOR_PATTERN.match(value):
            raise SlotError("anchor: a tag such as [[sig:member]]")
        width, height = _size(raw)
        return Slot(kind, target=value, width=width, height=height)
    if kind is SlotKind.BOX:
        if not isinstance(value, Mapping):
            raise SlotError("box: {page, rect}")
        page = value.get("page")
        rect = value.get("rect")
        if not isinstance(page, int) or page < 1:
            raise SlotError("box.page: 1-based page number")
        if (
            not isinstance(rect, list | tuple)
            or len(rect) != 4
            or not all(isinstance(v, int | float) for v in rect)
        ):
            raise SlotError("box.rect: [x1, y1, x2, y2]")
        x1, y1, x2, y2 = (float(v) for v in rect)
        if not (0 <= x1 < x2 <= _MAX_COORD and 0 <= y1 < y2 <= _MAX_COORD):
            raise SlotError("box.rect: x1 < x2 and y1 < y2, in points")
        return Slot(kind, page=page - 1, rect=(x1, y1, x2, y2))
    if not for_signature:
        raise SlotError("page: only a signature can go on an appended page")
    if not isinstance(value, Mapping):
        raise SlotError("page: {}")
    return Slot(kind)


def _size(raw: Mapping[str, Any]) -> tuple[float | None, float | None]:
    sizes: list[float | None] = []
    for name in ("width", "height"):
        value = raw.get(name)
        if value is None:
            sizes.append(None)
            continue
        if not isinstance(value, int | float) or not 8 <= float(value) <= 600:
            raise SlotError(f"anchor {name}: 8 to 600 points")
        sizes.append(float(value))
    return sizes[0], sizes[1]


def normalise_rect(rect: Any) -> tuple[float, float, float, float]:
    """A PDF /Rect as (x1, y1, x2, y2) with x1 < x2 and y1 < y2.

    Writers may store the corners in any order (WeasyPrint's forms put y2
    below y1); an appearance drawn from an unnormalised rectangle lands
    outside its box and shows nothing.
    """
    x1, y1, x2, y2 = (float(v) for v in rect)
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)
