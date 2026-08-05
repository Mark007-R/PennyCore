# AI-Customer-Ops-Engine

Production-grade AI infrastructure for customer-service AI agents in regulated industries — a memory layer (`context_engine`) and a decision layer (`orchestrator`), sharing one schema and one audit log.

Two services in one repo, backed by Postgres + Redis.

---

## What's in the box

| Component | Job |
|-----------|-----|
| [`context_engine/`](context_engine/README.md) | Ingest events from any channel, link customers across channels, assemble a token-budgeted brief for the LLM. Multiple retrieval strategies, prompt-injection defence, semantic cache. |
| [`orchestrator/`](orchestrator/README.md) | Listen to events, ask the LLM what to do, gate proposed actions through a tenant-specific policy engine, run an approval queue (with N-of-M quorum), execute, and audit. |
| [`contracts/`](contracts/README.md) | Shared Pydantic models (events, customers, actions, policies, audit, briefs) + observability shims. Imported by both services. |
| [`benchmarks/`](benchmarks/README.md) | Comparison harnesses and the load runner. |
| [`tests/`](tests/README.md) | pytest — unit · integration · adversarial. |
| `ui/` | Streamlit approver app and demo walkthrough. |
| `migrations/` | Alembic versions. |

---

## Running locally

```bash
docker-compose up                    # Postgres + Redis + both services
pytest
./scripts/run_takehome_evals.sh      # external evaluators
streamlit run ui/approver_app.py     # approver dashboard
streamlit run ui/demo_scenario_app.py
```

Real LLM keys go in `.env` (git-ignored); without keys the system runs in `MOCK_LLM` mode. See [`.env.example`](.env.example) for the variables.

---

## Guarantees (enforced by tests, not by convention)

1. **Multi-tenant.** Every query and every endpoint scopes by `tenant_id`. Cross-tenant access 404s with the same shape as missing-row.
2. **Idempotent.** Replays of the same `(tenant_id, event_id)` or `(tenant_id, idempotency_key)` are no-ops.
3. **Auditable.** Every proposal, policy decision, approval, rejection, and execution writes one row to `audit_log` keyed by `(tenant_id, action_id)` with a `caused_by_event_id` link.
4. **Provider-agnostic.** Every LLM call site goes through `context_engine.llm.get_client()`; no module reads API keys directly. Flipping `MOCK_LLM=true` runs the whole system with zero keys.
