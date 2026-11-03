"""What other repositories must know: the realtime topics, the notification types, the error codes.

An unregistered realtime topic is silently dropped (``build_envelope`` returns
None), and an unknown notification type renders as a raw i18n key: neither
fails anywhere. These scans notice it here.
"""

from __future__ import annotations

import ast
import pathlib
import re

from core.notifications import NotificationTypes
from core.realtime.envelope import TOPICS
from service import notify
from shared.custom_errors import errors

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SKIP = {".venv", "venv", "tests", "__pycache__", ".mypy_cache"}


def _sources() -> list[pathlib.Path]:
    return [p for p in _ROOT.rglob("*.py") if not _SKIP & set(p.relative_to(_ROOT).parts)]


def test_every_realtime_topic_is_registered():
    used = {notify.TOPIC_SIGNED, notify.TOPIC_REQUEST_UPDATED}
    assert used <= TOPICS
    assert {"signature.signed", "signature.request.updated"} == used


def test_every_notification_type_published_is_declared():
    declared = {v for k, v in vars(NotificationTypes).items() if k.isupper()}
    used: set[str] = set()
    for path in _sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "NotificationTypes"
            ):
                used.add(getattr(NotificationTypes, node.attr))
    assert (
        used
        == declared
        == {
            "signature.requested",
            "signature.reminder",
            "signature.signed",
            "signature.declined",
            "signature.completed",
        }
    )


def test_the_error_codes_stay_in_the_signature_block_and_are_unique():
    codes = [e.code for e in vars(type(errors.signature)).values() if hasattr(e, "code")]
    assert len(codes) == len(set(codes))
    assert all(2500 <= code <= 2599 for code in codes)


def test_every_message_ends_with_its_code():
    import json

    for locale in ("fr", "en", "nl", "de"):
        data = json.loads((_ROOT / "locales" / f"{locale}.json").read_text(encoding="utf-8"))
        for name, error in vars(type(errors.signature)).items():
            if not hasattr(error, "code"):
                continue
            message = data["ERRORS"]["SIGNATURE"][name]
            assert re.search(rf"\(code: {error.code}\)$", message), (locale, name)
