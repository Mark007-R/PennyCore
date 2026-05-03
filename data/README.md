# Data sources

- `data/samples/` — small synthetic fixtures used by unit tests. Clearly-fake
  names (Jane Doe, Acme Bank). Committed.
- `data/local/` — local dev data. **Never committed** (in `.gitignore`).

Phase 3 benchmark data lives in `benchmarks/data/` (200 customer histories
+ 200 queries + 200 policy scenarios), seeded from MultiWOZ for the dialogue
component and hand-validated for the banking component.
