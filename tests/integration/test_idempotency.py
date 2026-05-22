"""Idempotency hardening tests (Day 19, Phase 4).

PennyCore's idempotency invariant (SYSTEM_DESIGN §5.1, rule 16):
each unique customer-facing event MUST cause exactly one action,
exactly one publish, exactly one set of audit rows — regardless of
how many times it is delivered or how concurrent the delivery is.
Two independent dedup oracles enforce this:

  * Context-engine layer: `(tenant_id, idempotency_key)` UNIQUE on
    the `events` table. `InMemoryEventRepository.upsert_event` mirrors
    the DB constraint; the Postgres adapter (Day 6+) defers to the
    SQL UNIQUE. A replay returns the canonical stored event, never
    re-publishes on the bus, and never re-runs the customer linker.
  * Orchestrator layer: `(tenant_id, event_id)` dedup on the
    `DecisionPipeline`. A replayed event short-circuits to the cached
    `Action[]` instead of re-running planner + policy + executor.

This module asserts both oracles, plus the human-decision boundary
(double-approve / approve-without-enqueue), plus concurrency safety.

The 20 tests group by surface:

  A. ingest_event pure logic            tests 1-5
  B. POST /events HTTP surface          tests 6-10
  C. DecisionPipeline.handle_proposal   tests 11-16
  D. Audit + approval-queue invariants  tests 17-18
  E. Concurrency                        tests 19-20

Tests are deterministic — no real LLM calls, no Postgres, no Redis.
The concurrency tests use threads against the in-memory adapters,
which already carry RLock guards (see InMemoryEventRepository and
DecisionPipeline). Day 21 (race-condition tests) extends this with
DB-backed advisory-lock scenarios.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from context_engine.api import app, get_bus, get_customer_repo, get_repo
from context_engine.customer_repository import InMemoryCustomerRepository
from context_engine.event_bus import InMemoryEventBus, channel_for
from context_engine.ingestion import (
    IngestionRequest,
    build_event_from_request,
    ingest_event,
)
from context_engine.repository import InMemoryEventRepository
from contracts.actions import (
    ActionProposal,
    ActionStatus,
    ActionType,
    ProposedBy,
)
from contracts.audit import AuditKind
from orchestrator.approval_queue import (
    ApprovalNotFoundError,
    ApprovalStateError,
    InMemoryApprovalQueue,
)
from orchestrator.audit import InMemoryAuditLog
from orchestrator.decision_pipeline import DecisionPipeline, make_default_pipeline
from orchestrator.executor import ActionExecutor, DEFAULT_EXECUTORS
from orchestrator.policy import DeclarativePolicyEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo() -> InMemoryEventRepository:
    return InMemoryEventRepository()


@pytest.fixture
def bus() -> InMemoryEventBus:
    return InMemoryEventBus()


@pytest.fixture
def customer_repo() -> InMemoryCustomerRepository:
    return InMemoryCustomerRepository()


@pytest.fixture
def client(
    repo: InMemoryEventRepository,
    bus: InMemoryEventBus,
    customer_repo: InMemoryCustomerRepository,
) -> Iterator[TestClient]:
    app.dependency_overrides[get_repo] = lambda: repo
    app.dependency_overrides[get_bus] = lambda: bus
    app.dependency_overrides[get_customer_repo] = lambda: customer_repo
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _ingest_body(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tenant_id": "acme-bank",
        "channel_code": "sms",
        "event_type": "message_received",
        "idempotency_key": "msg-dup-001",
        "payload": {"text": "Did you receive my W-2?"},
    }
    base.update(overrides)
    return base


def _make_event(
    repo: InMemoryEventRepository,
    *,
    tenant_id: str = "acme-bank",
    idempotency_key: str = "k1",
    payload: dict | None = None,
):
    request = IngestionRequest(
        tenant_id=tenant_id,
        channel_code="sms",
        event_type="message_received",
        idempotency_key=idempotency_key,
        payload=payload if payload is not None else {"text": "hello"},
    )
    return build_event_from_request(request, idempotency_key=idempotency_key)


def _proposal(
    *,
    tenant_id: str = "tenant-a",
    event_id: str = "evt_dup",
    proposal_id: str | None = None,
    action_type: ActionType = ActionType.SEND_BORROWER_MESSAGE,
) -> ActionProposal:
    return ActionProposal(
        id=proposal_id or f"prop_{event_id}",
        tenant_id=tenant_id,
        event_id=event_id,
        customer_id=None,
        action_type=action_type,
        proposed_by=ProposedBy.LLM,
        payload={"_planner_reasoning": "duplicate-event test"},
    )


# ===========================================================================
# A. ingest_event pure-logic idempotency (5)
# ===========================================================================


class TestIngestionPureLogic:
    """The `(tenant_id, idempotency_key)` UNIQUE oracle, exercised
    against `ingest_event` directly so the HTTP layer can't mask
    behavior at the repository boundary."""

    def test_01_replay_returns_same_event_with_created_false(
        self, repo: InMemoryEventRepository, bus: InMemoryEventBus
    ) -> None:
        first = _make_event(repo, idempotency_key="msg-1")
        r1 = ingest_event(first, repo=repo, bus=bus)
        # Build a DIFFERENT in-memory Event with the same idem key — the
        # repository must collapse them.
        second = _make_event(repo, idempotency_key="msg-1")
        assert second.id != first.id  # confirm we constructed distinct IDs
        r2 = ingest_event(second, repo=repo, bus=bus)
        assert r1.created is True
        assert r2.created is False
        assert r2.event.id == first.id  # canonical row wins
        assert repo.count() == 1

    def test_02_replay_does_not_republish_on_bus(
        self, repo: InMemoryEventRepository, bus: InMemoryEventBus
    ) -> None:
        first = _make_event(repo, idempotency_key="msg-2")
        ingest_event(first, repo=repo, bus=bus)
        ingest_event(_make_event(repo, idempotency_key="msg-2"), repo=repo, bus=bus)
        ingest_event(_make_event(repo, idempotency_key="msg-2"), repo=repo, bus=bus)
        # Three calls, one publish — the bus is the orchestrator's
        # source of work, and a replayed event must NOT generate a
        # second decision pipeline pass.
        assert len(bus.published) == 1
        channel, envelope = bus.published[0]
        assert channel == channel_for("acme-bank")
        assert envelope["event_id"] == first.id

    def test_03_replay_returns_canonical_payload_not_inbound(
        self, repo: InMemoryEventRepository, bus: InMemoryEventBus
    ) -> None:
        # The stored row is the source of truth — a replay with a
        # tampered payload must NOT overwrite the stored payload.
        original = _make_event(repo, idempotency_key="msg-3", payload={"text": "real"})
        ingest_event(original, repo=repo, bus=bus)

        tampered = _make_event(
            repo, idempotency_key="msg-3", payload={"text": "TAMPERED"}
        )
        result = ingest_event(tampered, repo=repo, bus=bus)
        assert result.created is False
        assert result.event.payload == {"text": "real"}
        # Repository copy is the canonical record.
        stored = repo.get_event_by_idempotency(
            tenant_id="acme-bank", idempotency_key="msg-3"
        )
        assert stored is not None
        assert stored.payload == {"text": "real"}

    def test_04_different_tenants_same_key_are_independent(
        self, repo: InMemoryEventRepository, bus: InMemoryEventBus
    ) -> None:
        # The UNIQUE oracle is keyed by `(tenant_id, idempotency_key)` —
        # two tenants colliding on the SAME idem key get two events.
        ingest_event(
            _make_event(repo, tenant_id="bank-a", idempotency_key="shared"),
            repo=repo,
            bus=bus,
        )
        ingest_event(
            _make_event(repo, tenant_id="bank-b", idempotency_key="shared"),
            repo=repo,
            bus=bus,
        )
        assert repo.count(tenant_id="bank-a") == 1
        assert repo.count(tenant_id="bank-b") == 1
        # And critically, bank-a cannot see bank-b's row even though the
        # idempotency key is identical.
        a_view = repo.get_event_by_idempotency(
            tenant_id="bank-a", idempotency_key="shared"
        )
        b_view = repo.get_event_by_idempotency(
            tenant_id="bank-b", idempotency_key="shared"
        )
        assert a_view is not None and b_view is not None
        assert a_view.id != b_view.id

    def test_05_thousand_replays_collapse_to_one_event(
        self, repo: InMemoryEventRepository, bus: InMemoryEventBus
    ) -> None:
        # Stress the dedup oracle with a high replay multiplier so a
        # subtle counter bug (e.g. "republish every Nth call") would
        # surface deterministically.
        for _ in range(1000):
            ingest_event(
                _make_event(repo, idempotency_key="msg-storm"),
                repo=repo,
                bus=bus,
            )
        assert repo.count() == 1
        assert len(bus.published) == 1


# ===========================================================================
# B. POST /events HTTP-layer idempotency (5)
# ===========================================================================


class TestIngestionHTTP:
    """The same dedup oracle behind the HTTP boundary — plus the
    header-vs-body reconciliation rules from api.py."""

    def test_06_replay_returns_200_after_202(
        self, client: TestClient, repo: InMemoryEventRepository
    ) -> None:
        first = client.post("/events", json=_ingest_body())
        replay = client.post("/events", json=_ingest_body())
        assert first.status_code == 202
        assert replay.status_code == 200
        assert replay.json()["event_id"] == first.json()["event_id"]
        assert replay.json()["deduped"] is True
        assert repo.count() == 1

    def test_07_missing_key_anywhere_returns_400(self, client: TestClient) -> None:
        body = _ingest_body()
        body.pop("idempotency_key")
        # No body field, no header → 400, NOT a silent UUID-fill.
        resp = client.post("/events", json=body)
        assert resp.status_code == 400
        assert "idempotency_key" in resp.json()["detail"]

    def test_08_header_body_disagreement_returns_400(
        self, client: TestClient, repo: InMemoryEventRepository
    ) -> None:
        resp = client.post(
            "/events",
            json=_ingest_body(idempotency_key="body-key"),
            headers={"X-Idempotency-Key": "header-key"},
        )
        assert resp.status_code == 400
        assert "disagree" in resp.json()["detail"].lower()
        # AND no partial write — the disagreement raises BEFORE the
        # repository is touched.
        assert repo.count() == 0

    def test_09_header_only_dedupes_just_like_body(
        self, client: TestClient, repo: InMemoryEventRepository, bus: InMemoryEventBus
    ) -> None:
        body = _ingest_body()
        body.pop("idempotency_key")
        first = client.post(
            "/events", json=body, headers={"X-Idempotency-Key": "via-header"}
        )
        replay = client.post(
            "/events", json=body, headers={"X-Idempotency-Key": "via-header"}
        )
        assert first.status_code == 202
        assert replay.status_code == 200
        assert replay.json()["event_id"] == first.json()["event_id"]
        assert repo.count() == 1
        assert len(bus.published) == 1

    def test_10_replay_short_circuits_linker_run(
        self,
        client: TestClient,
        customer_repo: InMemoryCustomerRepository,
        bus: InMemoryEventBus,
    ) -> None:
        # A replay must NOT pass through the linker, otherwise a tampered
        # replay payload (different from_email) would pollute the
        # customer_identities table. This is the api.py replay
        # short-circuit invariant.
        first = client.post(
            "/events",
            json=_ingest_body(payload={"from_email": "jane@acme.com", "text": "hi"}),
        )
        assert first.status_code == 202
        cid = first.json()["customer_id"]
        idents_before = customer_repo.list_identities(
            tenant_id="acme-bank", customer_id=cid
        )
        replay = client.post(
            "/events",
            json=_ingest_body(
                payload={"from_email": "ATTACKER@evil.com", "text": "hi"}
            ),
        )
        assert replay.status_code == 200
        # Customer identity table unchanged — replay never ran linker.
        idents_after = customer_repo.list_identities(
            tenant_id="acme-bank", customer_id=cid
        )
        assert {i.identity_value for i in idents_after} == {
            i.identity_value for i in idents_before
        }
        # No second publish either.
        assert len(bus.published) == 1


# ===========================================================================
# C. DecisionPipeline.handle_proposal idempotency (6)
# ===========================================================================


class TestDecisionPipelineDedup:
    """The orchestrator's `(tenant_id, event_id)` dedup index."""

    def test_11_replay_returns_same_actions_no_new_audit(self) -> None:
        pipeline = make_default_pipeline()
        pipeline.policy.set_policies("tenant-a", {"*": "auto"})

        first = pipeline.handle_proposal(_proposal(event_id="evt-replay-1"))
        first_ids = [a.id for a in first]
        audit_before = len(pipeline.audit.all_entries())

        # Same (tenant_id, event_id) — different proposal_id is irrelevant.
        second = pipeline.handle_proposal(
            _proposal(event_id="evt-replay-1", proposal_id="prop_OTHER")
        )
        assert [a.id for a in second] == first_ids
        # No NEW audit rows on the replay — the dedup short-circuit
        # returns the cached actions before writing anything.
        assert len(pipeline.audit.all_entries()) == audit_before

    def test_12_dedup_is_tenant_scoped(self) -> None:
        pipeline = make_default_pipeline()
        pipeline.policy.set_policies("tenant-a", {"*": "auto"})
        pipeline.policy.set_policies("tenant-b", {"*": "auto"})

        a = pipeline.handle_proposal(
            _proposal(tenant_id="tenant-a", event_id="evt-shared")
        )
        b = pipeline.handle_proposal(
            _proposal(tenant_id="tenant-b", event_id="evt-shared")
        )
        # Same event_id under different tenants → two distinct actions.
        assert a[0].id != b[0].id
        assert a[0].tenant_id == "tenant-a"
        assert b[0].tenant_id == "tenant-b"

    def test_13_auto_replay_does_not_re_invoke_executor(self) -> None:
        # Inject a counting executor table — every invocation increments
        # the counter for the dispatched action_type. After the replay we
        # must still see exactly one call.
        call_counts: dict[ActionType, int] = {}

        def _make_counting_handler(at: ActionType):
            real = DEFAULT_EXECUTORS[at]

            def _h(action):
                call_counts[at] = call_counts.get(at, 0) + 1
                return real(action)

            return _h

        counting_table = {
            at: _make_counting_handler(at) for at in DEFAULT_EXECUTORS
        }
        pipeline = DecisionPipeline(
            policy=DeclarativePolicyEngine(),
            queue=InMemoryApprovalQueue(),
            audit_log=InMemoryAuditLog(),
            executor=ActionExecutor(executor_table=counting_table),
        )
        pipeline.policy.set_policies("tenant-a", {"*": "auto"})

        pipeline.handle_proposal(_proposal(event_id="evt-once"))
        pipeline.handle_proposal(_proposal(event_id="evt-once"))
        pipeline.handle_proposal(_proposal(event_id="evt-once"))

        assert call_counts == {ActionType.SEND_BORROWER_MESSAGE: 1}

    def test_14_approval_required_replay_no_second_queue_row(self) -> None:
        pipeline = make_default_pipeline()
        pipeline.policy.set_policies("tenant-a", {"*": "approval_required"})

        first = pipeline.handle_proposal(_proposal(event_id="evt-queued"))
        action_id = first[0].id
        assert pipeline.queue.is_pending(action_id)
        assert len(pipeline.queue.list_pending("tenant-a")) == 1

        pipeline.handle_proposal(_proposal(event_id="evt-queued"))
        pipeline.handle_proposal(_proposal(event_id="evt-queued"))
        # Queue depth unchanged — a duplicate event must not double-
        # enqueue (otherwise an approver would resolve one row and the
        # other would still page someone).
        pending = pipeline.queue.list_pending("tenant-a")
        assert len(pending) == 1
        assert pending[0].action_id == action_id

    def test_15_double_approve_raises_state_error(self) -> None:
        pipeline = make_default_pipeline()
        pipeline.policy.set_policies("tenant-a", {"*": "approval_required"})
        action = pipeline.handle_proposal(_proposal(event_id="evt-2x-approve"))[0]
        pipeline.approve_action(action.id, decided_by="alice")
        # Second approve on a resolved row is an explicit error — the
        # state machine forbids re-decisioning.
        with pytest.raises(ApprovalStateError):
            pipeline.approve_action(action.id, decided_by="alice")
        # AND a reject after approve raises too.
        with pytest.raises(ApprovalStateError):
            pipeline.reject_action(action.id, reason="late", decided_by="bob")

    def test_16_approve_without_enqueue_raises_not_found(self) -> None:
        pipeline = make_default_pipeline()
        pipeline.policy.set_policies("tenant-a", {"*": "auto"})
        # Auto-executed actions never enter the queue. Trying to approve
        # one must surface ApprovalNotFoundError — silently succeeding
        # would let a buggy admin UI mark an already-executed action as
        # "approved" and corrupt the audit trail.
        executed = pipeline.handle_proposal(_proposal(event_id="evt-auto"))[0]
        assert executed.status is ActionStatus.EXECUTED
        with pytest.raises(ApprovalNotFoundError):
            pipeline.approve_action(executed.id, decided_by="alice")


# ===========================================================================
# D. Audit + approval-queue invariants (2)
# ===========================================================================


class TestAuditAndQueueInvariants:
    def test_17_replay_audit_chain_unchanged(self) -> None:
        # Audit chain for an event_id should be IMMUTABLE on replay —
        # the dedup short-circuit MUST NOT write extra PROPOSAL /
        # DECISION / EXECUTION rows. A compliance officer reconstructing
        # the "why" must see a single chain per event.
        pipeline = make_default_pipeline()
        pipeline.policy.set_policies("tenant-a", {"*": "auto"})
        first = pipeline.handle_proposal(_proposal(event_id="evt-once-only"))[0]

        chain_initial = [e.kind for e in pipeline.audit.entries_for_action(first.id)]
        assert chain_initial.count(AuditKind.PROPOSAL) == 1
        assert chain_initial.count(AuditKind.DECISION) == 1
        assert chain_initial.count(AuditKind.EXECUTION) == 1

        for _ in range(5):
            pipeline.handle_proposal(_proposal(event_id="evt-once-only"))

        chain_after = [e.kind for e in pipeline.audit.entries_for_action(first.id)]
        assert chain_after == chain_initial

        # And the action's event-scoped audit view shows one chain too.
        event_chain = pipeline.audit.entries_for_event("evt-once-only")
        kinds = [e.kind for e in event_chain]
        assert kinds.count(AuditKind.PROPOSAL) == 1
        assert kinds.count(AuditKind.DECISION) == 1
        assert kinds.count(AuditKind.EXECUTION) == 1

    def test_18_approval_queue_enqueue_idempotent_on_action_id(self) -> None:
        # The approval queue itself MUST be idempotent on action_id —
        # the DecisionPipeline already prevents double-enqueue at the
        # dedup layer, but enqueue is part of the public Protocol and
        # Day-21 race tests will hit it from multiple threads.
        queue = InMemoryApprovalQueue()
        first = queue.enqueue("act_A", "tenant-a")
        second = queue.enqueue("act_A", "tenant-a")
        third = queue.enqueue("act_A", "tenant-a", assigned_to="alice")
        # All three calls return the same row (no new id) and the
        # tenant's pending list has length one.
        assert first.id == second.id == third.id
        assert len(queue.list_pending("tenant-a")) == 1


# ===========================================================================
# E. Concurrency (2)
# ===========================================================================


class TestConcurrentDuplicates:
    """The repository and pipeline carry RLock guards. Hammer them
    from multiple threads with the same (tenant_id, key) to verify
    no double-creation slips through."""

    def test_19_concurrent_pipeline_handlers_same_event_id_one_action(self) -> None:
        pipeline = make_default_pipeline()
        pipeline.policy.set_policies("tenant-a", {"*": "auto"})
        barrier = threading.Barrier(parties=10)
        results = []

        def _call() -> None:
            barrier.wait()
            actions = pipeline.handle_proposal(_proposal(event_id="evt-race"))
            results.append(actions[0].id)

        with ThreadPoolExecutor(max_workers=10) as pool:
            for _ in range(10):
                pool.submit(_call)
        # 10 threads, 10 returned action IDs, all identical — the
        # dedup index guarantees a single materialized action even
        # under contention.
        assert len(results) == 10
        assert len(set(results)) == 1
        # And the pipeline stored exactly one action for the tenant.
        assert len(pipeline.list_actions_for_tenant("tenant-a")) == 1
        # Audit chain still has one PROPOSAL row.
        chain = pipeline.audit.entries_for_event("evt-race")
        kinds = [e.kind for e in chain]
        assert kinds.count(AuditKind.PROPOSAL) == 1

    def test_20_concurrent_http_posts_same_idempotency_key_one_event(
        self, client: TestClient, repo: InMemoryEventRepository, bus: InMemoryEventBus
    ) -> None:
        # 8 concurrent POSTs with the SAME idempotency_key. Exactly
        # one must return 202 (created); the rest must return 200
        # (replay). The repository must hold exactly one event and
        # the bus must hold exactly one publish.
        N = 8
        barrier = threading.Barrier(parties=N)
        statuses: list[int] = []
        event_ids: list[str] = []
        lock = threading.Lock()

        def _post() -> None:
            barrier.wait()
            resp = client.post(
                "/events", json=_ingest_body(idempotency_key="storm-key")
            )
            with lock:
                statuses.append(resp.status_code)
                event_ids.append(resp.json()["event_id"])

        with ThreadPoolExecutor(max_workers=N) as pool:
            for _ in range(N):
                pool.submit(_post)

        assert statuses.count(202) == 1, (
            f"expected exactly one 202; got {statuses!r}"
        )
        assert statuses.count(200) == N - 1
        # All threads see the SAME event_id (the canonical row).
        assert len(set(event_ids)) == 1
        assert repo.count() == 1
        assert len(bus.published) == 1
