# takehome — external-scorecard adapters

PennyCore was built around an external take-home assessment harness.
The evaluator scripts (`evaluate.py`) live here unmodified; thin
adapter modules wrap the production packages to expose the external
interfaces.

This README explains the adapter pattern and the score the system
holds.

## Hard rule

`takehome/context-engine/evaluate.py` and
`takehome/orchestrator/evaluate.py` are **never** modified. Touching
them invalidates the grading. The adapter modules
(`memory_system.py`, `orchestrator_impl.py`) are the entire seam.

## Layout

```
takehome/
├── context-engine/
│   ├── evaluate.py           # external — NEVER modified
│   ├── memory_system.py      # adapter — implements MemorySystem Protocol
│   ├── DESIGN.md             # adapter design rationale
│   ├── SCORECARD.md          # per-scenario pass / fail history
│   ├── requirements.txt      # pinned to the external spec
│   └── .env                  # NOT committed — Azure keys for the external API
└── orchestrator/
    ├── evaluate.py           # external — NEVER modified
    ├── orchestrator_impl.py  # adapter — implements AgentOrchestrator ABC
    ├── DESIGN.md
    ├── SCORECARD.md
    ├── requirements.txt
    └── .env                  # NOT committed
```

## Adapter pattern

Each adapter does one job: translate between the production code's
shapes and the external evaluator's shapes.

```python
# takehome/context-engine/memory_system.py — sketch
from context_engine.repository import InMemoryRepository
from context_engine.brief_assembly import assemble_brief
from context_engine.retrieval.hybrid import HybridStrategy

class MemorySystem:  # implements the external Protocol
    def __init__(self) -> None:
        self._repo = InMemoryRepository()
        self._strategy = HybridStrategy()

    def add_message(self, customer_id: str, message: str) -> None:
        # translate external shape → production Event
        self._repo.write_event(_to_event(customer_id, message))

    def get_context(self, customer_id: str, query: str, token_budget: int) -> str:
        return assemble_brief(
            repo=self._repo,
            strategy=self._strategy,
            customer_id=customer_id,
            query=query,
            token_budget=token_budget,
        ).text
```

The adapter:
- Calls into the **same** modules every other call site uses. No
  parallel implementation, no copied logic. If a fix lands in
  `brief_assembly.py`, the takehome adapter inherits it for free.
- Pins the external dependency surface in `requirements.txt` so the
  external grader sees the exact versions the spec calls out.
- Reads its own `.env` (`takehome/*/.env` is in `.gitignore`) so the
  external "OpenAI-compatible API" requirement is satisfied via
  Azure AI Foundry — independently from whatever `LLM_PROVIDER` the
  main PennyCore stack runs.

## Score (Day 26 snapshot, last weekly re-validation)

| Adapter | Score | Notes |
|---------|-------|-------|
| context-engine | **5/5** non-LLM | Scenario 6 needs the `openai` SDK in the takehome venv; the adapter otherwise passes. |
| orchestrator | **6/6** | All scenarios pass — including the multi-tenant + idempotency + audit scenarios. |

Held across all Phase-6 additive days, re-validated weekly.

## Running

```bash
# Both evaluators
./scripts/run_takehome_evals.sh

# Just context-engine
cd takehome/context-engine && python evaluate.py

# Just orchestrator
cd takehome/orchestrator && python evaluate.py
```

`run_takehome_evals.sh` activates the right venv, ensures `.env` is
present (warns if not), and writes the scorecard to `results/`.

## Why the adapter pattern is the right call

A naive read of the takehome spec would have you implement the
evaluator's interfaces directly. That gets you a passing scorecard
on day 11 — and a piece of code that diverges from the production
PennyCore codebase the moment the second priority lands.

The adapter pattern keeps **one implementation**, two interfaces.
The production HTTP API and the external Protocol are both views
onto the same modules. The cost (one extra translation layer per
adapter, ~80 lines each) buys structurally-impossible drift between
the system the user runs and the system the grader scores.
