# orchestrator — decision layer

The decision-maker. Listens for events, asks the LLM what to do, gates
the proposed actions through a tenant-specific policy engine, runs a
human-approval queue (with N-of-M quorum support), executes, and
audits.

This README is a tour of the modules. Architecture picture in
[../ARCHITECTURE.md §1-2](../ARCHITECTURE.md#1-component-map);
prose source-of-truth in
[../docs/SYSTEM_DESIGN.md §2.2](../docs/SYSTEM_DESIGN.md);
policy model and the 4-engine comparison in
[../docs/POLICIES.md](../docs/POLICIES.md).

## Public surface

HTTP routes in `api.py`, all scoped by `tenant_id`:

| Route | What it does |
|-------|--------------|
| `GET  /pending?tenant_id=…` | List pending action proposals waiting for approval |
| `GET  /actions/{action_id}?tenant_id=…` | Retrieve a specific action proposal |
| `POST /approvals/{action_id}/approve` | Approve a pending action (contributes to N-of-M quorum) |
| `POST /approvals/{action_id}/reject` | Reject a pending action |
| `POST /events` (orchestrator-side) | Inject an event directly (test / replay path) |

Plus health probes (`/healthz`, `/readyz`).

The takehome adapter (`takehome/orchestrator/orchestrator_impl.py`)
wraps the same modules to expose the external `AgentOrchestrator` ABC.

## Modules

### Event pipeline
- **`event_listener.py`** — Redis subscriber that picks events off the
  bus context_engine publishes to.
- **`decision_pipeline.py`** — orchestrates one event through the full
  flow: fetch brief → plan → evaluate policy → enqueue or execute →
  audit. Idempotency keyed on `(tenant_id, event_id)`.
- **`planner.py`** — calls the LLM with structured-output tool
  calling. Returns a list of `ActionProposal`s. In mock mode, returns
  rule-based proposals per `event_type`.

### Policy engine — four implementations
All four implement the same `PolicyEngine` Protocol so the Phase-3
benchmark can swap them cleanly.

- **`policy/declarative.py`** — **champion**. YAML / dict rules per
  tenant: `allow:`, `require_approval:`, `reject:`, `require_quorum:`.
  100% correctness on 200 scenarios, 0.6 µs local p50, $0 marginal
  cost, 5/5 auditability + maintainability.
- **`policy/python_rules.py`** — same correctness as declarative but
  worse maintainability (compliance team can't edit Python).
- **`policy/llm_judge.py`** — LLM evaluates "auto-execute or queue?"
  with structured output. Ties declarative on correctness (100%) but
  carries a marginal LLM cost — $0.111/100 in the mock run, ~$0.57/100
  projected — vs $0 for declarative. (Latencies are mock-mode local
  compute, not real network calls.)
- **`policy/naive.py`** — baseline: "ask the LLM in plain English what
  to do." 54% correctness; fails entirely on `reject` scenarios.

The picked declarative champion is what every other module imports by
default; the four are kept side-by-side because the comparison IS the
research output.

### Approval queue + quorum
- **`approval_queue.py`** — pending action store. Tenant-scoped. Backs
  the `/pending` route and the approver UI in `ui/approver_app.py`.
- **`quorum.py`** — N-of-M approver logic. An action enters
  `await_quorum` when policy says so; transitions to `approved` once
  the threshold is met. Tracks individual approver decisions on the
  action so the audit log can name who voted which way.

### Execution + audit
- **`executor.py`** — action dispatch. Stub implementations for the
  demo (`send_text`, `send_email`, `disburse_funds`, …) — they log
  the intended side-effect with the structured payload rather than
  hit a real provider. The executor abstraction is production-shaped;
  swap in real integrations without touching planner / policy / queue.
- **`audit.py`** — append-only audit log writer. Every proposal,
  policy decision, approval, rejection, and execution writes a row
  keyed by `(tenant_id, action_id)` with a `caused_by_event_id`
  link. Optional `expected_tenant_id=` kwarg enforces the multi-tenant
  invariant at the audit layer too (Phase-4 Day 20 hardening).

## Invariants the tests enforce

1. **Multi-tenant.** Every route accepts `tenant_id`; cross-tenant
   reads 404 with the same shape as missing-row (OWASP API1:2023).
   `tests/integration/test_tenant_isolation.py`.
2. **Idempotent decisions.** Same
   `(tenant_id, event_id)` replayed by the pipeline produces no new
   actions. `tests/integration/test_idempotency.py`.
3. **Audit-complete.** Every action's full lifecycle (propose →
   policy-decide → approve/reject → execute) is reconstructable from
   the audit log alone. `tests/unit/test_audit_log.py` +
   `tests/integration/test_end_to_end_jane_scenario.py`.
4. **No double-execute under race.** Concurrent approvers can't push
   an action past its quorum threshold twice.
   `tests/integration/test_race_conditions.py`.

## Local dev

```bash
uvicorn orchestrator.api:app --reload --port 8002
pytest tests/unit -k "policy or queue or audit or executor or pipeline"
streamlit run ui/approver_app.py    # the approver dashboard
```
