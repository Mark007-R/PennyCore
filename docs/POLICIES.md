# PennyCore — Tenant Policies

**Status:** Locked at Phase 3 wrap (Day 18, 2026-05-21). Champion engine
named; alternatives retained as comparison rows.
**Scope:** How the orchestrator decides whether an LLM-proposed action
should auto-execute, queue for a human approver, or be rejected — and
how a compliance team configures that per tenant without engineering
touch.
**Cross-reference:** SYSTEM_DESIGN.md §6 (the architecture); API.md §
orchestrator (the surface that exposes pending actions); the
`orchestrator/policy/` package (the four engine implementations).

This document explains:

1. The decision space every engine must honor.
2. The policy data model (`Policy` Pydantic shape + the Postgres table).
3. The four engines we compared in Phase 3 — what each is, how each is
   configured, and what each costs.
4. The champion (`declarative`) and the empirical case for it.
5. How a compliance officer authors a policy table without an engineering
   deploy.

---

## 1. The decision space

The orchestrator's job, on every inbound event, is to turn the LLM
planner's `ActionProposal` into one of three `PolicyDecision` outcomes:

| Decision | Effect | Audit row written |
|----------|--------|--------------------|
| `auto` | Action goes straight to the executor; status transitions `pending_policy → pending_exec → executed` (or `execution_failed`). | `DECISION` + `EXECUTION` (or `EXECUTION_FAILED`) |
| `approval_required` | Action is enqueued on the tenant's approval queue; status `pending_policy → pending_approval`. A human approver later flips it to `approved` → `executed` or `rejected`. | `DECISION` at policy time; `APPROVAL` or `REJECTION` at human-decision time; `EXECUTION` when the approved action runs. |
| `reject` | Action is killed at policy time; status goes straight to `rejected`. No executor invocation, no queue row. | `DECISION` + `REJECTION` |

The decision space is deliberately small. Every Phase-3 engine variant
returns one of these three values — never a free-form string, never a
nullable answer. The downstream pipeline (`orchestrator/decision_pipeline.py`)
is the same code path for all four engines; only the *function that
returns the decision* differs.

This is the orchestrator's central correctness property: every action
the system performs is traceable to one `DECISION` audit row, and that
row names the engine, the tenant, the action type, and the decision.

---

## 2. The policy data model

### Pydantic shape (`contracts/policies.py`)

```python
class PolicyDecision(str, Enum):
    AUTO = "auto"
    APPROVAL_REQUIRED = "approval_required"
    REJECT = "reject"


class Policy(BaseModel):
    id: str
    tenant_id: str
    action_type: ActionType        # one of the 6 action types we support
    decision: PolicyDecision       # one of the 3 outcomes above
    body: dict[str, Any]           # engine-specific config (YAML rules, Python rule names, …)
    version: int                   # monotonic — bump on every edit
    created_at: datetime
    updated_at: datetime
```

The `body` field is engine-specific: the declarative engine ignores it
(the `decision` field is the whole answer), the Python-rules engine
reads a rule-class name, the LLM-as-judge engine reads a prompt
template. This shape lets all four engines share one persistence row
without one engine's schema dictating the others.

### Postgres table

```sql
CREATE TABLE policies (
    id            text PRIMARY KEY,
    tenant_id     text NOT NULL REFERENCES tenants(id),
    action_type   text NOT NULL,                -- mirrors ActionType enum
    decision      text NOT NULL,                -- mirrors PolicyDecision enum
    body          jsonb NOT NULL DEFAULT '{}',
    version       int NOT NULL DEFAULT 1,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, action_type, version)
);
```

The `(tenant_id, action_type, version)` UNIQUE constraint means: any
combination of tenant + action_type has exactly one *current* policy
row (the row with the max version). Older versions stay for audit —
the `policies` table is the historical record of what each tenant's
policy table looked like at any point in time.

### Multi-tenant invariant

Every engine reads policies scoped by `tenant_id` first, action_type
second. There is no global policy table, no shared default that
crosses tenants. A bug that forgets the tenant scope is a class-A
multi-tenant leak; tests in `tests/unit/test_declarative_policy.py`
assert no cross-tenant access path exists.

---

## 3. The four engines

All four implement the same Protocol (`orchestrator/policy/__init__.py`):

```python
class PolicyEngine(Protocol):
    def set_policies(self, tenant_id: str, policies: dict[str, str]) -> None: ...
    def decide(self, tenant_id: str, action_type: ActionType) -> PolicyDecision: ...
```

Swap the engine constructor in `make_default_pipeline()` and the
DecisionPipeline doesn't change.

### 3.1 `declarative` — flat dict / YAML rules (champion)

A tenant's policy table is a `dict[action_type_str, decision_str]`:

```yaml
tenant_acme_bank:
  send_borrower_message: approval_required
  notify_loan_officer:   auto
  request_document:      approval_required
  schedule_call:         approval_required
  update_status:         auto
  no_op:                 auto
  default:               approval_required   # fallback for unconfigured action types
```

Resolution order (`orchestrator/policy/declarative.py`):

1. Exact match on the action_type string.
2. Fall back to the explicit `"default"` key.
3. Fall back to the `"*"` wildcard key (compatibility shape used by
   the external take-home assessment).
4. Final safety net: `PolicyDecision.APPROVAL_REQUIRED` (unknown
   action types escalate to a human rather than silently auto-executing).

The engine itself is ~50 lines of Python. The cost of a decision is a
single dict lookup. The runtime shape is a denormalized
`dict[tenant_id, dict[action_type, decision]]` — the Postgres-backed
`sync_from_repository()` method hydrates this from `policies` rows.

### 3.2 `python_rules` — programmatic rule classes

A tenant's policy is expressed as a list of `PolicyRule` subclasses
that the engine evaluates in order. Each rule can inspect the action
context (tenant + action_type + optional payload) and return a
decision or pass to the next rule.

```python
class HighValueWireTransferRule(PolicyRule):
    def applies(self, ctx): return ctx.action_type == ActionType.UPDATE_STATUS and ctx.payload.get("amount", 0) > 10_000
    def decide(self, ctx): return PolicyDecision.APPROVAL_REQUIRED
```

More expressive than declarative — you can branch on payload fields.
Less maintainable: any policy change is a code deploy.

### 3.3 `llm_judge` — LLM-as-judge

Every decision goes through an LLM call with a structured prompt:

> *"Given action_type=X and tenant_policy='...', should this action
> auto-execute, require approval, or be rejected? Reply with JSON: {"decision": "..."}"*

Strict-JSON output (same shape the planner uses, see SYSTEM_DESIGN
§5.4), Pydantic-validated against `PolicyDecision`. On parse failure
or unknown decision, fall back to `APPROVAL_REQUIRED` (the same
conservative default the declarative engine uses).

### 3.4 `naive_llm` — "paste the policy into the prompt" baseline

The bare counterfactual most AI startups ship in 2025-2026: dump the
policy verbatim into the LLM's context window with a prompt like *"Here's
the policy. Decide."* No structured output, no validation, no fallback
beyond "if parsing fails, take the LLM's first verb." This is the
strawman the comparison study is built around — and it's the strawman
because *the strawman is what's in production at most companies right
now*.

---

## 4. Phase 3 comparison — the empirical case for declarative

Day 17 ran all four engines against the same 200-tuple dataset
(`benchmarks/data/orchestrator/`). The dataset has 127 expected-auto,
68 expected-approval, and 5 expected-reject scenarios, spread across
three synthetic tenants (Acme Bank, Globetrek Concierge, Jefferson
Credit). Results from `results/phase3_day18_consolidated.json`:

| Strategy | Correctness | Latency p50 (µs) | Cost / 100 decisions (USD) | Auditability (1-5) | Maintainability (1-5) | LLM calls |
|----------|------------:|------------------:|---------------------------:|--------------------:|-----------------------:|----------:|
| **declarative** | **1.000** | **0.6** | **$0.000** | **5** | **5** | 0 |
| python_rules | 1.000 | 0.7 | $0.000 | 3 | 2 | 0 |
| llm_judge | 1.000 | 6.7 | $0.111 | 4 | 4 | 200 |
| naive_llm | 0.540 | 1.8 | $0.139 | 2 | 4 | 200 |

The headline findings (verbatim from the Phase 3 wrap):

1. **Declarative beats LLM-as-judge on the cost axis at parity
   correctness** — same 1.00 correctness, $0.00 vs $0.111 per 100
   decisions, ~11× lower per-decision latency. "Just ask an LLM"
   is pure cost overhead when the policy table is exact.
2. **Naive-LLM drops to 54% correctness AND misses every `reject`
   scenario (5/5 wrong).** This is the silent-execution failure mode
   regulators audit for. Per-decision breakdown:
   - `reject` correctness: **0%** (5/5 wrong — naive-LLM routes
     high-risk actions to `auto` because the prompt's framing biases
     toward "do the thing")
   - `approval_required` correctness: 50%
   - `auto` correctness: 58%
   - Per-event-type: worst on `anomaly_detected` (22%) — exactly the
     events a regulator would expect the system to handle most
     carefully.
3. **Python-rules ties declarative on correctness but scores 2/5 on
   maintainability** — a compliance change requires a code deploy. Fine
   for a small team, painful at scale.

### Why declarative is the champion

Three reasons, in order of importance:

1. **Correctness at zero cost.** It matches the LLM-as-judge engine's
   correctness on this dataset while making zero LLM calls. The
   compute-cost question doesn't dominate at 200 decisions, but at
   100K decisions/day (the load PennyCore is shaped for), $0 vs
   $111/day is real money — and the latency delta compounds against
   end-to-end SLOs.
2. **Auditability.** A `DECISION` audit row from the declarative
   engine answers *exactly* why the decision was made: "policy table
   says (tenant=X, action_type=Y) → Z." There's no LLM prompt to
   replay, no model version to pin, no temperature to remember. A
   compliance officer reading the audit log six months later sees
   the rule directly.
3. **Maintainability.** A compliance team can edit a YAML file or a
   `policies` table row without engineering touch. The Day-31 admin
   UI exposes the policy table as a CRUD surface; a Python-rules
   engine would force every policy change through a PR + deploy.

### What stays for the slice declarative can't cover

LLM-as-judge is **retained in the codebase**, not deleted. The Phase 5
re-run (Day 28) tests it on the ambiguous-policy slice — situations
the declarative table cannot enumerate (e.g., "if the customer's
sentiment is hostile and the action is `schedule_call`, require
approval"). When the policy is itself fuzzy, the LLM's understanding
of the prompt becomes the right tool. The point of Phase 3 was not
that LLM-as-judge has no role; it's that **for clear policies, the
declarative engine wins outright**.

The naive-LLM engine is retained as the **comparison baseline** — it
stays in the repo as the reference for what *not* to ship. The 54%
correctness number is a portfolio artifact: hiring managers should be
able to run the benchmark themselves and see why the prompt-only
approach loses.

---

## 5. Authoring a policy table — compliance workflow

The intended workflow for a non-engineering compliance officer:

1. **Identify the new policy.** Example: "Bank A now wants every
   `request_document` event to auto-execute instead of requiring
   approval."
2. **Open the admin UI** (Day 31 deliverable, Streamlit-based) and
   navigate to *Tenants → Acme Bank → Policies*.
3. **Change the row** `request_document → approval_required` to
   `request_document → auto`. Click save.
4. **The UI writes a new `Policy` row** with `version = current + 1`
   to the `policies` table. The previous row is retained for audit.
5. **The DecisionPipeline picks up the new version on the next
   decision** — there's a refresh cadence (10 seconds in production)
   for the in-memory engine to sync against the DB.

What does NOT happen:

* No engineering ticket.
* No deploy.
* No restart.
* No code review (the change is config, not code).

This is the production-grade discipline that distinguishes a "we wired
the LLM up to the database" demo from infrastructure a regulated
business can ship. Phase 3's empirical case is the proof point;
Phase 6's admin UI is the operator surface that makes it real.

---

## 6. Cross-reference

- **`contracts/policies.py`** — Pydantic shapes.
- **`migrations/versions/0001_initial_schema.sql`** — `policies` and
  `approval_queue` DDL.
- **`orchestrator/policy/__init__.py`** — engine Protocol + exports.
- **`orchestrator/policy/declarative.py`** — the champion.
- **`orchestrator/policy/python_rules.py`** — the maintainability-loser
  alternative.
- **`orchestrator/policy/llm_judge.py`** — kept for ambiguous-policy slice.
- **`orchestrator/policy/naive.py`** — the comparison baseline.
- **`benchmarks/orchestrator_bench.py`** — the harness that produces
  the numbers in §4.
- **`results/phase3_day18_consolidated.json`** — the locked Phase-3
  wrap data the §4 table is sourced from.
- **`results/phase3_orchestrator_results.json`** — the raw per-strategy
  head-to-head run data behind those numbers.

The policy module is locked at Phase 3 wrap. Phase 4 (Days 19-23)
hardens around it; Phase 5 (Day 28) re-runs the LLM-judge slice with
the real LLM client active.
