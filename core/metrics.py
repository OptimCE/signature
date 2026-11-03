"""Application metrics for the signature service.

All instruments are created at module import. The OTel API ships a
``_ProxyMeterProvider`` until ``metrics.set_meter_provider(...)`` runs
(inside ``core.tracing.setup_tracer_provider``), and instruments created
against the proxy rebind to the real provider when it is installed —
so import order between this module and tracing setup does not matter.

In LOCAL ``setup_tracer_provider`` returns early and the proxy stays a
no-op, meaning every ``.add(...)``/``.record(...)`` call below becomes
a cheap function dispatch with no side effects. Tests that want to
observe values use ``InMemoryMetricReader`` + a fresh ``MeterProvider``
and re-fetch instruments from that provider.

Naming follows OTel semantic conventions: dotted lowercase, ``.total``
suffix on monotonically increasing counters, units in seconds for
duration histograms. The backend translates these to
``signature_requests_total`` etc.
"""

from __future__ import annotations

from opentelemetry import metrics

_meter = metrics.get_meter("signature")

requests_created = _meter.create_counter(
    name="signature.requests.total",
    description="Signature requests handled, labelled by source and outcome (accepted/failed)",
    unit="1",
)

seal_jobs = _meter.create_counter(
    name="signature.seal.jobs.total",
    description="Seal jobs run, labelled by job (prepare/sign/complete) and outcome",
    unit="1",
)

seal_duration = _meter.create_histogram(
    name="signature.seal.duration",
    description="Time spent in one pyHanko operation, labelled by job",
    unit="s",
)

signer_actions = _meter.create_counter(
    name="signature.signer.actions.total",
    description="Signer calls accepted, labelled by action (view/sign/decline)",
    unit="1",
)

worker_messages = _meter.create_counter(
    name="worker.messages.total",
    description="NATS message handler outcomes, labelled by consumer and outcome",
    unit="1",
)

health_checks = _meter.create_counter(
    name="health.checks.total",
    description="Readiness probe component outcomes",
    unit="1",
)
