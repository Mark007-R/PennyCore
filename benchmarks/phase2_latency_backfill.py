"""Phase 2 latency backfill (Day-11 follow-up).

The SKILL's "PRIMARY METRICS" section mandates two latency
measurements that Phase 2 did NOT capture in their natural-fit days:

  * **Orchestrator decision latency (p50 / p95 / p99)** — should have
    been measured on Day 9 (planner) or Day 10 (full pipeline).
    Captured here against `DecisionPipeline.handle_proposal()`.

  * **End-to-end latency from event ingestion to action execution
    (p50 / p95 / p99)** — should have been the centerpiece of the
    Day 11 Phase-2 wrap. Captured here as the wall-clock duration of
    a `POST /events` call when both FastAPI apps share an
    `InMemoryEventBus`, so the orchestrator's listener fires
    synchronously inside the same HTTP request that ingested the
    event.

## Method

Both benchmarks use `time.perf_counter_ns()` (monotonic, sub-microsecond
resolution on Windows + Linux) and report deciles + p95 + p99 across
`SAMPLES` independent runs. Warm-up: first `WARMUP` runs are excluded
from the stats so module-import / JIT / cache cost doesn't pollute the
p50.

Mode: **mock LLM** (the project default; `LLM_PROVIDER=anthropic` in
`.env` resolves to MockClient because the API key is a placeholder).
Real-LLM latency is dominated by the network call — measuring it here
would conflate "our code" with "the provider". Phase 3's quality study
will capture real-LLM latency as a separate metric.

## Caveat: in-process vs production

The end-to-end benchmark uses `InMemoryEventBus`, so the orchestrator
listener fires synchronously inside the publish call. Production runs
Redis Pub/Sub between publisher and subscriber — the orchestrator gets
a few extra ms of network + worker-scheduling latency, AND the POST
response returns BEFORE the orchestrator processes the event (the
publish is fire-and-forget). The in-process measurement is therefore
a **lower bound** on production end-to-end latency, NOT a model of
production wall-clock. Phase 6 OpenTelemetry instrumentation (Day 30)
captures the real production E2E from trace spans.

## Output

Prints a per-bench table to stdout AND writes a JSON file to
`results/phase2_latency_backfill.json` so the Day-12 commit can pin
the numbers into the metrics journal.

## Usage

    python benchmarks/phase2_latency_backfill.py

Reproducibility: every run uses fresh in-memory state (new pipeline /
new buses) and synthetic events with deterministic IDs (`evt_bench_NNN`)
so reruns are deterministic in shape, even though wall-clock numbers
vary by hardware.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

# Allow `python benchmarks/...` invocation from the project root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from context_engine import api as ce_api  # noqa: E402
from context_engine.customer_repository import (  # noqa: E402
    InMemoryCustomerRepository,
)
from context_engine.event_bus import InMemoryEventBus  # noqa: E402
from context_engine.repository import InMemoryEventRepository  # noqa: E402
from contracts.actions import (  # noqa: E402
    ActionProposal,
    ActionType,
    ProposedBy,
)
from orchestrator import api as orch_api  # noqa: E402
from orchestrator.decision_pipeline import make_default_pipeline  # noqa: E402


SAMPLES = 500
WARMUP = 50


def _percentiles_ms(times_ns: list[int]) -> dict[str, float]:
    """Convert a list of nanosecond durations to a {p50, p95, p99,
    mean, min, max, n} dict in milliseconds. All values rounded to
    4 decimal places (sub-microsecond precision)."""
    times_ms = [t / 1_000_000 for t in times_ns]
    times_ms.sort()
    n = len(times_ms)

    def pct(p: float) -> float:
        # Linear interpolation. statistics.quantiles is the alternative
        # but it returns the cut-points between deciles, not arbitrary
        # percentiles — and we need p95 / p99 specifically.
        if n == 1:
            return times_ms[0]
        idx = (n - 1) * p
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return times_ms[lo] + (times_ms[hi] - times_ms[lo]) * frac

    return {
        "n": n,
        "min": round(min(times_ms), 4),
        "p50": round(pct(0.50), 4),
        "p95": round(pct(0.95), 4),
        "p99": round(pct(0.99), 4),
        "max": round(max(times_ms), 4),
        "mean": round(statistics.fmean(times_ms), 4),
        "stdev": round(statistics.pstdev(times_ms), 4) if n > 1 else 0.0,
    }


# ---------------------------------------------------------------------------
# Bench 1 — orchestrator decision latency
# ---------------------------------------------------------------------------


def _build_proposal(i: int) -> ActionProposal:
    """One independent proposal per sample. Each gets a unique
    `event_id` AND `id` so the pipeline's `(tenant_id, event_id)`
    dedup index never short-circuits — every sample exercises the
    full propose → policy → action-store → audit-write path."""
    return ActionProposal(
        id=f"prop_bench_{i:06d}",
        tenant_id="bench-tenant",
        event_id=f"evt_bench_{i:06d}",
        customer_id=f"cust_bench_{i:06d}",
        action_type=ActionType.SEND_BORROWER_MESSAGE,
        proposed_by=ProposedBy.FALLBACK,
        payload={"_planner_reasoning": "synthetic bench proposal"},
    )


def bench_decision_pipeline() -> dict[str, Any]:
    """Per-proposal latency for `DecisionPipeline.handle_proposal()`.

    Setup: fresh pipeline, policy table configured so every action
    routes to the auto-execute branch (the longest pipeline path —
    proposal → policy → action-store put → executor call → audit
    write x3). The approval-required path is shorter (no execution +
    no execute-audit row), so timing auto-execute gives the
    conservative number.
    """
    pipeline = make_default_pipeline()
    pipeline.policy.set_policies("bench-tenant", {"*": "auto"})

    proposals = [_build_proposal(i) for i in range(SAMPLES + WARMUP)]

    # Warm up — pull module imports + JIT-ish startup cost out of the
    # critical path.
    for p in proposals[:WARMUP]:
        pipeline.handle_proposal(p)

    times_ns: list[int] = []
    for p in proposals[WARMUP:]:
        t0 = time.perf_counter_ns()
        pipeline.handle_proposal(p)
        times_ns.append(time.perf_counter_ns() - t0)

    return {
        "what": (
            "DecisionPipeline.handle_proposal() — synthetic proposal "
            "through policy(auto) -> action-store put -> mock executor "
            "-> audit writes"
        ),
        "samples": SAMPLES,
        "warmup_excluded": WARMUP,
        "policy_path": "auto (longest path: PROPOSAL + DECISION + EXECUTION audit rows)",
        "llm_mode": "n/a (pipeline is below the planner — no LLM call here)",
        "latency_ms": _percentiles_ms(times_ns),
    }


# ---------------------------------------------------------------------------
# Bench 2 — end-to-end ingestion → action execution
# ---------------------------------------------------------------------------


def _build_e2e_clients() -> tuple[TestClient, TestClient, InMemoryEventBus]:
    """Wire two TestClients against ONE shared `InMemoryEventBus`.

    Because the orchestrator's `InMemoryEventListener` registers as a
    bus subscriber, a `POST /events` on the context-engine TestClient
    fires the orchestrator's chained handler stack (buffer + planner +
    decision pipeline) synchronously inside the same call. The HTTP
    response time IS the end-to-end latency.
    """
    bus = InMemoryEventBus()
    repo = InMemoryEventRepository()
    customers = InMemoryCustomerRepository()

    ce_api.app.dependency_overrides[ce_api.get_repo] = lambda: repo
    ce_api.app.dependency_overrides[ce_api.get_bus] = lambda: bus
    ce_api.app.dependency_overrides[ce_api.get_customer_repo] = lambda: customers

    orch_api._reset_listener_for_tests()
    pipeline = orch_api._get_pipeline_for_tests()
    pipeline.policy.set_policies("bench-tenant", {"*": "auto"})
    orch_api.attach_in_memory_bus(bus)

    ce = TestClient(ce_api.app)
    orch = TestClient(orch_api.app)
    ce.__enter__()
    orch.__enter__()
    return ce, orch, bus


def _teardown_e2e_clients(ce: TestClient, orch: TestClient) -> None:
    try:
        ce.__exit__(None, None, None)
    finally:
        orch.__exit__(None, None, None)
        ce_api.app.dependency_overrides.clear()
        orch_api._reset_listener_for_tests()


def bench_end_to_end() -> dict[str, Any]:
    """Per-event latency for the full path: POST /events → ingestion →
    bus publish → listener handler → planner → decision pipeline →
    audit-row writes.

    Each sample uses a unique `idempotency_key` so the publisher's
    dedup short-circuit never fires. The orchestrator's pipeline
    dedup is also unique-per-sample because `event_id` is server-
    stamped per request.
    """
    ce, orch, _bus = _build_e2e_clients()
    try:
        # Warmup
        for i in range(WARMUP):
            ce.post(
                "/events",
                json={
                    "tenant_id": "bench-tenant",
                    "channel_code": "api",
                    "event_type": "message_received",
                    "idempotency_key": f"bench-warmup-{i:06d}",
                    "payload": {
                        "external_id": f"cust_bench_warm_{i:06d}",
                        "text": "warmup",
                    },
                },
            )

        times_ns: list[int] = []
        for i in range(SAMPLES):
            body = {
                "tenant_id": "bench-tenant",
                "channel_code": "api",
                "event_type": "message_received",
                "idempotency_key": f"bench-{i:06d}",
                "payload": {
                    "external_id": f"cust_bench_{i:06d}",
                    "text": "synthetic bench event",
                },
            }
            t0 = time.perf_counter_ns()
            resp = ce.post("/events", json=body)
            elapsed = time.perf_counter_ns() - t0
            if resp.status_code not in (200, 202):
                raise RuntimeError(
                    f"bench POST failed: status={resp.status_code} "
                    f"body={resp.text}"
                )
            times_ns.append(elapsed)

        # Sanity check: orchestrator received events. The /events/recent
        # API caps limit at 100 AND the RecentEventsBuffer is bounded at
        # 50 per tenant — so at SAMPLES=500 we expect `count` to be 50,
        # not SAMPLES+WARMUP. The decision pipeline's action store is the
        # source of truth for "did every event produce an action"; we
        # query it directly via the pipeline handle.
        recent_resp = orch.get(
            "/events/recent",
            params={"tenant_id": "bench-tenant", "limit": 100},
        )
        recent = recent_resp.json() if recent_resp.status_code == 200 else {}
        observed_in_buffer = recent.get("count", 0)
        actions_in_pipeline = len(
            orch_api._get_pipeline_for_tests().list_actions_for_tenant(
                "bench-tenant"
            )
        )

        return {
            "what": (
                "POST /events (context-engine) -> InMemoryEventBus -> "
                "orchestrator listener -> chain_handlers (buffer + planner "
                "+ pipeline) -> audit writes -- all in-process so response "
                "time IS end-to-end latency"
            ),
            "samples": SAMPLES,
            "warmup_excluded": WARMUP,
            "transport": "InMemoryEventBus (synchronous subscriber); production Redis Pub/Sub adds extra ms",
            "policy_path": "auto (every event auto-executes — longest pipeline path)",
            "llm_mode": "mock (rule-table fallback proposals)",
            "orchestrator_recent_events_buffered": observed_in_buffer,
            "orchestrator_recent_events_buffer_cap": 50,
            "orchestrator_actions_in_pipeline_store": actions_in_pipeline,
            "expected_actions": SAMPLES + WARMUP,
            "latency_ms": _percentiles_ms(times_ns),
        }
    finally:
        _teardown_e2e_clients(ce, orch)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    print(f"running with SAMPLES={SAMPLES}, WARMUP={WARMUP}")
    print()

    print("=" * 72)
    print(" bench 1 — DecisionPipeline.handle_proposal()")
    print("=" * 72)
    dec = bench_decision_pipeline()
    for k, v in dec["latency_ms"].items():
        print(f"  {k:>6} = {v} ms")
    print()

    print("=" * 72)
    print(" bench 2 — end-to-end ingestion -> action execution")
    print("=" * 72)
    e2e = bench_end_to_end()
    for k, v in e2e["latency_ms"].items():
        print(f"  {k:>6} = {v} ms")
    print()

    out = {
        "_about": (
            "Phase 2 latency backfill — captures the two PRIMARY-METRICS "
            "fields the day-by-day plan failed to land on Days 9-11."
        ),
        "run_date": "2026-05-14",
        "host_python": sys.version.split()[0],
        "host_platform": sys.platform,
        "benches": {
            "decision_pipeline": dec,
            "end_to_end": e2e,
        },
    }

    out_path = ROOT / "results" / "phase2_latency_backfill.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
