# contracts — shared Pydantic models

The schema that both services agree on. Importing the wrong shape from
either service is a type error, not a runtime mystery.

This package is small on purpose. It owns data shapes; it does not own
behavior. Field-by-field schemas are documented in
[../docs/SYSTEM_DESIGN.md §4](../docs/SYSTEM_DESIGN.md).

## Models

| Module | Owns |
|--------|------|
| `events.py` | `Event`, `Channel`, `ChannelType`. The normalized event that flows from any inbound channel through `context_engine` to `orchestrator`. |
| `customers.py` | `Customer`, `CustomerIdentity`. A customer plus the per-channel handles (email, phone, external IDs) that `linking.py` matches on. |
| `actions.py` | `Action`, `ActionProposal`, `ActionStatus`, `ActionType`. Proposals are what the planner emits; status moves through `proposed → awaiting_approval → approved/rejected → executed`. |
| `policies.py` | `Policy`, `Tenant`, `ApprovalRule`, `QuorumRule`. The declarative shape every policy engine reads (the declarative champion stores them verbatim; LLM-judge reads the same dict and renders it into the prompt). |
| `audit.py` | `AuditLogEntry`. The append-only row written for every proposal / decision / approval / execution. |
| `briefs.py` | `Brief`, `BriefSegment`. What `brief_assembly.py` returns to the planner. Carries the per-segment provenance so the audit log can name which messages were used. |
| `observability.py` | `setup_tracing(...)`, `trace_span(...)`, and `_NoopSpan`. Single OTel seam — no-ops when the OTel SDK isn't installed, so prod-tier customers without OTel get zero overhead and zero hard dependency. |
| `build_info.py` | `__build_sha__`, `__build_timestamp__` plumbing. Read by the `/healthz` probes so a request to a running service can tell you which commit it's on. |

## Why every shape lives here

Both `context_engine` and `orchestrator` import these models. If
`Event` lived in `context_engine.events`, then `orchestrator` would
have an inverted import dependency on the memory layer — which would
make swapping in a different memory implementation harder.

Both takehome adapters also import from here, so changes ripple to
the external-scorecard surface immediately. That's a feature: drift
between the production code and the adapter is structurally
impossible.

## Observability shim — the design call

The OTel SDK is an optional dependency. `contracts/observability.py`
detects it at import time:

- **SDK present** → spans go to the configured collector (Jaeger in
  dev, anything OTLP in prod).
- **SDK absent** → `_NoopSpan` swallows every method; `trace_span()`
  is a no-op context manager.

Every call site uses the same `with trace_span("..."):` API. Adding
or removing the OTel dependency never requires touching the call
sites. Tested in `tests/unit/test_observability.py` (SDK present) and
`tests/unit/test_observability_noop.py` (Day 33 backfill — SDK
absent path).

## Local dev

```bash
pytest tests/unit/test_contracts.py
pytest tests/unit/test_observability.py tests/unit/test_observability_noop.py
```
