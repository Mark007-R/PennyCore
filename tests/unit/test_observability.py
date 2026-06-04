"""Tests for the Day-30 OpenTelemetry observability surface.

Five things matter:

  1. `trace_span` is a no-op (no exceptions, no spans emitted) when
     `setup_tracing` hasn't been called.
  2. `setup_tracing()` with no `OTEL_EXPORTER_OTLP_ENDPOINT` configured
     resolves to ``"noop"`` mode — spans run but are not exported.
  3. With a `TracerProvider` configured (via in-memory exporter), every
     `trace_span` actually creates a span carrying its name + tenant_id
     + caller-supplied attributes.
  4. Spine instrumentation: `ingest_event`, `handle_proposal`, and
     `executor.execute` all produce spans with the expected
     `pennycore.*` attributes when the system runs end-to-end through
     an InMemorySpanExporter.
  5. The `/info` endpoint surfaces the active tracing mode so an
     operator can confirm OTel is wired without reading container env.
"""

from __future__ import annotations

import importlib
from typing import Iterator

import pytest

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from contracts.observability import (
    _reset_for_tests,
    get_tracing_mode,
    setup_tracing,
    trace_span,
)


# ---------------------------------------------------------------------------
# Test fixtures — every test starts with a fresh exporter so spans don't
# leak across tests.
# ---------------------------------------------------------------------------


_EXPORTER: InMemorySpanExporter | None = None
_PROVIDER: TracerProvider | None = None


@pytest.fixture(scope="module", autouse=True)
def _install_inmem_provider() -> Iterator[None]:
    """Install one shared TracerProvider + InMemorySpanExporter for the
    test module. OTel doesn't allow re-installing a provider once
    set; sharing one across the module is the only test pattern that
    plays nicely with that rule."""
    global _EXPORTER, _PROVIDER
    _EXPORTER = InMemorySpanExporter()
    _PROVIDER = TracerProvider(
        resource=Resource.create({"service.name": "pennycore-test"})
    )
    _PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
    trace.set_tracer_provider(_PROVIDER)
    yield


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    """Per-test handle on the module-shared exporter. Cleared before
    AND after each test so spans never leak."""
    assert _EXPORTER is not None
    _EXPORTER.clear()
    _reset_for_tests()
    yield _EXPORTER
    _EXPORTER.clear()
    _reset_for_tests()


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


def test_setup_tracing_returns_noop_when_no_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    _reset_for_tests()
    assert setup_tracing("pennycore-test-svc") == "noop"
    assert get_tracing_mode() == "noop"


def test_setup_tracing_is_idempotent(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    _reset_for_tests()
    first = setup_tracing("svc")
    second = setup_tracing("svc")
    assert first == second == "noop"


def test_setup_tracing_otlp_mode_when_endpoint_present(monkeypatch) -> None:
    """With the OTLP endpoint set, `setup_tracing` resolves to ``otlp``
    even when a global TracerProvider is already installed. OTel
    forbids overriding the provider — `setup_tracing` logs the
    "Overriding ... not allowed" warning but still updates the
    module-local mode marker, which is what `/info` surfaces."""
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318"
    )
    _reset_for_tests()
    mode = setup_tracing("pennycore-test-svc")
    assert mode == "otlp"
    assert get_tracing_mode() == "otlp"


# ---------------------------------------------------------------------------
# trace_span shape
# ---------------------------------------------------------------------------


def test_trace_span_is_safe_without_setup(exporter) -> None:
    """`trace_span` must work even if `setup_tracing` was never called.
    This is the "import-time safety" invariant — code can use the
    decorator/contextmanager without the API user having to wire OTel.
    The shared in-memory provider is installed by the autouse fixture;
    this test just asserts the surface doesn't raise."""
    with trace_span("pennycore.test", tenant_id="acme"):
        pass
    assert len(exporter.get_finished_spans()) == 1


def test_trace_span_emits_one_span_with_tenant_attribute(exporter) -> None:
    with trace_span(
        "pennycore.test_span",
        tenant_id="tenant_acme_bank",
        attributes={"pennycore.event_id": "evt_123"},
    ):
        pass
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "pennycore.test_span"
    assert span.attributes["pennycore.tenant_id"] == "tenant_acme_bank"
    assert span.attributes["pennycore.event_id"] == "evt_123"


def test_trace_span_sets_attribute_after_open(exporter) -> None:
    with trace_span("pennycore.late_attr", tenant_id="acme") as span:
        span.set_attribute("pennycore.decision", "auto")
    spans = exporter.get_finished_spans()
    assert spans[0].attributes["pennycore.decision"] == "auto"


def test_trace_span_marks_error_on_exception(exporter) -> None:
    with pytest.raises(ValueError):
        with trace_span("pennycore.boom", tenant_id="acme"):
            raise ValueError("boom")
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["error.type"] == "ValueError"
    assert span.status.status_code.name == "ERROR"


def test_trace_span_omits_none_attributes(exporter) -> None:
    """Passing `None` attribute values must not crash the OTel SDK."""
    with trace_span(
        "pennycore.skip_none",
        tenant_id="acme",
        attributes={"pennycore.event_id": None, "pennycore.real": "yes"},
    ):
        pass
    span = exporter.get_finished_spans()[0]
    assert "pennycore.event_id" not in span.attributes
    assert span.attributes["pennycore.real"] == "yes"


# ---------------------------------------------------------------------------
# Spine instrumentation — ingest_event, handle_proposal, executor.execute
# ---------------------------------------------------------------------------


def test_ingest_event_emits_span_with_event_id(exporter) -> None:
    from context_engine.event_bus import InMemoryEventBus
    from context_engine.ingestion import ingest_event
    from context_engine.repository import InMemoryEventRepository
    from contracts import ChannelType, Event, EventType
    from datetime import datetime, timezone

    repo = InMemoryEventRepository()
    bus = InMemoryEventBus()
    event = Event(
        id="evt_abc",
        tenant_id="tenant_acme_bank",
        customer_id="cus_jane",
        channel_code=ChannelType.EMAIL,
        event_type=EventType.MESSAGE_RECEIVED,
        idempotency_key="idem_evt_abc",
        payload={"body": "hi"},
        received_at=datetime.now(timezone.utc),
    )

    result = ingest_event(event, repo=repo, bus=bus)
    assert result.created

    spans = [s for s in exporter.get_finished_spans() if s.name == "pennycore.ingest_event"]
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["pennycore.tenant_id"] == "tenant_acme_bank"
    assert span.attributes["pennycore.event_id"] == "evt_abc"
    assert span.attributes["pennycore.event_type"] == EventType.MESSAGE_RECEIVED.value
    assert span.attributes["pennycore.created"] is True
    assert span.attributes["pennycore.bus_published"] is True


def test_ingest_event_duplicate_marks_created_false(exporter) -> None:
    from context_engine.event_bus import InMemoryEventBus
    from context_engine.ingestion import ingest_event
    from context_engine.repository import InMemoryEventRepository
    from contracts import ChannelType, Event, EventType
    from datetime import datetime, timezone

    repo = InMemoryEventRepository()
    bus = InMemoryEventBus()
    base_event = dict(
        id="evt_dup",
        tenant_id="tenant_acme_bank",
        customer_id="cus_jane",
        channel_code=ChannelType.EMAIL,
        event_type=EventType.MESSAGE_RECEIVED,
        idempotency_key="idem_evt_dup",
        payload={"body": "hi"},
        received_at=datetime.now(timezone.utc),
    )

    ingest_event(Event(**base_event), repo=repo, bus=bus)
    exporter.clear()
    ingest_event(Event(**{**base_event, "id": "evt_dup_2"}), repo=repo, bus=bus)

    spans = [s for s in exporter.get_finished_spans() if s.name == "pennycore.ingest_event"]
    assert len(spans) == 1
    assert spans[0].attributes["pennycore.created"] is False
    # `pennycore.bus_published` is only set when an actual publish happens —
    # duplicates skip the bus, so the attribute must be absent.
    assert "pennycore.bus_published" not in spans[0].attributes


def test_handle_proposal_emits_decision_span(exporter) -> None:
    from contracts.actions import ActionProposal, ActionType, ProposedBy
    from orchestrator.decision_pipeline import make_default_pipeline

    pipeline = make_default_pipeline()
    proposal = ActionProposal(
        id="pp_1",
        tenant_id="tenant_acme_bank",
        event_id="evt_ingested",
        customer_id="cus_jane",
        action_type=ActionType.SEND_BORROWER_MESSAGE,
        proposed_by=ProposedBy.FALLBACK,
        payload={"_planner_reasoning": "ack the message"},
    )
    pipeline.handle_proposal(proposal)

    spans = [
        s
        for s in exporter.get_finished_spans()
        if s.name == "pennycore.decision.handle_proposal"
    ]
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["pennycore.tenant_id"] == "tenant_acme_bank"
    assert span.attributes["pennycore.event_id"] == "evt_ingested"
    assert span.attributes["pennycore.action_type"] == "send_borrower_message"
    assert "pennycore.decision" in span.attributes
    assert "pennycore.action_id" in span.attributes


def test_handle_proposal_dedup_hit_marked_in_span(exporter) -> None:
    from contracts.actions import ActionProposal, ActionType, ProposedBy
    from orchestrator.decision_pipeline import make_default_pipeline

    pipeline = make_default_pipeline()
    proposal = ActionProposal(
        id="pp_dedup",
        tenant_id="tenant_acme_bank",
        event_id="evt_dedup",
        customer_id="cus_jane",
        action_type=ActionType.SEND_BORROWER_MESSAGE,
        proposed_by=ProposedBy.FALLBACK,
        payload={"_planner_reasoning": "ack"},
    )
    pipeline.handle_proposal(proposal)
    exporter.clear()
    pipeline.handle_proposal(proposal)

    spans = [
        s
        for s in exporter.get_finished_spans()
        if s.name == "pennycore.decision.handle_proposal"
    ]
    assert len(spans) == 1
    assert spans[0].attributes["pennycore.dedup_hit"] is True


def test_executor_execute_emits_span(exporter) -> None:
    """Exercise the executor span via the pipeline. The default
    declarative engine has no tenant configured, so it falls through
    to APPROVAL_REQUIRED; we configure the tenant's `send_borrower_message`
    policy as `auto` so the auto-execute path runs end-to-end."""
    from contracts.actions import ActionProposal, ActionType, ProposedBy
    from orchestrator.decision_pipeline import make_default_pipeline

    pipeline = make_default_pipeline()
    # Configure a tenant policy that lets send_borrower_message
    # auto-execute. This is the documented `set_policies` surface;
    # without it the pipeline goes to approval-queue and the executor
    # span never opens.
    pipeline.policy.set_policies(
        "tenant_acme_bank",
        {"send_borrower_message": "auto"},
    )

    proposal = ActionProposal(
        id="pp_exec",
        tenant_id="tenant_acme_bank",
        event_id="evt_exec",
        customer_id="cus_jane",
        action_type=ActionType.SEND_BORROWER_MESSAGE,
        proposed_by=ProposedBy.FALLBACK,
        payload={"_planner_reasoning": "ack"},
    )
    pipeline.handle_proposal(proposal)

    spans = [
        s
        for s in exporter.get_finished_spans()
        if s.name == "pennycore.executor.execute"
    ]
    assert len(spans) == 1
    span = spans[-1]
    assert span.attributes["pennycore.tenant_id"] == "tenant_acme_bank"
    assert span.attributes["pennycore.action_type"] == "send_borrower_message"
    assert span.attributes["pennycore.executed"] is True


# ---------------------------------------------------------------------------
# /info endpoint surfaces the tracing mode
# ---------------------------------------------------------------------------


def test_info_endpoint_surfaces_tracing_mode(monkeypatch) -> None:
    """A deployed operator hits `/info` to confirm tracing is on. The
    surface must report the mode `setup_tracing` resolved to."""
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318"
    )
    _reset_for_tests()

    import context_engine.api as ce_api

    importlib.reload(ce_api)

    from fastapi.testclient import TestClient

    client = TestClient(ce_api.app)
    payload = client.get("/info").json()
    assert payload["service"] == "context-engine"
    assert payload["tracing_mode"] == "otlp"


def test_info_endpoint_tracing_mode_noop_when_endpoint_unset(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    _reset_for_tests()

    import context_engine.api as ce_api

    importlib.reload(ce_api)

    from fastapi.testclient import TestClient

    client = TestClient(ce_api.app)
    payload = client.get("/info").json()
    assert payload["tracing_mode"] == "noop"


# ---------------------------------------------------------------------------
# Collector config asserts
# ---------------------------------------------------------------------------


def test_otel_collector_config_exists_and_routes_to_jaeger() -> None:
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    cfg = (repo_root / "otel" / "collector-config.yaml").read_text(encoding="utf-8")
    assert "receivers:" in cfg
    assert "otlp:" in cfg
    assert "exporters:" in cfg
    assert "otlp/jaeger" in cfg
    assert "jaeger:4317" in cfg


def test_otel_compose_overlay_injects_endpoint_into_both_services() -> None:
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    overlay = (repo_root / "docker-compose.otel.yml").read_text(encoding="utf-8")
    # The overlay must inject the collector endpoint into BOTH services.
    assert overlay.count("OTEL_EXPORTER_OTLP_ENDPOINT") >= 2
    assert "otel-collector:4318" in overlay
    assert "jaegertracing/all-in-one" in overlay
