# PennyCore — Demo Scenario: Jane Doe's Mortgage Journey

**Status:** Locked at Phase 2 wrap (Day 11, 2026-05-14). Lives forever
as the canonical end-to-end walkthrough hiring managers and recruiters
see when reading the repo.
**Source of truth:** `tests/integration/test_end_to_end_jane_scenario.py`
— if this doc and that test disagree, the test wins.
**Cross-reference:** SYSTEM_DESIGN.md §1 (architecture overview),
POLICIES.md §3.1 (the declarative policies Acme Bank uses below),
API.md (the request/response shapes mentioned here).

This document walks one customer — Jane Doe, applying for a mortgage at
Acme Bank — through the system end-to-end. It's the demo a hiring
manager runs to see the system working; it's the story the README
points at; it's the canonical "everything composed" test in
`tests/integration/`. If you only read one walkthrough, read this one.

---

## 1. The scenario in one paragraph

Jane Doe is applying for a mortgage at Acme Bank, a regulated lender
that has chosen a *strict-approval* posture: any message PennyCore
proposes to send to Jane must be approved by a human loan officer
before it goes out. Over the course of one day, Jane interacts with
the bank through three different channels (chat, email, SMS), the
back-office system fires two operational events (status change,
anomaly detected), and PennyCore handles every one: it links each
event to the same customer record, asks the LLM what to do, runs the
proposal through the policy engine, queues borrower-facing messages
for human approval, auto-executes internal notifications, and writes
a complete audit trail. By the end of the journey, the loan officer
has approved one queued action and the audit log can reconstruct
exactly why every decision was made.

That's the demo. The architecture (event ingestion + memory layer +
decision pipeline + audit log + policy engine + approval queue) is
indifferent to the vertical — the same system would work for
healthcare appointment scheduling or insurance claim triage. Mortgage
is the demo because mortgage is the cleanest illustration of a
regulated industry where audit and approval matter.

---

## 2. The cast

- **Jane Doe** — the borrower. Identified across channels by external
  CRM id `CRM-001`, email `jane@acme.com`, and phone `+15550100100`.
- **Sam Patel** — the loan officer at Acme Bank. The human approver
  for any borrower-facing message PennyCore proposes.
- **Acme Bank** — the tenant. Tenant id `acme-bank`. Policy posture:
  strict-approval (see POLICIES.md §3.1).
- **Beta Bank** — a second tenant present in the scenario *only* to
  prove multi-tenant isolation. Tenant id `beta-bank`. Policy posture:
  permissive auto-everything. Jane has no relationship with Beta Bank;
  it's there as a control.

---

## 3. The five-event journey

Five events arrive in order. Each one walks the full pipeline.

### Event 1 — Chat: "What's the status of my application?"

* **Channel:** chat
* **Payload:** `{"from_email": "jane@acme.com", "external_id": "CRM-001", "text": "Hi, what's the status of my W-2 review?"}`
* **What happens inside PennyCore:**
  1. `POST /events` lands on the context-engine. Validation passes; an
     idempotency key (`jane-msg-001`) is stamped.
  2. The customer linker resolves the `external_id` → no prior
     customer found → creates a fresh `Customer` row, attaches three
     identities (external_id, email, phone-not-yet-known).
  3. Event is persisted; a small envelope is published to the
     `events.acme-bank` channel.
  4. The orchestrator's event listener picks up the envelope, calls
     the planner with Jane's (empty so far) brief, gets back an
     `ActionProposal` for `send_borrower_message` with body
     "We received your message — Sam will respond shortly."
  5. The declarative policy engine looks up
     `(acme-bank, send_borrower_message)` → `approval_required`.
  6. The pipeline materializes the `Action` row at status
     `pending_approval` and enqueues it for Sam.
  7. Audit rows written: `PROPOSAL` + `DECISION`. Status: queued.

### Event 2 — Email: pay-stub document uploaded

* **Channel:** email
* **Payload:** `{"from_email": "jane@acme.com", "subject": "Pay stubs attached", "document_type": "pay_stubs"}`
* **What happens inside PennyCore:**
  1. Linker matches `from_email` to Jane's existing customer record
     (same `external_id` would also win — both routes resolve to
     `cust_jane`). No new customer created.
  2. Event published; orchestrator picks up.
  3. Planner proposes `update_status` (status: `documents_received`).
  4. Policy engine: `(acme-bank, update_status)` → `auto`. No human
     in the loop for internal status flips.
  5. Pipeline materializes the action, runs the mock executor, status
     transitions to `executed`.
  6. Audit rows written: `PROPOSAL` + `DECISION` + `EXECUTION`.

### Event 3 — SMS: "Calling about my application"

* **Channel:** sms
* **Payload:** `{"from_phone": "+15550100100", "text": "I'll call back at 3pm"}`
* **What happens inside PennyCore:**
  1. Linker matches `from_phone` to Jane's record. Cross-channel
     linking is now proven: chat (event 1), email (event 2), SMS
     (event 3) all resolve to the same `cust_jane`.
  2. Planner proposes `send_borrower_message` ("Acknowledged —
     looking forward to your call").
  3. Policy gates to `approval_required`. Sam now has TWO pending
     actions in his queue.
  4. Audit: `PROPOSAL` + `DECISION`.

### Event 4 — API: back-office status change

* **Channel:** api
* **Payload:** `{"customer_id": "cust_jane", "new_status": "ready_for_underwriting"}`
* **What happens inside PennyCore:**
  1. No linker needed — explicit `customer_id`.
  2. Planner proposes `notify_loan_officer` ("Jane's file is ready
     for review — Sam, take a look").
  3. Policy: `(acme-bank, notify_loan_officer)` → `auto`. Internal
     notifications don't gate on a human.
  4. Executor runs; audit rows: `PROPOSAL` + `DECISION` + `EXECUTION`.

### Event 5 — API: anomaly detected (income mismatch)

* **Channel:** api
* **Payload:** `{"customer_id": "cust_jane", "reason": "stated_income_vs_w2_mismatch", "delta_usd": 12500}`
* **What happens inside PennyCore:**
  1. Anomaly events bypass the planner's customer-facing default —
     the LLM proposes `notify_loan_officer` ("Anomaly detected,
     income mismatch of $12,500 — Sam, please review").
  2. Policy: `auto`. The audit row carries the anomaly reason.
  3. Executor runs; Sam has visibility on the anomaly the moment
     it's detected.

---

## 4. The loan officer's view

Sam logs into the admin UI (Day 31 deliverable, Streamlit). He sees:

* **Pending approvals queue:** two rows — both for Jane.
  - Row 1: `send_borrower_message` from event 1 (chat
    acknowledgement). Created 4 hours ago.
  - Row 2: `send_borrower_message` from event 3 (SMS
    acknowledgement). Created 3 hours ago.
* **Recent audit log:** five events fanned out into ten audit rows
  (`PROPOSAL` + `DECISION` for the two queued; `PROPOSAL` +
  `DECISION` + `EXECUTION` for the three auto-executed).
* **Customer detail for Jane:** one customer record, three identities
  (external_id, email, phone), five events all linked.

Sam clicks **Approve** on Row 1. The action transitions:
`pending_approval → pending_exec → executed`. The mock executor logs
"send chat message to Jane: 'We received your message — Sam will
respond shortly.'" An `APPROVAL` + `EXECUTION` audit row is added.
The queue now has one remaining row.

Sam clicks **Reject** on Row 2 with reason "I'll call her directly."
The action transitions `pending_approval → rejected`. A `REJECTION`
audit row is added. The queue is empty.

---

## 5. The audit reconstruction

The whole point of writing audit rows on every transition is that
compliance can later reconstruct what happened and why. Six months
later, a regulator asks: *"Why did Acme Bank's system send Jane Doe
the message at 14:32 on May 14?"*

The compliance officer pulls `audit_log` rows where
`actor_id='cust_jane'` (or queries `entries_for_action(action_id)`
through the admin UI). They see:

```
[14:31:12]  PROPOSAL   from=llm  action=send_borrower_message  reasoning="customer initiated chat, acknowledge politely"
[14:31:12]  DECISION   from=system  decision=approval_required  rule="(acme-bank, send_borrower_message)"
[14:32:08]  APPROVAL   from=human  actor=sam_patel  prior_status=pending_approval
[14:32:08]  EXECUTION  from=system  payload={recipient:borrower, message:"We received your message..."}
```

Four rows. Four-line reconstruction of the entire decision:
- The LLM proposed the action (because the customer initiated chat).
- The policy engine required approval (because Acme is strict).
- Sam approved (his user id is on the row).
- The mock executor "sent" the message (in production this would
  carry the Twilio / SendGrid message id; in the demo it's a logged
  intent).

This is the property that distinguishes infrastructure from a demo:
*every* action the system performs can be reconstructed from the
audit log alone, without re-running the LLM, without replaying the
event stream, without trusting the application's memory.

---

## 6. Multi-tenant control: Beta Bank

While Jane's five events are flowing through the system on the
`acme-bank` tenant, a single test event fires on `beta-bank` (a
different tenant, permissive policy posture, no relationship to Jane).
The test asserts:

* Beta's event produces its own `Action` with a globally-unique action
  id (no collision with Acme's action ids).
* Querying `/events/recent?tenant_id=acme-bank` returns Jane's five
  events and nothing from Beta.
* Querying `/events/recent?tenant_id=beta-bank` returns Beta's one
  event and nothing from Acme.
* Querying `/approvals?tenant_id=acme-bank` returns Sam's two pending
  rows and nothing from Beta.
* Sam (an Acme user) cannot see any Beta data through any endpoint.

This is the multi-tenant invariant from SYSTEM_DESIGN §3 made
concrete: same code path, two tenants, zero cross-pollination. The
Day-20 hardening pass adds 15 more isolation tests; the demo
scenario's role is to prove the invariant *visibly* in front of a
recruiter.

---

## 7. Idempotent redelivery

The scenario also covers what happens when the upstream channel
(Twilio, SendGrid, the back-office system) redelivers an event. In
the test:

* Event 1's payload is POSTed a second time with the same
  `idempotency_key = jane-msg-001`.
* PennyCore returns `200 OK` (not `202 Accepted`) with the original
  event id and `deduped: true`.
* The customer linker is NOT re-run (so a tampered replay payload
  can't pollute the customer identities table).
* The bus is NOT re-published (so the orchestrator doesn't see a
  second copy of the event).
* No new `Action` is created, no new audit rows are written.

This is the Day-19 idempotency invariant made concrete (see also
`tests/integration/test_idempotency.py` for the 20-test verification
suite that locks the property in).

---

## 8. How to run the demo

Two ways to run Jane's scenario.

### A. The unit/integration test (fast, hermetic)

```bash
pytest tests/integration/test_end_to_end_jane_scenario.py -v
```

This runs in-process with in-memory adapters. ~1 second total. Three
tests:

1. `test_jane_mortgage_end_to_end_happy_path` — all five events,
   approval flow, audit assertion.
2. `test_jane_mortgage_idempotent_redelivery_no_double_action` —
   the §7 invariant.
3. `test_jane_mortgage_multi_tenant_isolation_acme_vs_beta` — the
   §6 invariant.

### B. The live docker-compose stack (slower, real DB + Redis)

```bash
docker-compose up -d        # boots Postgres + Redis + both FastAPI apps
bash scripts/seed_demo_data.py   # sets up Acme + Beta tenants with policies
bash scripts/run_jane_scenario.sh  # POSTs the five events
```

This runs against real Postgres (with the `events`, `actions`,
`audit_log` tables populated) and real Redis (with the `events.*`
pattern subscription firing). Browse to `http://localhost:8001/approvals?tenant_id=acme-bank`
to see Sam's queue.

The Day-31 admin UI (Streamlit) is the recommended way to walk
through the scenario in a hiring conversation — it shows the queue,
the audit log, and the approve/reject buttons in one screen.

---

## 9. Why this scenario, not a "real" mortgage dataset

The mortgage flow is *synthesized*. Jane Doe is a fabricated person,
Acme Bank is a fabricated bank, the events are hand-written. Real
mortgage data is unavailable (PII), inappropriate (regulatory) and
unnecessary — what we're demonstrating is the *infrastructure
pattern*, not the domain.

The Phase 3 benchmarks use **real public conversation data** where
the comparison is on retrieval/policy quality (200 customer histories
from MultiWOZ 2.2, 50 from banking templates; see
`benchmarks/data/README.md`). That's where data realism matters.
The Jane demo is for **architecture clarity** — one customer, one
story, one happy-path-plus-edge-cases narrative a recruiter can hold
in their head while reading the code.

---

## 10. Cross-reference

- **`tests/integration/test_end_to_end_jane_scenario.py`** — the test
  this doc narrates. Source of truth.
- **`reports/day11_phase2_report.md`** — the Day-11 build report.
- **`docs/SYSTEM_DESIGN.md`** — the architecture this scenario walks
  through.
- **`docs/POLICIES.md`** — the policy posture (Acme strict, Beta
  permissive) referenced throughout.
- **`docs/API.md`** — the HTTP contracts the scenario uses.
- **`scripts/seed_demo_data.py`** — sets up the tenants for the live
  stack.
- **`ui/demo_scenario_app.py`** (Day 32) — the Streamlit walkthrough.

Locked at Phase 2 wrap. The scenario doesn't change; what changes
across the remaining phases is the *implementation* underneath it
(idempotency hardening Phase 4, semantic retrieval Phase 5, admin UI
Phase 6) — Jane's story stays the same.
