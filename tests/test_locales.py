"""i18n smoke tests.

Every error key declared in shared/custom_errors.py must resolve to a
non-empty, non-key string in every supported locale. A regression here means a
user would see a raw ``ERRORS.SIGNATURE.*``-style key in the response body.
"""

import pytest

from core.i18n import SUPPORTED_LOCALES, translate
from shared.custom_errors import errors

# All translation keys declared in shared/custom_errors.py. Re-derived from the
# Error registry so adding a new error code surfaces here as a missing
# translation rather than a silent gap.
_ALL_KEYS = sorted(
    {
        e.key
        for domain in (errors.auth, errors.subscription, errors.signature)
        for e in domain.__class__.__dict__.values()
        if hasattr(e, "key")
    }
)


def test_supported_locales_includes_fr_en_de_nl():
    assert {"fr", "en", "de", "nl"} <= SUPPORTED_LOCALES


@pytest.mark.parametrize("locale", sorted(SUPPORTED_LOCALES))
@pytest.mark.parametrize("key", _ALL_KEYS)
def test_translate_returns_localized_string_for_every_supported_locale(locale: str, key: str):
    translated = translate(key, locale)
    assert (
        translated != key
    ), f"Key {key!r} is missing in locale {locale!r} (translate returned the bare key)"
    assert isinstance(translated, str)
    assert translated.strip(), f"Empty translation for {key!r} in {locale!r}"
