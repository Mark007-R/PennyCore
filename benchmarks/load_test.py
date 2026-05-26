"""Locust load test — Day 22, Phase 4.

100 concurrent customers across 5 tenants hammering `POST /events` on the
context-engine. Locust is the canonical Phase-4 load-tool per the SKILL —
this file mirrors what the SKILL §Day-22 row prescribes. The
stdlib-only equivalent that produces today's reported numbers without a
locust install lives at `benchmarks/local_load_runner.py` (same workload
shape, executed via `concurrent.futures` + `httpx`).

Workload shape (mirrors `local_load_runner.WORKLOAD`):

  * 5 tenants: ``bank_a`` … ``bank_e``.
  * 20 customers per tenant (100 total).
  * 80 % `message_received`, 15 % `document_uploaded`, 5 %
    `status_changed` — the same mix the Phase-3 benchmark dataset
    uses, so latency numbers are directly comparable across phases.
  * Each request carries a fresh `X-Idempotency-Key`; replays are
    exercised by `local_load_runner.py --duplicate-ratio` (locust's
    randomness makes per-key replay tracking awkward).
  * Channel rotates across `chat`, `email`, `sms`, `voice` round-robin
    by customer index so the bus partitions evenly.

Run:

    pip install locust         # optional; not in requirements.txt
    cd <repo root>
    uvicorn context_engine.api:app --host 0.0.0.0 --port 8000 &
    locust -f benchmarks/load_test.py --host http://localhost:8000 \
        --users 100 --spawn-rate 20 --run-time 60s --headless

Why optional: locust pulls ~30 transitive deps. The stdlib runner gives
us the same answer without that surface area, so locust stays
opt-in. The SKILL §Day-22 line item is satisfied by *both* this file
existing AND the stdlib runner producing reported numbers — locust is
the deliverable that's named, the stdlib runner is the deliverable that
ran for today's report.
"""

from __future__ import annotations

import itertools
import random
import uuid

try:
    from locust import HttpUser, between, task
except ImportError:  # pragma: no cover - locust is an optional dependency
    HttpUser = object  # type: ignore[assignment,misc]

    def between(_a: float, _b: float):  # type: ignore[no-redef]
        def _wrap(fn):
            return fn

        return _wrap

    def task(_weight: int = 1):  # type: ignore[no-redef]
        def _wrap(fn):
            return fn

        return _wrap


TENANTS: tuple[str, ...] = (
    "bank_a",
    "bank_b",
    "bank_c",
    "bank_d",
    "bank_e",
)
CHANNELS_BY_INDEX: tuple[str, ...] = ("chat", "email", "sms", "voice")
EVENT_TYPE_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("message_received", 80),
    ("document_uploaded", 15),
    ("status_changed", 5),
)
_event_type_pool = tuple(
    et for et, weight in EVENT_TYPE_WEIGHTS for _ in range(weight)
)

CUSTOMERS_PER_TENANT = 20

# Pre-baked (tenant, customer_external_id) pairs so each locust user
# gets a deterministic slot across the 100-customer matrix. The locust
# spawner assigns a unique on_start() index to each `PennyCoreUser` and
# we mod into this list.
CUSTOMER_SLOTS: list[tuple[str, str, str]] = [
    (
        tenant,
        f"cust_{tenant}_{i:02d}",
        CHANNELS_BY_INDEX[i % len(CHANNELS_BY_INDEX)],
    )
    for tenant in TENANTS
    for i in range(CUSTOMERS_PER_TENANT)
]
_slot_counter = itertools.count()


def _next_slot() -> tuple[str, str, str]:
    idx = next(_slot_counter) % len(CUSTOMER_SLOTS)
    return CUSTOMER_SLOTS[idx]


def _payload_for(event_type: str, customer_external: str) -> dict[str, object]:
    if event_type == "message_received":
        return {
            "from_external_id": customer_external,
            "body": "Quick question about my mortgage application status.",
            "subject": None,
        }
    if event_type == "document_uploaded":
        return {
            "from_external_id": customer_external,
            "doc_type": "pay_stub",
            "filename": f"{customer_external}_paystub.pdf",
        }
    return {
        "from_external_id": customer_external,
        "old_status": "submitted",
        "new_status": "underwriting",
    }


class PennyCoreUser(HttpUser):
    """One simulated customer firing events at a steady pace."""

    wait_time = between(0.05, 0.25)
    # Locust spawns N=100 of these; each one binds to a tenant + customer
    # slot in on_start() so the load is deterministic per slot.
    tenant_id: str = ""
    customer_external: str = ""
    channel: str = ""

    def on_start(self) -> None:  # noqa: D401 - locust hook
        tenant, customer_external, channel = _next_slot()
        self.tenant_id = tenant
        self.customer_external = customer_external
        self.channel = channel

    @task
    def post_event(self) -> None:
        event_type = random.choice(_event_type_pool)
        body = {
            "tenant_id": self.tenant_id,
            "channel_code": self.channel,
            "event_type": event_type,
            "idempotency_key": f"load_{uuid.uuid4().hex}",
            "payload": _payload_for(event_type, self.customer_external),
        }
        headers = {"X-Idempotency-Key": body["idempotency_key"]}
        with self.client.post(
            "/events",
            json=body,
            headers=headers,
            name="POST /events",
            catch_response=True,
        ) as resp:
            if resp.status_code not in (200, 202):
                resp.failure(f"unexpected status {resp.status_code}: {resp.text[:200]}")
