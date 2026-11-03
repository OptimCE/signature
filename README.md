# OptimCE — signature

Electronic signature of platform-generated PDFs, inside the app.

- A domain (administrative documents today) asks over NATS
  (`signature.request.create`) for a document it generated to be signed by
  one or more members or admins, in parallel or in order.
- The signer signs from « À signer »: typed or drawn, with the consent
  statement and the fields the domain declared, after a fresh login.
- Every revision is sealed with the platform's certificate (PAdES-B-T); the
  final file is PAdES-B-LTA, and an evidence report (PDF + JSON, sealed)
  records every event of a hash-chained, append-only log.
- The domain hears back on `signature.event.<source>.<type>` (`accepted`,
  `prepared`, `signer_signed`, `completed`, `declined`, `cancelled`, `expired`,
  `failed`).

Legal level: a **simple electronic signature** (eIDAS). The cryptographic
signer is the platform; each person's identity is proven by their
authentication and the evidence log. See the spec's « Legal level and limits ».

## Run

```bash
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements/all.txt
ENV=test DOCKER_CONTEXT=desktop-linux .venv/Scripts/python.exe -m pytest -q
```

In the dev monorepo: `signature` (API, port 8009), `signature-worker`,
`signature-test-pki` (a disposable test CA, TSA, OCSP and CRL: `devpki/`).

## Configuration

See `.env.exemple` and `core/config.py`. Production needs a seal certificate
trusted by PDF readers (`SEAL_PKCS12_PATH`, mounted for the worker only) and
a timestamp authority on an EU trusted list (`TSA_URLS`).

Licence: Apache 2.0 (`LICENSE`). pyHanko and pyhanko-certvalidator are MIT,
pypdf BSD, Pillow HPND, Certomancer MIT (tests and dev only).
