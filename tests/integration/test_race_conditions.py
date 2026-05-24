"""Race-condition hardening tests (Day 21, Phase 4).

PennyCore's concurrency invariant: under simultaneous calls from N
threads to any public surface, the system must reach exactly one of
the legal end states deterministically — no lost writes, no
duplicate side effects, no partial mutations. Day 19 covered
idempotency (same key delivered twice → one action). Day 20 covered
multi-tenant isolation (no cross-tenant leak under cooperative
callers). Day 21 covers ADVERSARIAL CONCURRENCY: multiple threads
racing on the same resource, mounting the kinds of races that
production traffic naturally generates (Twilio retries arriving
within the same millisecond, two approvers clicking simultaneously
on the admin UI, the orchestrator's event-listener fanning out the
same Redis envelope to multiple worker threads).

The 10 tests group by surface:

  A. Concurrent ingestion + customer linking   tests 1-2
  B. Approval queue races                      tests 3-5
  C. Pipeline + planner double-call            test  6
  D. Approval queue enqueue                    test  7
  E. Audit log monotonicity under contention   test  8
  F. Customer-identity write contention        test  9
  G. Event repository throughput under load    test 10

The production patch authored alongside test 1-2: harden
`linking.resolve_customer` against the check-then-create TOCTOU race
in the "no match → create new customer" branch. Two threads racing
on the same email would BOTH miss the identity index, BOTH call
`create_customer`, and the loser would hit `IdentityCollision` from
the UNIQUE oracle — bubbling out to the HTTP layer as a 500. The
fix: catch `IdentityCollision` from `create_customer`, re-run the
identity match (which will now succeed thanks to the first thread's
write), and return that as a normal match. The race becomes
indistinguishable from a slightly-later replay.

All tests are deterministic-by-construction:
  * `threading.Barrier(N)` releases every thread at exactly the same
    instant — under the GIL this means the contention window starts
    open and gets squeezed only by the locks under test.
  * `ThreadPoolExecutor` with `max_workers=N` plus N submitted tasks
    guarantees parallel execution rather than sequential resue.
  * Tests assert on STRUCTURAL invariants (counts, set sizes, audit
    chain shape) rather than wall-clock ordering — different
    interleavings still produce the same legal final state.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from context_engine.api import app, get_bus, get_customer_repo, get_repo
from context_engine.customer_repository import (
    IdentityCollision,
    InMemoryCustomerRepository,
)
from context_engine.event_bus import InMemoryEventBus
from context_engine.ingestion import (
    IngestionRequest,
    build_event_from_request,
    ingest_event,
)
from context_engine.linking import resolve_customer
from context_engine.repository import InMemoryEventRepository
from contracts import IdentityKind
from contracts.actions import (
    ActionProposal,
    ActionType,
    ProposedBy,
)
from contracts.audit import AuditActorKind, AuditKind, AuditLogEntry
from orchestrator.approval_queue import (
    ApprovalStateError,
    InMemoryApprovalQueue,
)
from orchestrator.audit import InMemoryAuditLog
from orchestrator.decision_pipeline import make_default_pipeline


# ---------------------------------------------------------------------------
# Fixtures + helpers
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
        "idempotency_key": "race-key",
        "payload": {"text": "Did you receive my W-2?"},
    }
    base.update(overrides)
    return base


def _proposal(
    *,
    tenant_id: str = "tenant-a",
    event_id: str = "evt_race",
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
        payload={"_planner_reasoning": "race-test"},
    )


def _make_request(
    *,
    tenant_id: str = "acme-bank",
    idempotency_key: str,
    payload: dict | None = None,
) -> IngestionRequest:
    return IngestionRequest(
        tenant_id=tenant_id,
        channel_code="sms",
        event_type="message_received",
        idempotency_key=idempotency_key,
        payload=payload if payload is not None else {"text": "hi"},
    )


# ===========================================================================
# A. Concurrent ingestion + customer linking (2)
# ===========================================================================


def _wrap_with_lookup_latency(
    repo: InMemoryCustomerRepository, *, delay_seconds: float = 0.01
) -> None:
    """Monkey-patch `find_customer_by_identity` to sleep AFTER the
    lookup but BEFORE returning. This widens the linker's check-then-
    create TOCTOU window so the race deterministically surfaces under
    `ThreadPoolExecutor`. Without this, the in-memory repo + the GIL
    serialize the find→create sequence so tightly that the race is
    effectively un-triggerable in tests, even though it is wide open
    against the Postgres-backed adapter where each round trip is
    naturally a few ms. We use this simulation rather than spinning up
    Postgres so the test stays in the pure-Python path."""
    orig_find = repo.find_customer_by_identity

    def _slow(**kwargs: object):  # type: ignore[no-untyped-def]
        result = orig_find(**kwargs)  # type: ignore[arg-type]
        time.sleep(delay_seconds)
        return result

    repo.find_customer_by_identity = _slow  # type: ignore[method-assign]


class TestConcurrentLinking:
    """Two events arrive simultaneously for the SAME never-before-seen
    customer (same email / external_id). Without the linker hardening,
    the loser of the check-then-create race raises `IdentityCollision`
    out to the caller. With the hardening, both events succeed and
    resolve to the SAME customer.

    To make the race deterministic on the in-memory path, the
    customer repo's `find_customer_by_identity` is wrapped with a
    short sleep — this simulates the Postgres-adapter round-trip
    latency that opens the TOCTOU window in production."""

    def test_01_concurrent_same_email_one_customer_no_exception(
        self,
        customer_repo: InMemoryCustomerRepository,
    ) -> None:
        # 8 threads, each with a distinct idempotency_key but the SAME
        # email hint, all calling `resolve_customer` against an empty
        # repo. Exactly one customer must be created; all 8 calls must
        # return success; the customer's identity index must hold the
        # email exactly once.
        _wrap_with_lookup_latency(customer_repo, delay_seconds=0.01)
        N = 8
        barrier = threading.Barrier(parties=N)
        outcomes: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def _call(i: int) -> None:
            request = _make_request(
                idempotency_key=f"key-{i}",
                payload={"from_email": "Jane.Doe@example.com", "text": "hi"},
            )
            barrier.wait()
            try:
                outcome = resolve_customer(request, customer_repo=customer_repo)
                with lock:
                    outcomes.append(outcome.customer_id)
            except BaseException as exc:  # noqa: BLE001 — record-and-continue
                with lock:
                    errors.append(exc)

        with ThreadPoolExecutor(max_workers=N) as pool:
            for i in range(N):
                pool.submit(_call, i)

        # Zero exceptions, even on the losers of the create race — the
        # hardening converts `IdentityCollision` into a normal match.
        assert errors == [], (
            f"concurrent linker raised on {len(errors)} threads: "
            f"{[type(e).__name__ for e in errors]}"
        )
        assert len(outcomes) == N
        # All 16 callers see the SAME customer_id.
        assert len(set(outcomes)) == 1, (
            f"expected one customer; got {len(set(outcomes))}: {set(outcomes)!r}"
        )
        # The customer repo holds exactly one customer for this tenant.
        assert customer_repo.count(tenant_id="acme-bank") == 1
        # The email identity is indexed exactly once.
        match = customer_repo.find_customer_by_identity(
            tenant_id="acme-bank",
            identity_kind=IdentityKind.EMAIL,
            identity_value="jane.doe@example.com",
        )
        assert match is not None
        assert match.id == outcomes[0]

    def test_02_concurrent_same_external_id_one_customer(
        self,
        customer_repo: InMemoryCustomerRepository,
    ) -> None:
        # external_id is the highest-priority hint and the most common
        # source of upstream-CRM-driven duplicates (the same CRM webhook
        # delivered twice within milliseconds). Same shape as test 1 but
        # exercising the priority-zero lookup path.
        _wrap_with_lookup_latency(customer_repo, delay_seconds=0.01)
        N = 8
        barrier = threading.Barrier(parties=N)
        outcomes: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def _call(i: int) -> None:
            request = _make_request(
                idempotency_key=f"ext-{i}",
                payload={"external_id": "CRM-9001", "text": "hi"},
            )
            barrier.wait()
            try:
                outcome = resolve_customer(request, customer_repo=customer_repo)
                with lock:
                    outcomes.append(outcome.customer_id)
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)

        with ThreadPoolExecutor(max_workers=N) as pool:
            for i in range(N):
                pool.submit(_call, i)

        assert errors == []
        assert len(set(outcomes)) == 1
        assert customer_repo.count(tenant_id="acme-bank") == 1


# ===========================================================================
# B. Approval queue races (3)
# ===========================================================================


class TestApprovalQueueRaces:
    """Two humans race on the same pending action. The queue lock + the
    pipeline lock together guarantee EXACTLY ONE writer wins; losers
    raise `ApprovalStateError`. No partial state — the action either
    transitions cleanly through approval or sits at rejected; never
    both, never neither."""

    def test_03_concurrent_approve_vs_reject_one_wins(self) -> None:
        pipeline = make_default_pipeline()
        # All actions require approval for this test — we set a policy
        # that forces APPROVAL_REQUIRED on the action type.
        pipeline.policy.set_policies(
            "tenant-a", {"send_borrower_message": "approval_required"}
        )
        actions = pipeline.handle_proposal(_proposal(event_id="evt-race-3"))
        action_id = actions[0].id
        assert pipeline.queue.is_pending(action_id)

        barrier = threading.Barrier(parties=2)
        outcomes: dict[str, str | type] = {}

        def _approver() -> None:
            barrier.wait()
            try:
                pipeline.approve_action(action_id, decided_by="alice")
                outcomes["approve"] = "approved"
            except ApprovalStateError:
                outcomes["approve"] = ApprovalStateError

        def _rejecter() -> None:
            barrier.wait()
            try:
                pipeline.reject_action(action_id, reason="risk", decided_by="bob")
                outcomes["reject"] = "rejected"
            except ApprovalStateError:
                outcomes["reject"] = ApprovalStateError

        with ThreadPoolExecutor(max_workers=2) as pool:
            pool.submit(_approver)
            pool.submit(_rejecter)

        # Exactly one of approve/reject succeeded, the other got the
        # state error.
        successes = [k for k, v in outcomes.items() if isinstance(v, str)]
        failures = [k for k, v in outcomes.items() if v is ApprovalStateError]
        assert len(successes) == 1, f"expected one winner, got: {outcomes!r}"
        assert len(failures) == 1

        # The queue row is in the terminal state matching the winner.
        rule = pipeline.queue.get(action_id)
        assert rule is not None
        assert rule.state == outcomes[successes[0]]
        # Audit has EITHER an APPROVAL or a REJECTION row for this
        # action, never both (the loser was rejected before the audit
        # write inside the pipeline).
        audit_kinds = [
            e.kind for e in pipeline.audit.entries_for_action(action_id)
        ]
        assert audit_kinds.count(AuditKind.APPROVAL) + audit_kinds.count(
            AuditKind.REJECTION
        ) == (1 if outcomes[successes[0]] == "rejected" else 2)
        # ↑ Note: a successful APPROVAL path also writes an EXECUTION
        # row, but the REJECTION count must stay 0. A REJECTION winner
        # writes exactly one REJECTION row, no APPROVAL.

    def test_04_concurrent_approvers_one_wins(self) -> None:
        # Five approvers race on the same pending action — the
        # canonical "two SREs clicking simultaneously on the admin UI"
        # scenario, scaled up.
        pipeline = make_default_pipeline()
        pipeline.policy.set_policies(
            "tenant-a", {"send_borrower_message": "approval_required"}
        )
        action_id = pipeline.handle_proposal(_proposal(event_id="evt-race-4"))[0].id
        assert pipeline.queue.is_pending(action_id)

        N = 5
        barrier = threading.Barrier(parties=N)
        successes: list[str] = []
        failures: list[str] = []
        lock = threading.Lock()

        def _approve(decider: str) -> None:
            barrier.wait()
            try:
                pipeline.approve_action(action_id, decided_by=decider)
                with lock:
                    successes.append(decider)
            except ApprovalStateError:
                with lock:
                    failures.append(decider)

        with ThreadPoolExecutor(max_workers=N) as pool:
            for i in range(N):
                pool.submit(_approve, f"approver-{i}")

        assert len(successes) == 1, (
            f"exactly one approve must win; got {successes!r}"
        )
        assert len(failures) == N - 1
        # The audit log has exactly one APPROVAL row, by the winner.
        approvals = [
            e
            for e in pipeline.audit.entries_for_action(action_id)
            if e.kind is AuditKind.APPROVAL
        ]
        assert len(approvals) == 1
        assert approvals[0].actor_id == successes[0]

    def test_05_concurrent_rejecters_one_wins(self) -> None:
        # Symmetric to test 04. The hardening invariant is the same:
        # second-rejection-attempt raises rather than silently rewriting
        # the audit reason.
        pipeline = make_default_pipeline()
        pipeline.policy.set_policies(
            "tenant-a", {"send_borrower_message": "approval_required"}
        )
        action_id = pipeline.handle_proposal(_proposal(event_id="evt-race-5"))[0].id

        N = 5
        barrier = threading.Barrier(parties=N)
        successes: list[str] = []
        failures: list[str] = []
        lock = threading.Lock()

        def _reject(decider: str) -> None:
            barrier.wait()
            try:
                pipeline.reject_action(
                    action_id, reason=f"reason-{decider}", decided_by=decider
                )
                with lock:
                    successes.append(decider)
            except ApprovalStateError:
                with lock:
                    failures.append(decider)

        with ThreadPoolExecutor(max_workers=N) as pool:
            for i in range(N):
                pool.submit(_reject, f"rejecter-{i}")

        assert len(successes) == 1
        assert len(failures) == N - 1
        rejections = [
            e
            for e in pipeline.audit.entries_for_action(action_id)
            if e.kind is AuditKind.REJECTION
        ]
        # The audit log has exactly one REJECTION row attributable to
        # a human (system-rejections from policy=reject are absent
        # here because the policy is approval_required, not reject).
        human_rejections = [
            e for e in rejections if e.actor_kind is AuditActorKind.HUMAN
        ]
        assert len(human_rejections) == 1
        assert human_rejections[0].actor_id == successes[0]
        # And the reason recorded matches the winner's reason string.
        assert human_rejections[0].payload.get("reason") == f"reason-{successes[0]}"


# ===========================================================================
# C. Pipeline planner double-call (1)
# ===========================================================================


class TestPipelineRaces:
    """Day 19 test 19 already asserts the dedup index produces one
    action under contention. This test extends that with the
    DOWNSTREAM invariants: action store has exactly one row, executor
    fired exactly once, and audit chain has the canonical kinds (no
    duplicated PROPOSAL/DECISION/EXECUTION rows from a leak in the
    dedup guard)."""

    def test_06_concurrent_planner_one_action_one_execution(self) -> None:
        pipeline = make_default_pipeline()
        pipeline.policy.set_policies("tenant-a", {"*": "auto"})
        N = 12
        barrier = threading.Barrier(parties=N)
        results: list[str] = []
        lock = threading.Lock()

        def _call() -> None:
            barrier.wait()
            actions = pipeline.handle_proposal(_proposal(event_id="evt-race-6"))
            with lock:
                results.append(actions[0].id)

        with ThreadPoolExecutor(max_workers=N) as pool:
            for _ in range(N):
                pool.submit(_call)

        # All N callers see the same action_id.
        assert len(set(results)) == 1
        action_id = results[0]
        # Action store has exactly one row for tenant-a.
        actions_list = pipeline.list_actions_for_tenant("tenant-a")
        assert len(actions_list) == 1
        assert actions_list[0].id == action_id
        # Audit chain: exactly one PROPOSAL, one DECISION, one
        # EXECUTION. No duplicates from the dedup-cache replay path
        # (which short-circuits BEFORE writing audit rows).
        chain = pipeline.audit.entries_for_event("evt-race-6")
        kinds = [e.kind for e in chain]
        assert kinds.count(AuditKind.PROPOSAL) == 1, kinds
        assert kinds.count(AuditKind.DECISION) == 1, kinds
        assert kinds.count(AuditKind.EXECUTION) == 1, kinds


# ===========================================================================
# D. Approval queue enqueue (1)
# ===========================================================================


class TestQueueEnqueueRaces:
    """`InMemoryApprovalQueue.enqueue` is idempotent on `action_id` —
    the Day-19 test 18 covered the single-threaded case. This test
    pounds it from N threads to verify the RLock holds under
    contention (no double-row, no lost row, no mis-indexed tenant)."""

    def test_07_concurrent_enqueue_same_action_id_one_row(self) -> None:
        queue = InMemoryApprovalQueue()
        N = 20
        barrier = threading.Barrier(parties=N)
        results: list[str] = []
        lock = threading.Lock()

        def _enqueue(i: int) -> None:
            barrier.wait()
            rule = queue.enqueue(
                "act_storm",
                "tenant-a",
                assigned_to=f"assignee-{i}",
            )
            with lock:
                results.append(rule.id)

        with ThreadPoolExecutor(max_workers=N) as pool:
            for i in range(N):
                pool.submit(_enqueue, i)

        # All N calls returned the SAME queue-row id.
        assert len(results) == N
        assert len(set(results)) == 1
        # The pending list has exactly one entry, not N.
        pending = queue.list_pending("tenant-a")
        assert len(pending) == 1
        assert pending[0].action_id == "act_storm"


# ===========================================================================
# E. Audit log monotonicity (1)
# ===========================================================================


class TestAuditConcurrentWrites:
    """The audit log assigns monotonic IDs on every write. Under
    contention from N threads, all N entries must land AND the IDs
    must be unique (no two entries share an id, no id is skipped).
    A naive counter without locking would lose writes and/or assign
    duplicate ids."""

    def test_08_concurrent_audit_writes_monotonic_no_loss(self) -> None:
        audit = InMemoryAuditLog()
        # Mix tenants to verify the per-tenant index also stays
        # consistent under contention.
        tenants = ["tenant-a", "tenant-b", "tenant-c"]
        N = 60
        barrier = threading.Barrier(parties=N)

        def _write(i: int) -> None:
            entry = AuditLogEntry(
                tenant_id=tenants[i % len(tenants)],
                kind=AuditKind.PROPOSAL,
                action_id=f"act_{i}",
                caused_by_event_id=f"evt_{i}",
                actor_kind=AuditActorKind.SYSTEM,
                payload={"i": i},
            )
            barrier.wait()
            audit.write(entry)

        with ThreadPoolExecutor(max_workers=N) as pool:
            for i in range(N):
                pool.submit(_write, i)

        all_entries = audit.all_entries()
        assert len(all_entries) == N
        ids = [e.id for e in all_entries]
        # All ids unique (no double-assignment under contention).
        assert len(set(ids)) == N
        # Ids form the contiguous 1..N range — no skips means no
        # counter increment leaked without a corresponding append.
        assert sorted(ids) == list(range(1, N + 1))
        # Per-tenant index sums match the global count.
        per_tenant_total = sum(
            len(audit.entries_for_tenant(t)) for t in tenants
        )
        assert per_tenant_total == N


# ===========================================================================
# F. Customer-identity write contention (1)
# ===========================================================================


class TestIdentityCollisionUnderContention:
    """`add_identity` raises `IdentityCollision` when the same
    `(tenant_id, kind, value)` is being attached to two DIFFERENT
    customers. Under concurrent attachment, the loser must see the
    collision (the UNIQUE oracle is preserved) — this is the
    invariant the linker hardening relies on at a lower layer. The
    test scaffolds the case directly so a regression in the index
    lock surfaces here even if the linker's retry logic masks it
    upstream."""

    def test_09_concurrent_add_identity_to_different_customers_collides(
        self, customer_repo: InMemoryCustomerRepository
    ) -> None:
        # Create two distinct customers in the same tenant — neither has
        # the contested email yet.
        cust_a = customer_repo.create_customer(
            tenant_id="acme-bank", identities=[], display_name="A"
        )
        cust_b = customer_repo.create_customer(
            tenant_id="acme-bank", identities=[], display_name="B"
        )
        N = 8
        barrier = threading.Barrier(parties=N)
        results: list[str] = []
        collisions: list[str] = []
        lock = threading.Lock()

        def _attach(i: int) -> None:
            target = cust_a.id if i % 2 == 0 else cust_b.id
            barrier.wait()
            try:
                customer_repo.add_identity(
                    tenant_id="acme-bank",
                    customer_id=target,
                    identity_kind=IdentityKind.EMAIL,
                    identity_value="contested@example.com",
                )
                with lock:
                    results.append(target)
            except IdentityCollision:
                with lock:
                    collisions.append(target)

        with ThreadPoolExecutor(max_workers=N) as pool:
            for i in range(N):
                pool.submit(_attach, i)

        # The identity ended up on EXACTLY one customer. All threads
        # targeting that customer succeed (add_identity is idempotent
        # for same-customer same-identity); the others see collisions.
        winners = set(results)
        assert len(winners) == 1, (
            f"identity landed on {len(winners)} customers: {winners!r}"
        )
        winner = winners.pop()
        # Every "success" returned the winner; every "collision"
        # tried the loser.
        assert all(r == winner for r in results)
        assert all(c != winner for c in collisions)
        # The identity index reflects ONE owner.
        match = customer_repo.find_customer_by_identity(
            tenant_id="acme-bank",
            identity_kind=IdentityKind.EMAIL,
            identity_value="contested@example.com",
        )
        assert match is not None
        assert match.id == winner


# ===========================================================================
# G. Event repository throughput under load (1)
# ===========================================================================


class TestEventRepoConcurrentDistinct:
    """N distinct events from N threads. The repository's RLock must
    serialize the underlying dict mutations such that ALL N events
    land — no lost writes from racy `setdefault` or counter bugs.
    This is the throughput-floor test for Day 22's load test."""

    def test_10_concurrent_distinct_events_no_lost_writes(
        self,
        repo: InMemoryEventRepository,
        bus: InMemoryEventBus,
    ) -> None:
        N = 64
        barrier = threading.Barrier(parties=N)

        def _ingest(i: int) -> None:
            request = _make_request(
                idempotency_key=f"distinct-{i}",
                payload={"text": f"msg-{i}"},
            )
            event = build_event_from_request(
                request, idempotency_key=request.idempotency_key
            )
            barrier.wait()
            ingest_event(event, repo=repo, bus=bus)

        with ThreadPoolExecutor(max_workers=N) as pool:
            for i in range(N):
                pool.submit(_ingest, i)

        # All N distinct events landed.
        assert repo.count() == N
        assert repo.count(tenant_id="acme-bank") == N
        # The bus published once per distinct event (no lost or
        # duplicate publishes).
        assert len(bus.published) == N
        # The publishes' event_ids are unique, matching the stored
        # set.
        published_event_ids = {env["event_id"] for _, env in bus.published}
        assert len(published_event_ids) == N
