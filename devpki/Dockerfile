# The dev stack's test PKI (signature-test-pki). Never deployed to production.
FROM python:3.12-slim

RUN pip install --no-cache-dir "certomancer[web-api]==0.16.0"

WORKDIR /app
COPY devpki/ devpki/

ENV DEVPKI_DIR=/pki
EXPOSE 9000
CMD ["python", "-m", "devpki.serve"]
