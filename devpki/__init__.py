"""A disposable test PKI: a root CA, the seal, a timestamp authority (Certomancer).

Used by the test suite (in-process, on 127.0.0.1) and by the dev stack's
``signature-test-pki`` container (``python -m devpki.serve``), which serves the
TSA, OCSP and CRLs over HTTP and writes the seal's PKCS#12 and the root
certificate for the worker. Never shipped: the production images do not copy
this package, and production uses a real seal and a qualified TSA.

The keys are generated on first use (ECDSA P-256) into the given directory and
reused afterwards, so certificates stay stable across restarts. Nothing here
is secret, and nothing here is committed.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ARCH = "optimce-dev"
SEAL_PASSPHRASE = "dev-only"  # noqa: S105  # the throwaway test PKI's, by design

_CONFIG = """
external-url-prefix: "{base_url}"
keysets:
  {arch}:
    path-prefix: keys
    keys:
      root: {{path: root.key.pem}}
      seal: {{path: seal.key.pem}}
      tsa: {{path: tsa.key.pem}}
pki-architectures:
  {arch}:
    keyset: {arch}
    entity-defaults:
      country-name: BE
      organization-name: OptimCE (test)
    entities:
      root: {{common-name: OptimCE Test Root CA}}
      seal: {{common-name: OptimCE Test Seal}}
      tsa: {{common-name: OptimCE Test TSA}}
    certs:
      root:
        subject: root
        issuer: root
        validity: {{valid-from: "2020-01-01T00:00:00+0000", valid-to: "2040-01-01T00:00:00+0000"}}
        extensions:
          - id: basic_constraints
            critical: true
            value: {{ca: true}}
          - id: key_usage
            critical: true
            smart-value: {{schema: key-usage, params: [digital_signature, key_cert_sign, crl_sign]}}
      seal:
        subject: seal
        issuer: root
        validity: {{valid-from: "2020-01-01T00:00:00+0000", valid-to: "2039-01-01T00:00:00+0000"}}
        extensions:
          - id: key_usage
            critical: true
            smart-value: {{schema: key-usage, params: [digital_signature, non_repudiation]}}
          - id: crl_distribution_points
            smart-value: {{schema: crl-dist-url, params: {{crl-repo-names: [root]}}}}
          - id: authority_information_access
            smart-value: {{schema: aia-urls, params: {{ocsp-responder-names: [root]}}}}
      tsa:
        subject: tsa
        issuer: root
        validity: {{valid-from: "2020-01-01T00:00:00+0000", valid-to: "2039-01-01T00:00:00+0000"}}
        extensions:
          - id: key_usage
            critical: true
            smart-value: {{schema: key-usage, params: [digital_signature]}}
          - id: extended_key_usage
            critical: true
            value: [time_stamping]
          - id: crl_distribution_points
            smart-value: {{schema: crl-dist-url, params: {{crl-repo-names: [root]}}}}
    services:
      ocsp:
        root:
          for-issuer: root
          responder-cert: root
          signing-key: root
      crl-repo:
        root:
          for-issuer: root
          signing-key: root
          simulated-update-schedule: "P30D"
      time-stamping:
        tsa:
          signing-key: tsa
          signing-cert: tsa
"""


@dataclass(frozen=True)
class TestPki:
    base_url: str
    seal_p12: Path
    root_cert: Path
    config: Any

    @property
    def tsa_url(self) -> str:
        return f"{self.base_url}/{ARCH}/tsa/tsa"


def build(directory: Path, base_url: str) -> TestPki:
    """Generate (once) the keys, then the certificates for ``base_url``."""
    from certomancer.registry import ArchLabel, CertLabel, CertomancerConfig
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    keys = directory / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    for name in ("root", "seal", "tsa"):
        path = keys / f"{name}.key.pem"
        if not path.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            path.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
    config_path = directory / "certomancer.yml"
    config_path.write_text(_CONFIG.format(base_url=base_url, arch=ARCH), encoding="utf-8")
    config = CertomancerConfig.from_file(str(config_path), key_search_dir=str(directory))
    arch = config.get_pki_arch(ArchLabel(ARCH))
    seal_p12 = directory / "seal.p12"
    seal_p12.write_bytes(arch.package_pkcs12(CertLabel("seal"), password=SEAL_PASSPHRASE.encode()))
    root_cert = directory / "root.cer"
    root_cert.write_bytes(arch.get_cert(CertLabel("root")).dump())
    return TestPki(base_url=base_url, seal_p12=seal_p12, root_cert=root_cert, config=config)


def serve_in_thread(pki: TestPki, host: str, port: int) -> Any:
    """Serve the TSA, OCSP and CRLs; returns the server (call ``shutdown()``)."""
    from certomancer.integrations.animator import Animator, AnimatorArchStore
    from werkzeug.serving import make_server

    app = Animator(AnimatorArchStore(pki.config.pki_archs), with_web_ui=False)
    server = make_server(host, port, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True, name="devpki").start()
    return server
