"""Day 33 — Phase 7 test-sweep coverage backfill for the observability
no-op / SDK-unavailable branches.

The main observability test file (`test_observability.py`) requires the OTel
SDK to be installed AND a TracerProvider to be wired up — those tests don't
reach the "API isn't installed" fallback code in `contracts/observability.py`
because the SDK is always installed in the test image.

These tests close that gap by:

  1. Exercising `_NoopSpan` directly — the stand-in returned when the OTel
     API isn't importable. Every public method must accept its kwargs and
     silently no-op, so call sites don't have to branch on
     `_OTEL_API_AVAILABLE`.
  2. Monkeypatching `_OTEL_API_AVAILABLE` to False to drive `setup_tracing`
     into the "unavailable" mode and `trace_span` into the noop yield.

Why direct monkeypatching is OK: `_OTEL_API_AVAILABLE` is module-level state
that gets set once at import. Tests that need to simulate "the SDK isn't
installed" do so by flipping the flag — uninstalling the SDK per-test would
break every other observability test.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from contracts import observability
from contracts.observability import _NoopSpan, setup_tracing, trace_span


@pytest.fixture
def _force_otel_unavailable(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pretend the opentelemetry API isn't importable.

    `_OTEL_API_AVAILABLE` gates every branch in the module — flipping it
    drives both `setup_tracing` and `trace_span` into the no-op shape that
    PennyCore expects when the production image was built without the
    optional `opentelemetry-api` extra.
    """
    monkeypatch.setattr(observability, "_OTEL_API_AVAILABLE", False)
    # The mode cache must also be cleared so `setup_tracing` re-resolves.
    monkeypatch.setattr(observability, "_active_mode", None)
    yield


class TestNoopSpan:
    """The stand-in span surface — every method is a deliberate no-op."""

    def test_set_attribute_returns_none_and_does_not_raise(self) -> None:
        span = _NoopSpan()
        assert span.set_attribute("pennycore.tenant_id", "acme") is None

    def test_set_attributes_returns_none_and_does_not_raise(self) -> None:
        span = _NoopSpan()
        assert (
            span.set_attributes(
                {"pennycore.tenant_id": "acme", "pennycore.event_id": "evt_1"}
            )
            is None
        )

    def test_add_event_with_and_without_attributes(self) -> None:
        span = _NoopSpan()
        assert span.add_event("dedup-hit") is None
        assert span.add_event("dedup-hit", {"pennycore.action_id": "act_1"}) is None

    def test_set_status_with_and_without_description(self) -> None:
        # Real callers pass an OTel `Status` object; the stub ignores it.
        span = _NoopSpan()
        assert span.set_status(object()) is None
        assert span.set_status(object(), description="executor timeout") is None


class TestSetupTracingUnavailable:
    def test_setup_tracing_returns_unavailable_when_otel_api_missing(
        self, _force_otel_unavailable: None
    ) -> None:
        assert setup_tracing("pennycore-test-svc") == "unavailable"
        # The mode is cached so `/info` can surface "unavailable" too.
        assert observability.get_tracing_mode() == "unavailable"


class TestTraceSpanUnavailable:
    def test_trace_span_yields_noop_when_otel_api_missing(
        self, _force_otel_unavailable: None
    ) -> None:
        with trace_span(
            "pennycore.test",
            tenant_id="acme",
            attributes={"pennycore.event_id": "evt_1"},
        ) as span:
            assert isinstance(span, _NoopSpan)
            # set_attribute on the NoopSpan must not raise — that's the
            # whole point of the surface symmetry.
            span.set_attribute("pennycore.late_attr", "value")

    def test_trace_span_swallows_no_exceptions_under_noop(
        self, _force_otel_unavailable: None
    ) -> None:
        """Even in noop mode, exceptions raised inside the block must propagate
        — `trace_span` is observability, not error handling."""
        with pytest.raises(ValueError):
            with trace_span("pennycore.boom", tenant_id="acme"):
                raise ValueError("boom")
