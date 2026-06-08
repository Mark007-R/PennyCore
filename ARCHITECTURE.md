# PennyCore — Architecture (mermaid)

This is the visual companion to [docs/SYSTEM_DESIGN.md](docs/SYSTEM_DESIGN.md).
The prose source-of-truth (responsibilities, schemas, invariants, scenarios)
lives there; this file is the picture-first overview a reader can scan in
60 seconds.

> Render note: GitHub renders these blocks natively. For local preview,
> any mermaid-capable viewer (VS Code "Markdown Preview Mermaid Support",
> Obsidian, Typora) works.

---

## 1. Component map

Two services in one repo, sharing Postgres + Redis, fronted by one of four
LLM providers (Anthropic by default, Azure for the takehome adapter, OpenAI
or Mock as fallback). Tests count: **871 passed, 10 skipped** (Postgres-gated)
as of Day 33; core-package coverage **90%**.

```mermaid
flowchart TB
    subgraph external["External world"]
        Chat[chat]
        Email[email]
        SMS[sms]
        Voice[voice]
        Approver[human approver]
    end

    subgraph contextengine["context_engine — memory layer"]
        Ingest["POST /events<br/>ingestion.py"]
        Link["linking.py<br/>(cross-channel identity)"]
        Brief["GET /brief<br/>brief_assembly.py"]
        Retrieval["retrieval/<br/>recency · semantic · summarized · hybrid · rerank"]
        Safety["safety/<br/>prompt_injection.py"]
        SemCache["llm/semantic_cache.py"]
    end

    subgraph orch["orchestrator — decision layer"]
        Listener[event_listener.py]
        Pipeline[decision_pipeline.py]
        Planner["planner.py<br/>(LLM call, structured output)"]
        Policy["policy/<br/>declarative · python_rules · llm_judge · naive"]
        Queue[approval_queue.py]
        Quorum["quorum.py<br/>(N-of-M approvers)"]
        Exec["executor.py<br/>(mock side-effects)"]
        Audit[audit.py]
    end

    subgraph stores["state"]
        PG[(Postgres<br/>system of record)]
        Redis[(Redis<br/>event bus + cache)]
    end

    subgraph llm["context_engine/llm — provider dispatch"]
        Anthropic[anthropic_client]
        Azure[azure_client]
        OpenAI[openai_client]
        Mock[mock]
    end

    Chat --> Ingest
    Email --> Ingest
    SMS --> Ingest
    Voice --> Ingest

    Ingest --> Link --> PG
    Ingest --> Redis
    Brief --> Retrieval --> PG
    Brief --> SemCache
    Safety -.guards.-> Brief

    Redis --> Listener --> Pipeline
    Pipeline --> Brief
    Pipeline --> Planner --> Policy
    Policy --> Queue
    Policy --> Exec
    Queue --> Approver
    Approver --> Quorum --> Exec
    Exec --> Audit --> PG
    Pipeline --> Audit

    Planner --> Anthropic
    Planner --> Azure
    Planner --> OpenAI
    Planner --> Mock
    Brief --> Anthropic
    Brief --> Mock
```

**Reading order:** events fly in on the left, hit `context_engine` first
(memory: write to Postgres, publish to Redis, assemble a brief on demand),
then `orchestrator` picks the event off Redis, asks the LLM what to do,
runs the proposed actions through the policy engine, queues for approval
when policy demands it, executes, and audits.

---

## 2. End-to-end data flow — Jane's mortgage scenario

Three events, three actions, every state shape: auto-execute, quorum
approval, reject-then-approve. This is the canonical demo and the
`tests/integration/test_end_to_end_jane_scenario.py` happy path.

```mermaid
sequenceDiagram
    autonumber
    actor Jane
    participant CE as context_engine
    participant Bus as Redis
    participant Orch as orchestrator
    participant Policy as policy/declarative
    participant Q as approval_queue
    participant Ex as executor
    participant Audit as audit_log (Postgres)
    participant Officer as loan officer

    Jane->>CE: POST /events (mortgage_inquiry)
    CE->>CE: link customer (email match)
    CE->>Postgres: write event + identity
    CE->>Bus: publish event
    Bus->>Orch: event_listener picks up
    Orch->>CE: GET /brief?customer_id=...
    CE-->>Orch: token-budgeted brief
    Orch->>Orch: planner — LLM proposes 3 actions
    Orch->>Policy: evaluate (tenant=acme, action=send_welcome_text)
    Policy-->>Orch: auto-execute
    Orch->>Ex: executor.send_text(...)
    Ex->>Audit: write (proposal, decision, execution)
    Orch->>Policy: evaluate (action=disburse_funds)
    Policy-->>Orch: require quorum 2-of-3
    Orch->>Q: enqueue pending action
    Q->>Officer: notify
    Officer->>Q: approve (1 of 3)
    Officer->>Q: approve (2 of 3 — threshold met)
    Q->>Ex: executor.disburse_funds(...)
    Ex->>Audit: write (approval × 2, execution)
```

The audit log row links every action back to the originating event_id —
that's the "compliance officer can reconstruct any decision" invariant
(SYSTEM_DESIGN §5).

---

## 3. Multi-tenant isolation surface

Every read and every write scopes by `tenant_id`. Day 20 (Phase 4)
hardened the HTTP surface; the diagram below shows where the tenant
check happens at each layer.

```mermaid
flowchart LR
    Req[HTTP request<br/>?tenant_id=acme] --> R1[FastAPI route]
    R1 --> Check{tenant_id<br/>present?}
    Check -- no --> R422[422 validation error]
    Check -- yes --> R2[repository.<br/>by_tenant filter]
    R2 --> Match{row tenant<br/>matches?}
    Match -- no --> R404[404 not found<br/>same shape as missing row<br/>no existence leak]
    Match -- yes --> R200[200 OK]

    style R422 fill:#fde8e8
    style R404 fill:#fde8e8
    style R200 fill:#e8f7e8
```

Wrapped by `tests/integration/test_tenant_isolation.py` (15 tests across
events, customers, actions, approvals, audit, HTTP). Same-shape error on
cross-tenant access satisfies OWASP API1:2023.

---

## 4. Idempotency model

Every replay surface keys on `(tenant_id, event_id)` for ingestion and
`(tenant_id, idempotency_key)` for actions. The audit chain is
append-only so re-runs surface as additional rows, not silent
overwrites.

```mermaid
flowchart TD
    A[event arrives] --> B{seen<br/>tenant_id, event_id?}
    B -- yes --> Skip[no-op<br/>return stored result]
    B -- no --> C[ingest + write]
    C --> D[publish to Redis]
    D --> E[orchestrator picks up]
    E --> F{seen<br/>tenant_id, idempotency_key?}
    F -- yes --> SkipAct[no-op<br/>return prior action]
    F -- no --> G[propose · evaluate · execute]
    G --> H[audit row]
    H --> I[done]

    style Skip fill:#e8f0fa
    style SkipAct fill:#e8f0fa
```

Wrapped by `tests/integration/test_idempotency.py` (20 tests covering
ingestion HTTP + pure, pipeline dedup, approval-queue double-decision,
audit-chain immutability, threaded races).

---

## 5. LLM provider dispatch

One env var (`LLM_PROVIDER`) flips the whole system between four
back-ends. Mock is the safe default (no keys → no surprise spend).
Every call site imports `from context_engine.llm import get_client` —
nothing reads keys directly.

```mermaid
flowchart LR
    Call[caller<br/>get_client] --> R{MOCK_LLM=true?}
    R -- yes --> M[Mock]
    R -- no --> P{LLM_PROVIDER}
    P -- anthropic --> A{ANTHROPIC_API_KEY<br/>set and real?}
    P -- azure --> Z{AZURE_API_KEY<br/>set and real?}
    P -- openai --> O{OPENAI_API_KEY<br/>set and real?}
    A -- yes --> Acl[claude-sonnet-4-6<br/>· claude-haiku-4-5]
    A -- no --> M
    Z -- yes --> Zcl[AI Foundry Models<br/>Kimi-K2.6]
    Z -- no --> M
    O -- yes --> Ocl[gpt-4o · gpt-4o-mini]
    O -- no --> M

    style M fill:#fef3c7
    style Acl fill:#d1fae5
    style Zcl fill:#d1fae5
    style Ocl fill:#d1fae5
```

The takehome adapter (`takehome/*/`) always flips to Azure regardless
of the global setting, satisfying the external scorecard's
"OpenAI-compatible API" requirement.

---

## 6. Deployment topology

Single host in development; the same compose stack lifts to a managed
host (fly.io / Railway script written but unrun in local-only mode).
OpenTelemetry traces flow to a local Jaeger collector in dev (Day 30).

```mermaid
flowchart TB
    subgraph host["one host"]
        subgraph apps["app containers"]
            CEApp[context_engine<br/>uvicorn :8001]
            OrchApp[orchestrator<br/>uvicorn :8002]
        end
        subgraph infra["state containers"]
            PG[(Postgres :5432)]
            Redis[(Redis :6379)]
        end
        subgraph obs["observability"]
            Jaeger[Jaeger :16686]
            OTC[OTel Collector :4317]
        end
        UI[ui/approver_app.py<br/>Streamlit :8501]
    end

    CEApp --> PG
    CEApp --> Redis
    OrchApp --> PG
    OrchApp --> Redis
    CEApp -.spans.-> OTC --> Jaeger
    OrchApp -.spans.-> OTC --> Jaeger
    UI --> OrchApp
```

Boot the whole thing with `docker-compose up`. The OTel collector is in
`docker-compose.otel.yml` so it's opt-in; production tightening lives
in `docker-compose.prod.yml`.

---

## 7. Phase-by-phase wiring (build order, locked)

```mermaid
gantt
    title PennyCore — 35-day build (one PR per phase)
    dateFormat YYYY-MM-DD
    axisFormat %m-%d
    section Phase 1 — Foundation
    Schema + design + scaffold     :p1, 2026-05-04, 4d
    section Phase 2 — MVP build
    Ingestion → planner → policy   :p2, after p1, 7d
    section Phase 3 — Comparisons
    5 retrieval × 4 policy on 400  :p3, after p2, 7d
    section Phase 4 — Hardening
    Idempotency · isolation · races:p4, after p3, 5d
    section Phase 5 — Naive baseline
    Champion vs naive head-to-head :p5, after p4, 5d
    section Phase 6 — Polish
    Docker · OTel · approver UI    :p6, after p5, 4d
    section Phase 7 — Ship
    Tests · docs · system writeup  :p7, after p6, 3d
```

---

## 8. Where the actual prose lives

| Doc | What it answers |
|-----|-----------------|
| [docs/SYSTEM_DESIGN.md](docs/SYSTEM_DESIGN.md) | Component responsibilities, schema, invariants, scenarios |
| [docs/API.md](docs/API.md) | HTTP contracts for both services |
| [docs/POLICIES.md](docs/POLICIES.md) | Tenant policy model + Phase-3 4-engine comparison |
| [docs/DEMO_SCENARIO.md](docs/DEMO_SCENARIO.md) | Jane's mortgage walkthrough, step by step |
| [docs/RESEARCH_SURVEY.md](docs/RESEARCH_SURVEY.md) | Phase-1 production-AI-infrastructure survey |
| [docs/DEMO_VIDEO_SCRIPT.md](docs/DEMO_VIDEO_SCRIPT.md) | Five-minute recorded walkthrough script |
| [context_engine/README.md](context_engine/README.md) | Memory layer — modules, public API |
| [orchestrator/README.md](orchestrator/README.md) | Decision layer — modules, public API |
| [contracts/README.md](contracts/README.md) | Shared Pydantic models + observability shims |
| [benchmarks/README.md](benchmarks/README.md) | Phase-3 / Phase-5 harness anatomy |
| [tests/README.md](tests/README.md) | Test layout + how to run subsets |
| [takehome/README.md](takehome/README.md) | External-scorecard adapter notes |

This ARCHITECTURE.md is intentionally diagram-first. If a diagram looks
wrong, the prose docs win — they are versioned alongside the code that
implements them.
