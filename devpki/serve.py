"""The dev stack's test PKI (``signature-test-pki``): ``python -m devpki.serve``.

Writes ``seal.p12`` and ``root.cer`` into ``DEVPKI_DIR`` (a volume the seal
worker mounts read-only) and serves the TSA, OCSP and CRLs on port 9000. The
certificates name ``DEVPKI_BASE_URL`` (default ``http://signature-test-pki:9000``)
as where their revocation data lives, so the worker reaches it by that name.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path

from devpki import build, serve_in_thread

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("devpki")


def main() -> None:
    directory = Path(os.getenv("DEVPKI_DIR", "/pki"))
    base_url = os.getenv("DEVPKI_BASE_URL", "http://signature-test-pki:9000")
    pki = build(directory, base_url)
    server = serve_in_thread(pki, "0.0.0.0", 9000)  # noqa: S104  # dev-only container
    logger.info("test PKI ready: TSA %s, seal %s", pki.tsa_url, pki.seal_p12)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    server.shutdown()


if __name__ == "__main__":
    main()
