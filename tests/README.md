# tests

871 passing, 10 skipped (Postgres-gated).
Core-package coverage: 90% as of Day 33.

```
tests/
├── unit/           # ~700 fast, in-process, mock-LLM
├── integration/    # ~150 multi-module, including Postgres-gated
└── adversarial/    # prompt-injection + future red-team
```

## Layout

### `unit/`
One file per module. Fast (no Postgres, no Redis, no network). Mock
LLM by default. Includes the four big policy / strategy comparison
files (`test_declarative_policy.py`, `test_naive_policy.py`,
`test_llm_judge_policy.py`, plus `test_python_rules` coverage inside
`test_decision_pipeline.py`). The LLM-adapter coverage tests
(`test_llm_clients.py`, `test_llm_dispatch.py`) inject fake SDK
modules via `monkeypatch.setitem(sys.modules, ...)` so they exercise
the adapter construction paths without touching the network. Same
shape for `test_observability_noop.py` (Day 33 backfill —
monkeypatches the `_OTEL_API_AVAILABLE` flag).

### `integration/`
- `test_end_to_end_jane_scenario.py` — the canonical Day-11
  end-to-end happy path. Three events, three actions, every state
  shape. Runs against the in-memory bus + repos by default.
- `test_idempotency.py` — **20 tests** covering every replay surface
  (ingestion pure + HTTP, pipeline dedup, approval-queue double-
  decision, audit-chain immutability, threaded races). Phase 4 Day 19.
- `test_tenant_isolation.py` — **15 tests** across 6 surfaces
  (events, customers, action store, approval queue, audit, HTTP).
  Found and closed two real HTTP leak surfaces. Phase 4 Day 20.
- `test_race_conditions.py` — **10 tests** across 7 concurrent
  surfaces. The tenth (linker TOCTOU) was a real bug; the rest were
  safe by Phase-2 construction. Phase 4 Day 21.
- `test_load_runner.py` — wraps `benchmarks/local_load_runner.py`
  for a CI-friendly load smoke (no Locust UI needed).
- `test_parallel_approval_workflow.py` — Phase 5 Day 26 end-to-end
  for N-of-M quorum.
- `test_pg_repositories.py` — Postgres adapter integration. The 10
  skipped tests live here; they run when `DATABASE_URL` is set.

### `adversarial/`
- `test_prompt_injection.py` — red-team coverage for the safety
  filter in `context_engine/safety/prompt_injection.py`. Phase 4
  Day 23.

## Running

```bash
pytest                                       # full suite, 871 passing
pytest tests/unit                            # fast subset
pytest tests/integration                     # multi-module
pytest tests/adversarial                     # red-team

# Coverage on core packages
pytest --cov=context_engine --cov=orchestrator --cov=contracts \
       --cov-report=term-missing

# Postgres-gated tests (skipped without DATABASE_URL)
DATABASE_URL=postgresql://localhost/pennycore_test pytest tests/integration

# Watch one module while iterating
pytest tests/unit/test_brief_assembly.py -vv
```

## Markers & gates

- No custom markers. Postgres-gated tests use a runtime skip on
  `DATABASE_URL` rather than a `@pytest.mark.postgres` marker — the
  visible skip count in CI signals what's gated.
- Mock LLM is the default. Tests that need a real provider use a
  `@pytest.mark.skipif(no_real_key, ...)` guard.
- Adversarial tests run by default (no special opt-in) — they're
  cheap and they're the kind of regression you want surfaced on
  every CI pass.

## Coverage map (Day 33 snapshot)

| Module | Coverage |
|--------|----------|
| `context_engine/llm/*` | 100% |
| `context_engine/repository.py` | 92% |
| `context_engine/customer_repository.py` | 95% |
| `context_engine/linking.py` | 99% |
| `orchestrator/decision_pipeline.py` | 99% |
| `orchestrator/approval_queue.py` | 97% |
| `orchestrator/audit.py` | 100% |
| `orchestrator/policy/declarative.py` | 100% |
| `orchestrator/policy/naive.py` | 84% |
| `contracts/observability.py` | 88% |
| `context_engine/pg_*_repository.py` | 25–29% (Postgres-gated) |
| **TOTAL — core packages** | **90%** |

The Postgres-only adapters under 80% are correctly gated to the
`tests/integration/test_pg_repositories.py` suite that needs
`DATABASE_URL` — the in-memory equivalents that CI always runs sit
at 92–95%.
