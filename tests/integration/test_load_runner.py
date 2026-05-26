"""Day 22 — smoke + correctness tests for the load-runner harness.

These are NOT performance tests (no asserting on a specific p95). They
verify the harness itself: does it fire requests, account for them,
distribute across tenants evenly, and write a parseable result file.
Performance numbers come from the manual run captured in
``results/phase4_load_results.json`` and the Day 22 report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.local_load_runner import (
    LoadSummary,
    Sample,
    TENANTS,
    _build_request_body,
    _make_testclient_poster,
    _percentile,
    _summarize,
    format_summary,
    make_slots,
    run_load,
    write_results,
)


def test_make_slots_distributes_evenly_across_5_tenants() -> None:
    slots = make_slots(num_customers=100, num_tenants=5)
    assert len(slots) == 100
    by_tenant = {tenant: 0 for tenant in TENANTS}
    for s in slots:
        by_tenant[s.tenant_id] += 1
    # 100 / 5 = 20 each, exactly — the SKILL workload shape.
    assert all(count == 20 for count in by_tenant.values()), by_tenant


def test_make_slots_handles_uneven_split() -> None:
    # 7 customers across 5 tenants → first 2 tenants get one extra each
    # (rounding up the leftover) so total still equals N customers.
    slots = make_slots(num_customers=7, num_tenants=5)
    assert len(slots) == 7
    by_tenant: dict[str, int] = {}
    for s in slots:
        by_tenant[s.tenant_id] = by_tenant.get(s.tenant_id, 0) + 1
    # Two tenants get 2 customers, the other three get 1.
    counts = sorted(by_tenant.values())
    assert counts == [1, 1, 1, 2, 2]


def test_make_slots_rejects_too_many_tenants() -> None:
    with pytest.raises(ValueError):
        make_slots(num_customers=10, num_tenants=99)


def test_build_request_body_shape() -> None:
    slot = make_slots(num_customers=5, num_tenants=5)[0]
    body, idem = _build_request_body(slot)
    assert body["tenant_id"] == slot.tenant_id
    assert body["channel_code"] == slot.channel
    assert body["event_type"] in {
        "message_received",
        "document_uploaded",
        "status_changed",
    }
    assert body["idempotency_key"] == idem
    assert idem.startswith("load_")
    assert isinstance(body["payload"], dict)
    # Every payload carries the customer external id so the linker can
    # match it against the same identity row.
    assert body["payload"]["from_external_id"] == slot.customer_external


def test_percentile_handles_edge_cases() -> None:
    assert _percentile([], 0.5) == 0.0
    assert _percentile([42.0], 0.99) == 42.0
    # Sorted ascending: p50 of 1..100 is ~50.5 via linear interpolation.
    values = sorted(range(1, 101))
    p50 = _percentile([float(v) for v in values], 0.5)
    assert 50.0 <= p50 <= 51.0


def test_summarize_buckets_by_tenant() -> None:
    samples = [
        Sample("bank_a", "chat", "message_received", 202, 0.010),
        Sample("bank_a", "chat", "message_received", 202, 0.020),
        Sample("bank_b", "email", "document_uploaded", 200, 0.030),
        Sample("bank_b", "email", "document_uploaded", 500, 0.040),  # error
    ]
    summary = _summarize(samples, wall_s=2.0)
    assert summary.total_requests == 4
    assert summary.success_count == 3
    assert summary.error_count == 1
    assert pytest.approx(summary.success_rate) == 0.75
    assert summary.throughput_rps == 2.0
    by_tenant = {t.tenant_id: t for t in summary.by_tenant}
    assert by_tenant["bank_a"].count == 2
    assert by_tenant["bank_a"].success_rate == 1.0
    assert by_tenant["bank_b"].success_rate == 0.5
    assert summary.by_event_type == {
        "message_received": 2,
        "document_uploaded": 2,
    }


def test_format_summary_renders_per_tenant_lines() -> None:
    samples = [
        Sample("bank_a", "chat", "message_received", 202, 0.010),
        Sample("bank_b", "email", "document_uploaded", 200, 0.030),
    ]
    summary = _summarize(samples, wall_s=1.0)
    text = format_summary(summary, label="unit-test")
    assert "PennyCore load test" in text
    assert "bank_a" in text
    assert "bank_b" in text


def test_run_load_smoke_against_testclient(tmp_path: Path, monkeypatch) -> None:
    """End-to-end smoke: 5 workers, 2 tenants, 1 second of load against
    the in-process TestClient. Asserts:
      * every recorded request was a 202 (new) or 200 (dedupe) — the
        ingestion contract on POST /events.
      * at least one request per tenant landed (slot distribution
        works).
      * the runner wrote a parseable JSON file to the target path.
    """
    poster, label, meta = _make_testclient_poster()
    summary, samples = run_load(
        poster=poster,
        duration_s=1.0,
        num_customers=5,
        num_tenants=2,
        seed=7,
    )

    assert summary.total_requests >= 5, "should fire at least one request per worker"
    statuses = {s.status for s in samples}
    assert statuses <= {200, 202}, f"unexpected statuses: {statuses}"
    assert summary.success_rate == 1.0

    # Slot distribution: every chosen tenant got at least one hit.
    tenants_hit = {s.tenant_id for s in samples}
    assert len(tenants_hit) == 2

    # Write + reload — the result JSON must be valid and contain the
    # summary shape downstream tooling (Day 22 report) reads.
    out_path = tmp_path / "phase4_load_results.json"
    monkeypatch.setattr(
        "benchmarks.local_load_runner.RESULTS_PATH", out_path
    )
    written = write_results(summary, label, samples)
    assert written == out_path
    payload = json.loads(out_path.read_text())
    assert payload["summary"]["total_requests"] == summary.total_requests
    assert "raw_latency_ms_sample" in payload
