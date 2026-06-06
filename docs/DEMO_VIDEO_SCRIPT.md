# PennyCore — Demo video script

Target length: **5 minutes** end-to-end.
Target audience: hiring manager or founding-engineer interviewer.
Recording mode: screen capture (Loom / OBS / QuickTime), one take is
fine — this is not a marketing video. The point is showing the system
running, not slick production values.

The recording itself is the **human** step. This script is the
shot-list and on-screen narration so the recording is repeatable.

---

## Pre-flight (do this once, before hitting record)

```bash
# from project root
docker-compose up -d                     # Postgres + Redis + both services
# in another terminal:
streamlit run ui/approver_app.py         # http://localhost:8501
# in another:
streamlit run ui/demo_scenario_app.py    # http://localhost:8502
```

Confirm all of the following before recording:

- `curl http://localhost:8001/healthz` returns 200 (context_engine)
- `curl http://localhost:8002/healthz` returns 200 (orchestrator)
- Streamlit approver app at :8501 shows an empty pending queue
- Streamlit demo app at :8502 loads the Jane timeline cleanly

Quit any noisy chat apps. Set the terminal font size up to 16+ pt.

---

## Shot list

Open the recording with the **README** on screen so the viewer
anchors on the project before any code rolls.

### Scene 1 — Frame the problem (0:00 – 0:35)

**On screen:** [`README.md`](../README.md), scrolled to the "Status"
and "What's in the box" sections.

**Narration:**

> "PennyCore is the production AI infrastructure for customer-service
> agents in regulated industries — what the back end of Decagon or
> Sierra looks like. Two services: a memory layer called context_engine
> that assembles a token-budgeted brief on every reply, and a
> decision layer called orchestrator that gates LLM-proposed actions
> through a tenant-specific policy engine, runs an approval queue, and
> writes a full audit trail. Built over 35 days. Currently 871 tests
> passing, 90% coverage on core packages, both halves of the external
> take-home harness graded at full marks."

### Scene 2 — Show the architecture (0:35 – 1:10)

**On screen:** [`ARCHITECTURE.md`](../ARCHITECTURE.md), scrolled to
the component map (mermaid section 1), then the sequence diagram
(section 2).

**Narration:**

> "Two services, one repo, one schema. context_engine ingests events
> from any channel — chat, email, SMS, voice — links the customer
> across channels, assembles the brief. orchestrator picks the event
> off Redis, asks the LLM what to do, runs the proposed actions
> through the policy engine, queues for approval if policy says so,
> executes, audits. The demo scenario is Jane Doe applying for a
> mortgage at Acme Bank — three events, three actions, every state
> shape: auto-execute, quorum approval, reject-then-approve."

### Scene 3 — End-to-end run: ingest + auto-execute (1:10 – 2:00)

**On screen:** terminal, running curl commands. Have these ready in
a scratch file to paste in.

**Narration:** *(do the curl, then read the result aloud while it's
on screen)*

> "Watch the event flow. POST a mortgage_inquiry event to
> context_engine, scoped to tenant=acme..."

```bash
curl -X POST http://localhost:8001/events \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "acme",
    "event_id": "evt-demo-001",
    "customer_email": "jane.doe@example.com",
    "channel": "email",
    "event_type": "mortgage_inquiry",
    "payload": {"text": "Hi, I'\''d like to check my mortgage rate."}
  }'
```

> "...returns 201, event ingested. context_engine linked it to a
> customer record, wrote the row, published to Redis. orchestrator
> picked it up, fetched the brief, asked the planner. Policy
> engine — declarative YAML — said auto-execute for the welcome-text
> action, so the executor ran it. The audit log now has three rows
> for this event. Let's pull them..."

```bash
curl http://localhost:8002/pending?tenant_id=acme
# (empty — the welcome action auto-executed)

# Now show the audit trail for the action:
curl "http://localhost:8002/actions/<action_id>?tenant_id=acme"
```

### Scene 4 — Quorum-gated action + approver UI (2:00 – 3:00)

**On screen:** terminal for the POST, then switch to the approver
Streamlit app at :8501.

**Narration:**

> "Now a higher-stakes event. Same flow, but the disburse_funds action
> falls under a quorum-required rule — 2 of 3 loan officers must
> approve."

```bash
curl -X POST http://localhost:8001/events \
  -H "Content-Type: application/json" \
  -d '{
    "tenant_id": "acme",
    "event_id": "evt-demo-002",
    "customer_email": "jane.doe@example.com",
    "channel": "email",
    "event_type": "disbursement_request",
    "payload": {"amount_cents": 25000000, "loan_id": "ln-jane-001"}
  }'
```

**Switch to the approver UI tab.** Show the new pending row.

> "The approver dashboard shows the new pending row. I'll approve as
> officer-1..." *(click approve)* "...action is now 1 of 3.
> Approve as officer-2..." *(click approve)* "...threshold met, the
> action transitions to approved, executor runs it, audit log gets
> two approval rows plus the execution row. Officer-3's vote isn't
> needed — quorum already cleared."

### Scene 5 — The headline finding (3:00 – 3:50)

**On screen:** [`docs/POLICIES.md`](POLICIES.md), Phase-3 comparison
table at the top.

**Narration:**

> "Here's the comparison study that drove the policy-engine pick.
> Four engines on 200 scenarios: a naive 'ask the LLM in plain
> English' baseline, a Python rules engine, the declarative YAML
> champion, and LLM-as-judge with structured output. The naive LLM
> baseline got 54% correctness and zero out of five on the reject
> class — it does not know how to say no. LLM-as-judge ties the
> declarative champion on correctness at 100%, but at 11 milliseconds
> per decision and ~$0.11 per 100 decisions versus the declarative
> engine's 0.6 microseconds and zero cost. The 'best practice' for
> compliance decisions in 2025-2026 — use the LLM to interpret the
> policy — wins on nothing except the appearance of sophistication.
> The retrieval comparison in context_engine reads the same way:
> hybrid retrieval wins on quality AND cost, the LLM-summary-only
> strategy actually loses on quality, and the naive dump-everything
> baseline costs about 6× the champion at parity quality."

### Scene 6 — Show the tests + coverage (3:50 – 4:25)

**On screen:** terminal — run pytest with a coverage report.

**Narration:**

> "Test suite — 871 passing, 10 skipped. Skips are Postgres-gated;
> they run when DATABASE_URL is set."

```bash
pytest --tb=no -q | tail -5
```

> "Coverage on the core packages — 90%. The LLM adapter modules and
> the OpenTelemetry no-op path were backfilled to 100% on Day 33."

```bash
pytest --cov=context_engine --cov=orchestrator --cov=contracts \
       --cov-report=term --tb=no -q | tail -15
```

> "Three hardening test suites that bear special mention. Twenty
> idempotency tests across every replay surface. Fifteen multi-tenant
> isolation tests — those found and closed two real HTTP leak surfaces
> in Phase 4. Ten race condition tests — nine were safe by Phase-2
> construction, the tenth was a real linker TOCTOU bug that was
> invisible to vanilla threaded tests because the GIL serialized the
> window. Fixed with a catch-and-retry-on-UNIQUE pattern."

### Scene 7 — Takehome scorecard + wrap (4:25 – 5:00)

**On screen:** terminal — run the takehome eval script if the
real-API venv is set up; otherwise show the saved scorecard.

**Narration:**

> "The external take-home scorecard the project was built to satisfy:
> context-engine adapter passes 5 out of 5 non-LLM scenarios,
> orchestrator adapter passes 6 out of 6. Both adapters are thin
> wrappers over the same production modules every other call site
> uses — no parallel implementation. That's the design call that
> makes drift between the production system and the scored system
> structurally impossible."

```bash
cat results/takehome_scorecard.md | head -40
```

> "Repo is at github.com/Mark007-R/PennyCore. README and
> ARCHITECTURE.md are the place to start; docs/SYSTEM_DESIGN and
> docs/POLICIES are the deep dives. Thanks for watching."

**Stop recording.**

---

## Editing pass (optional)

If a rough cut is acceptable, ship the raw take. If polishing:

- Trim dead air > 1 second.
- Add a 1-second fade at top + tail.
- Caption the four scene transitions with a half-second title card
  (Scene 1 / 2 / 5 / 7 are the natural breakpoints).
- Burn in a small "PennyCore — github.com/Mark007-R/PennyCore"
  lower-third for the full duration.

## Save location

Save the final video as `results/samples/demo_video.mp4`. The path is
referenced by the Day-35 final report.

If hosting externally (YouTube unlisted, Loom share link), update
the Day-35 final report with the URL. Don't link from the README —
the README links to the script, not the video, so the script can be
re-recorded if the demo evolves.

## Re-recording when the demo evolves

This script is versioned alongside the code. If a future commit
changes the API contracts (event payload shape, policy YAML format),
update the curl bodies in Scenes 3 + 4 in the **same commit** so the
script never goes out of sync with the running system. The whole
point of versioning the script is that re-recording is a
20-minute job, not an archaeology project.
