"""The platform seal, the timestamp authorities, the validation context.

The seal is one certificate and key, loaded from a PKCS#12 file mounted as a
secret (``SEAL_PKCS12_PATH``), in the seal worker only. Rotating it is a
config change: each signature embeds the certificate that made it, so old
revisions stay valid.

pyHanko's synchronous API runs its own event loop (``asyncio.run``), so every
function here and in the sibling modules is called from a worker thread
(``asyncio.to_thread``), never from the worker's own loop.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from asn1crypto import pem, tsp, x509
from pyhanko.sign import signers
from pyhanko.sign.timestamps import HTTPTimeStamper
from pyhanko.sign.timestamps.api import TimeStamper
from pyhanko_certvalidator import ValidationContext

logger = logging.getLogger(__name__)


class SealConfigurationError(RuntimeError):
    """The seal or the TSA list is missing or unreadable: nothing can be sealed."""


def load_certificates(paths: Sequence[str]) -> list[x509.Certificate]:
    """Read PEM or DER certificates (one or several per PEM file)."""
    certificates: list[x509.Certificate] = []
    for raw_path in paths:
        path = Path(raw_path)
        data = path.read_bytes()
        if pem.detect(data):
            for _type, _headers, der in pem.unarmor(data, multiple=True):
                certificates.append(x509.Certificate.load(der))
        else:
            certificates.append(x509.Certificate.load(data))
    return certificates


def split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


class FallbackTimeStamper(TimeStamper):
    """Ask each RFC 3161 authority in order; the first answer wins.

    A production TSA can be down for maintenance; an ordered list keeps
    sealing going. Every authority must be trusted by the validation context,
    or the signatures it stamps would not validate.
    """

    def __init__(self, urls: Sequence[str], *, timeout: int) -> None:
        if not urls:
            raise SealConfigurationError("no timestamp authority configured (TSA_URLS)")
        super().__init__()
        self._stampers = [HTTPTimeStamper(url, timeout=timeout) for url in urls]
        self._urls = list(urls)

    async def async_request_tsa_response(self, req: tsp.TimeStampReq) -> tsp.TimeStampResp:
        last_error: Exception | None = None
        for url, stamper in zip(self._urls, self._stampers, strict=True):
            try:
                return await stamper.async_request_tsa_response(req)
            except Exception as exc:  # any transport or protocol failure: next one
                logger.warning("timestamp authority %s failed: %s", url, exc)
                last_error = exc
        raise OSError(f"every timestamp authority failed: {last_error}") from last_error


@dataclass
class SealMaterial:
    signer: signers.SimpleSigner
    trust_roots: list[x509.Certificate]
    tsa_urls: list[str]
    tsa_timeout: int
    other_certs: list[x509.Certificate] = field(default_factory=list)

    def timestamper(self) -> TimeStamper:
        return FallbackTimeStamper(self.tsa_urls, timeout=self.tsa_timeout)

    def validation_context(self, *, allow_fetching: bool = True) -> ValidationContext:
        """Hard-fail revocation checking against our trust roots.

        Used to gather the validation data at completion (fetching on) and to
        check every revision before it is stored.
        """
        return ValidationContext(
            trust_roots=self.trust_roots,
            other_certs=self.other_certs,
            allow_fetching=allow_fetching,
            revocation_mode="hard-fail",
        )


def load_seal_material(
    *,
    pkcs12_path: str,
    passphrase: str,
    ca_chain_paths: str,
    trust_root_paths: str,
    tsa_urls: str,
    tsa_timeout: int,
) -> SealMaterial:
    if not pkcs12_path:
        raise SealConfigurationError("SEAL_PKCS12_PATH is not set")
    if not Path(pkcs12_path).is_file():
        raise SealConfigurationError(f"seal PKCS#12 not found at {pkcs12_path}")
    chain = split_csv(ca_chain_paths)
    signer = signers.SimpleSigner.load_pkcs12(
        pkcs12_path,
        ca_chain_files=chain or None,
        passphrase=passphrase.encode() if passphrase else None,
    )
    if signer is None:
        raise SealConfigurationError("the seal PKCS#12 could not be read (passphrase?)")
    roots = load_certificates(split_csv(trust_root_paths))
    if not roots:
        raise SealConfigurationError("SEAL_TRUST_ROOT_PATHS names no certificate")
    urls = split_csv(tsa_urls)
    if not urls:
        raise SealConfigurationError("TSA_URLS names no timestamp authority")
    return SealMaterial(
        signer=signer,
        trust_roots=roots,
        tsa_urls=urls,
        tsa_timeout=tsa_timeout,
        other_certs=load_certificates(chain),
    )
