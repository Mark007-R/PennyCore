"""Stdlib + httpx load runner — Day 22, Phase 4.

Reproducible mid-Phase-4 load harness that:

  * Drives `POST /events` on the context-engine.
  * Either (a) talks to an already-running Uvicorn process via `httpx`
    or (b) drives the FastAPI app in-process via `fastapi.testclient
    .TestClient`. The TestClient path is the default — it boots no
    network listener, requires nothing installed beyond what
    `requirements.txt` already pins, and produces deterministic numbers
    that CI can replay. The remote path is what the SKILL line item
    ("locust simulating 100 concurrent customers") *conceptually*
    describes; `benchmarks/load_test.py` is its locust-shaped twin.
  * Simulates 100 concurrent customers across 5 tenants. Workload mix
    matches `benchmarks/load_test.py` exactly so the two harnesses are
    apples-to-apples.
  * Records per-request latency, tags by (tenant_id, channel,
    event_type), and writes a summary JSON to
    `results/phase4_load_results.json` (the path
    `docs/SYSTEM_DESIGN.md` §load names).

Why TestClient by default: the takehome dev box runs Windows + no
locust install. The in-process measurement still exposes the
ingestion-pipeline cost (validation → linker → repo upsert → bus
publish) which IS where Day-21 race tests showed contention; it just
omits the kernel socket layer (which is constant and small for
localhost). Reported numbers carry an explicit `transport` field so
nobody mistakes the in-process latency for a wire-level number.

CLI:

    python -m benchmarks.local_load_runner \
        --users 100 --duration 30 --tenants 5

    # Or against a live Uvicorn (drop into a second terminal first):
    python -m benchmarks.local_load_runner --base-url http://localhost:8000

Output: writes `results/phase4_load_results.json`; prints a summary
table to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence


RESULTS_PATH = Path(__file__).resolve().parent.parent / "results" / "phase4_load_results.json"

TENANTS: tuple[str, ...] = ("bank_a", "bank_b", "bank_c", "bank_d", "bank_e")
CHANNELS: tuple[str, ...] = ("chat", "email", "sms", "voice")
EVENT_TYPE_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("message_received", 80),
    ("document_uploaded", 15),
    ("status_changed", 5),
)
_event_type_pool = tuple(
    et for et, weight in EVENT_TYPE_WEIGHTS for _ in range(weight)
)


@dataclass
class CustomerSlot:
    tenant_id: str
    customer_external: str
    channel: str


def make_slots(num_customers: int, num_tenants: int) -> list[CustomerSlot]:
    """Distribute `num_customers` across `num_tenants` evenly, then
    rotate channel by index so each tenant sees all four channels.
    """
    if num_tenants > len(TENANTS):
        raise ValueError(f"max {len(TENANTS)} tenants, got {num_tenants}")
    per_tenant = num_customers // num_tenants
    leftover = num_customers - per_tenant * num_tenants
    slots: list[CustomerSlot] = []
    for t_idx in range(num_tenants):
        tenant = TENANTS[t_idx]
        count = per_tenant + (1 if t_idx < leftover else 0)
        for c_idx in range(count):
            slots.append(
                CustomerSlot(
                    tenant_id=tenant,
                    customer_external=f"cust_{tenant}_{c_idx:02d}",
                    channel=CHANNELS[(t_idx * per_tenant + c_idx) % len(CHANNELS)],
                )
            )
    return slots


def _payload_for(event_type: str, slot: CustomerSlot) -> dict[str, Any]:
    if event_type == "message_received":
        return {
            "from_external_id": slot.customer_external,
            "body": "Quick question about my mortgage application status.",
            "subject": None,
        }
    if event_type == "document_uploaded":
        return {
            "from_external_id": slot.customer_external,
            "doc_type": "pay_stub",
            "filename": f"{slot.customer_external}_paystub.pdf",
        }
    return {
        "from_external_id": slot.customer_external,
        "old_status": "submitted",
        "new_status": "underwriting",
    }


def _build_request_body(slot: CustomerSlot) -> tuple[dict[str, Any], str]:
    event_type = random.choice(_event_type_pool)
    idem = f"load_{uuid.uuid4().hex}"
    body = {
        "tenant_id": slot.tenant_id,
        "channel_code": slot.channel,
        "event_type": event_type,
        "idempotency_key": idem,
        "payload": _payload_for(event_type, slot),
    }
    return body, idem


# ---------------------------------------------------------------------------
# Transports
#
# Two flavors: in-process FastAPI TestClient (default; needs nothing
# extra) and live HTTP via httpx (requires a running Uvicorn). Same
# function shape so the runner doesn't care.
# ---------------------------------------------------------------------------


PostFn = Callable[[dict[str, Any], str], tuple[int, float]]
"""(body, idempotency_key) -> (status_code, elapsed_seconds)"""


def _make_testclient_poster() -> tuple[PostFn, str, dict[str, Any]]:
    """Return a TestClient-backed poster. Returns the poster + a label +
    a meta dict (mode/datastore/bus) suitable for the result JSON.
    """
    # Force in-memory + mock LLM so the runner doesn't need Postgres/Redis
    # to demonstrate the ingestion-pipeline cost. The reported numbers
    # carry a `datastore=memory` tag — Day 22 explicitly identifies this
    # as the next bottleneck to swap out (see report §Bottleneck).
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("REDIS_URL", None)
    os.environ["MOCK_LLM"] = "true"

    # Lazy import — TestClient bootstraps the FastAPI app, which has its
    # own import cost we want to bill against startup, not the loop.
    from fastapi.testclient import TestClient

    from context_engine.api import (
        app,
        get_bus,
        get_customer_repo,
        get_repo,
    )
    from context_engine.customer_repository import InMemoryCustomerRepository
    from context_engine.event_bus import InMemoryEventBus
    from context_engine.repository import InMemoryEventRepository

    # Shared repos across all workers so we exercise the same lock
    # contention surface the production deploy would see (one process,
    # one repo, N threads). This is the whole point of Day 21's RLock
    # work — Day 22 measures the cost of holding those locks under load.
    shared_repo = InMemoryEventRepository()
    shared_bus = InMemoryEventBus()
    shared_customers = InMemoryCustomerRepository()

    app.dependency_overrides[get_repo] = lambda: shared_repo
    app.dependency_overrides[get_bus] = lambda: shared_bus
    app.dependency_overrides[get_customer_repo] = lambda: shared_customers

    client = TestClient(app)
    label = "testclient (in-process)"
    meta = {
        "transport": "fastapi.testclient",
        "datastore": "memory",
        "bus": "in-memory",
        "llm": "mock",
    }

    def post(body: dict[str, Any], idem: str) -> tuple[int, float]:
        headers = {"X-Idempotency-Key": idem}
        t0 = time.perf_counter()
        resp = client.post("/events", json=body, headers=headers)
        elapsed = time.perf_counter() - t0
        return resp.status_code, elapsed

    return post, label, meta


def _make_httpx_poster(base_url: str) -> tuple[PostFn, str, dict[str, Any]]:
    import httpx

    client = httpx.Client(base_url=base_url, timeout=httpx.Timeout(10.0))
    label = f"httpx → {base_url}"
    meta = {
        "transport": "httpx",
        "datastore": "live (whatever the server is configured for)",
        "bus": "live",
        "llm": "live",
        "base_url": base_url,
    }

    def post(body: dict[str, Any], idem: str) -> tuple[int, float]:
        headers = {"X-Idempotency-Key": idem}
        t0 = time.perf_counter()
        resp = client.post("/events", json=body, headers=headers)
        elapsed = time.perf_counter() - t0
        return resp.status_code, elapsed

    return post, label, meta


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class Sample:
    tenant_id: str
    channel: str
    event_type: str
    status: int
    latency_s: float


@dataclass
class TenantStats:
    tenant_id: str
    count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    success_rate: float


@dataclass
class LoadSummary:
    total_requests: int
    duration_s: float
    throughput_rps: float
    success_count: int
    error_count: int
    success_rate: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    by_tenant: list[TenantStats]
    by_event_type: dict[str, int]
    meta: dict[str, Any]


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct * (len(sorted_values) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = rank - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def run_load(
    *,
    poster: PostFn,
    duration_s: float,
    num_customers: int,
    num_tenants: int,
    seed: int | None = None,
) -> tuple[LoadSummary, list[Sample]]:
    """Fire requests for `duration_s` seconds with `num_customers`
    concurrent workers across `num_tenants` tenants. Each worker holds
    one CustomerSlot and submits requests in a tight loop with a small
    randomized jitter (matching the locust `between(0.05, 0.25)`).
    """
    if seed is not None:
        random.seed(seed)

    slots = make_slots(num_customers, num_tenants)
    if len(slots) != num_customers:
        raise AssertionError(f"slot count {len(slots)} != {num_customers}")

    samples: list[Sample] = []
    samples_lock = threading.Lock()
    stop_at = time.monotonic() + duration_s

    def worker(slot: CustomerSlot) -> None:
        local: list[Sample] = []
        while time.monotonic() < stop_at:
            body, idem = _build_request_body(slot)
            try:
                status, latency = poster(body, idem)
            except Exception as exc:  # network / TestClient blowup
                status = -1
                latency = 0.0
                _ = exc
            local.append(
                Sample(
                    tenant_id=slot.tenant_id,
                    channel=slot.channel,
                    event_type=str(body["event_type"]),
                    status=status,
                    latency_s=latency,
                )
            )
            time.sleep(random.uniform(0.05, 0.25))
        with samples_lock:
            samples.extend(local)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=num_customers) as pool:
        futures = [pool.submit(worker, slot) for slot in slots]
        for f in as_completed(futures):
            # Surface any worker exception in the main thread.
            f.result()
    wall = time.perf_counter() - t0

    return _summarize(samples, wall), samples


def _summarize(samples: list[Sample], wall_s: float) -> LoadSummary:
    if not samples:
        return LoadSummary(
            total_requests=0,
            duration_s=wall_s,
            throughput_rps=0.0,
            success_count=0,
            error_count=0,
            success_rate=0.0,
            p50_ms=0.0,
            p95_ms=0.0,
            p99_ms=0.0,
            max_ms=0.0,
            by_tenant=[],
            by_event_type={},
            meta={},
        )

    latencies_ms = sorted(s.latency_s * 1000.0 for s in samples)
    success = sum(1 for s in samples if 200 <= s.status < 300)
    error = len(samples) - success

    by_tenant: list[TenantStats] = []
    tenant_groups: dict[str, list[Sample]] = {}
    for s in samples:
        tenant_groups.setdefault(s.tenant_id, []).append(s)
    for tenant_id, group in sorted(tenant_groups.items()):
        glats = sorted(s.latency_s * 1000.0 for s in group)
        gsucc = sum(1 for s in group if 200 <= s.status < 300)
        by_tenant.append(
            TenantStats(
                tenant_id=tenant_id,
                count=len(group),
                p50_ms=_percentile(glats, 0.50),
                p95_ms=_percentile(glats, 0.95),
                p99_ms=_percentile(glats, 0.99),
                success_rate=gsucc / len(group) if group else 0.0,
            )
        )

    by_event_type: dict[str, int] = {}
    for s in samples:
        by_event_type[s.event_type] = by_event_type.get(s.event_type, 0) + 1

    return LoadSummary(
        total_requests=len(samples),
        duration_s=wall_s,
        throughput_rps=len(samples) / wall_s if wall_s > 0 else 0.0,
        success_count=success,
        error_count=error,
        success_rate=success / len(samples),
        p50_ms=_percentile(latencies_ms, 0.50),
        p95_ms=_percentile(latencies_ms, 0.95),
        p99_ms=_percentile(latencies_ms, 0.99),
        max_ms=max(latencies_ms),
        by_tenant=by_tenant,
        by_event_type=by_event_type,
        meta={},
    )


def format_summary(summary: LoadSummary, label: str) -> str:
    lines: list[str] = []
    lines.append(f"=== PennyCore load test — {label} ===")
    lines.append(
        f"requests={summary.total_requests}  duration={summary.duration_s:.2f}s  "
        f"rps={summary.throughput_rps:.1f}  success={summary.success_rate * 100:.1f}%"
    )
    lines.append(
        f"latency ms: p50={summary.p50_ms:.2f}  p95={summary.p95_ms:.2f}  "
        f"p99={summary.p99_ms:.2f}  max={summary.max_ms:.2f}"
    )
    lines.append("per-tenant:")
    for t in summary.by_tenant:
        lines.append(
            f"  {t.tenant_id:8s} count={t.count:5d}  "
            f"p50={t.p50_ms:7.2f}  p95={t.p95_ms:7.2f}  "
            f"p99={t.p99_ms:7.2f}  success={t.success_rate * 100:5.1f}%"
        )
    lines.append(f"by event_type: {summary.by_event_type}")
    return "\n".join(lines)


def write_results(summary: LoadSummary, label: str, samples: list[Sample]) -> Path:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "label": label,
        "summary": asdict(summary),
        "raw_count": len(samples),
        # Keep a small sample of raw latencies for downstream charting
        # — full list would bloat the JSON without adding signal beyond
        # the summary stats.
        "raw_latency_ms_sample": [
            samples[i].latency_s * 1000.0
            for i in range(0, len(samples), max(1, len(samples) // 200))
        ],
    }
    RESULTS_PATH.write_text(json.dumps(payload, indent=2, default=str))
    return RESULTS_PATH


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=100, help="concurrent customers")
    parser.add_argument("--tenants", type=int, default=5, help="number of tenants (max 5)")
    parser.add_argument("--duration", type=float, default=30.0, help="seconds")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help="if set, use httpx against a live server (default: TestClient in-process)",
    )
    parser.add_argument("--out", type=Path, default=RESULTS_PATH)
    args = parser.parse_args(argv)

    if args.base_url:
        poster, label, meta = _make_httpx_poster(args.base_url)
    else:
        poster, label, meta = _make_testclient_poster()

    summary, samples = run_load(
        poster=poster,
        duration_s=args.duration,
        num_customers=args.users,
        num_tenants=args.tenants,
        seed=args.seed,
    )
    summary.meta = {
        **meta,
        "users": args.users,
        "tenants": args.tenants,
        "duration_requested_s": args.duration,
        "seed": args.seed,
    }
    print(format_summary(summary, label))
    path = write_results(summary, label, samples)
    # `print()` on Windows cp1252 console rejects non-ASCII glyphs; an
    # ASCII arrow keeps this runnable from PowerShell without an
    # `PYTHONIOENCODING=utf-8` shim.
    print(f"results -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
