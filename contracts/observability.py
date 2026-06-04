"""OpenTelemetry tracing surface (Day 30, Phase 6).

The observability story for PennyCore is intentionally tiny:

  * **One module here**, imported by every span site in the system.
  * **API-only by default**: with no exporter configured, every
    `trace_span` is a no-op (the OTel API gives back a NoOp span when
    no SDK tracer provider is registered).
  * **One env var flips on real export**: `OTEL_EXPORTER_OTLP_ENDPOINT`
    set → `setup_tracing()` configures an OTLP HTTP exporter pointing
    at that endpoint. Used in `docker-compose.otel.yml` to route into
    the collector → Jaeger.
  * **Graceful absence of the OTel SDK**: if `opentelemetry-api` is not
    installed at all, the module still imports successfully and every
    call is a no-op. Production images ship the API; the SDK is
    installed only when the OTel compose overlay is active.

## Semantic conventions

Attribute names follow OpenTelemetry semantic conventions where
relevant (`http.*`, `service.*`, `error.type`) and use the
`pennycore.*` namespace for project-specific dims. The project-specific
attributes — the ones the Phase 5 wrap flagged as load-bearing for the
demo — are:

  * `pennycore.tenant_id`      — every span. Tenant isolation invariant
                                  (rule 15) made readable in the trace
                                  viewer at-a-glance.
  * `pennycore.event_id`       — ingestion + decision spans.
  * `pennycore.action_id`      — decision + executor spans.
  * `pennycore.action_type`    — decision + executor spans.
  * `pennycore.decision`       — policy decision (`auto` /
                                  `approval_required` / `reject`).
  * `pennycore.brief.strategy` — `recency` | `semantic` | `summarized`
                                  | `hybrid` — Phase-5 champion
                                  visible in the trace.
  * `pennycore.brief.tokens`   — the budgeted brief's measured size.
  * `pennycore.cache.hit`      — Phase-5 semantic cache hit/miss.
  * `pennycore.cache.scope`    — `per_customer` (champion, default).

`trace_span` is the only public entry point — everything else inside
this module is glue.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional OTel SDK import. The API alone is enough for "no-op when not
# configured" — the SDK is needed only for export to a collector.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - exercised by both branches via the tests
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import Status, StatusCode

    _OTEL_API_AVAILABLE = True
except ImportError:
    _otel_trace = None  # type: ignore[assignment]
    Status = None  # type: ignore[assignment]
    StatusCode = None  # type: ignore[assignment]
    _OTEL_API_AVAILABLE = False


# Module-level marker the test surface introspects to assert the
# active mode. None = no setup called; "noop" = setup ran without an
# endpoint; "otlp" = setup ran with an OTLP endpoint configured.
_active_mode: str | None = None


def _service_name_from_env(default: str) -> str:
    """Resolve the OTel service name. `OTEL_SERVICE_NAME` (the spec-blessed
    env var) wins; otherwise the caller's default applies."""
    return os.getenv("OTEL_SERVICE_NAME", "").strip() or default


def setup_tracing(service_name: str) -> str:
    """Configure tracing for the calling service.

    Returns the active mode label:

      * ``"otlp"``  — `OTEL_EXPORTER_OTLP_ENDPOINT` is set; spans flow
        through a BatchSpanProcessor → OTLP HTTP exporter to that
        endpoint (the Day 30 compose overlay points it at the
        collector).
      * ``"noop"`` — no endpoint configured; spans are no-ops. This is
        the default in dev and in CI.
      * ``"unavailable"`` — `opentelemetry-api` isn't installed.
        Spans are no-ops; the module surface is still safe to import.

    Safe to call more than once: re-running with the same env vars is a
    no-op past the first call (idempotent installation of the global
    tracer provider).
    """
    global _active_mode

    if not _OTEL_API_AVAILABLE:
        _active_mode = "unavailable"
        return _active_mode

    if _active_mode is not None:
        return _active_mode

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    resolved_service = _service_name_from_env(service_name)

    if not endpoint:
        _active_mode = "noop"
        return _active_mode

    # Lazy-import the SDK only when an endpoint is actually configured.
    # Keeps the import-time cost zero for the dev path.
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except ImportError as exc:
        # SDK missing — fall back to no-op rather than crashing the boot.
        # Production images install the SDK alongside the API; dev does not.
        _LOG.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT=%s but opentelemetry SDK is not "
            "installed (%s); spans will be no-ops.",
            endpoint,
            exc,
        )
        _active_mode = "noop"
        return _active_mode

    resource = Resource.create(
        {
            "service.name": resolved_service,
            "service.namespace": "pennycore",
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    )
    _otel_trace.set_tracer_provider(provider)
    _active_mode = "otlp"
    _LOG.info(
        "OpenTelemetry tracing configured: service=%s endpoint=%s",
        resolved_service,
        endpoint,
    )
    return _active_mode


def get_tracing_mode() -> str | None:
    """Return the cached `setup_tracing` mode, or ``None`` if setup has
    not been called yet. Used by the `/info` endpoints to surface the
    observability posture, and by tests."""
    return _active_mode


def _reset_for_tests() -> None:
    """Drop the module-level mode cache.

    Used by the OTel unit tests so each test can re-resolve
    `setup_tracing`'s mode without a stale value. We deliberately do
    NOT reset OpenTelemetry's global TracerProvider — OTel intentionally
    forbids overriding it once set, and most tests rely on a shared
    `InMemorySpanExporter` installed via a module-level fixture.
    Per-test isolation comes from calling `exporter.clear()` between
    tests, not from re-installing the provider. NOT for production
    callers.
    """
    global _active_mode
    _active_mode = None


@contextmanager
def trace_span(
    name: str,
    *,
    tenant_id: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Open a span for `name`, yield a handle, close on exit.

    `tenant_id` is special-cased because every span in the system is
    expected to carry it (rule 15) — passing it as a kwarg avoids the
    "I forgot to set it again" footgun. The handle yielded supports
    `.set_attribute(...)` so callers can attach span dims discovered
    mid-block (e.g. the brief's measured token count, the policy
    decision).

    When the OTel API is unavailable, yields a tiny stub that
    silently swallows `.set_attribute` calls — same surface, no
    branches at the call site.

    On exception inside the block, the span is marked ERROR with the
    exception class name as `error.type` (OTel semantic convention).
    The exception is re-raised — this is observability, not error
    handling.
    """
    if not _OTEL_API_AVAILABLE:
        yield _NoopSpan()
        return

    tracer = _otel_trace.get_tracer("pennycore")
    span_attrs: dict[str, Any] = {}
    if tenant_id is not None:
        span_attrs["pennycore.tenant_id"] = tenant_id
    if attributes:
        for key, value in attributes.items():
            if value is None:
                continue
            span_attrs[key] = value

    with tracer.start_as_current_span(name, attributes=span_attrs) as span:
        try:
            yield span
        except Exception as exc:  # noqa: BLE001 — observe-then-reraise
            span.set_attribute("error.type", type(exc).__name__)
            if Status is not None and StatusCode is not None:
                span.set_status(Status(StatusCode.ERROR, description=str(exc)))
            raise


class _NoopSpan:
    """Stand-in for an OTel span when the API isn't installed.

    Implements just the methods the project's call sites use. Adding a
    new method here is the same conceptual step as adding a new
    semantic convention — be deliberate."""

    def set_attribute(self, _key: str, _value: Any) -> None:
        return None

    def set_attributes(self, _attrs: dict[str, Any]) -> None:
        return None

    def add_event(self, _name: str, _attrs: dict[str, Any] | None = None) -> None:
        return None

    def set_status(self, _status: Any, description: str | None = None) -> None:
        return None
