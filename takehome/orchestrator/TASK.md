# Autonomy-Gated Agent Orchestrator

## Background

You are building the core of an AI agent orchestrator. The system receives
**domain events** (a document was uploaded, a borrower sent a message, a loan
status changed) and must decide what **actions** to take in response.

The catch: not all actions should execute automatically. Each tenant
(customer) configures **autonomy policies** that control which action types
the AI can execute on its own and which require a human to approve first.

```
Events --> [Your Orchestrator] --> [Policy Engine] --> Actions
                                                        |
                                          auto-executed  OR  queued for approval
```

## What to Build

Build a Python system that can:

1. **Ingest domain events** and use an LLM to reason about what actions to take
2. **Apply tenant-specific autonomy policies** to gate those actions
3. **Track action state** through a lifecycle (proposed → approved/rejected → executed)
4. **Surface actions needing approval** and allow humans to approve or reject them

Your system must implement the `AgentOrchestrator` protocol defined in
`evaluate.py`. **Read evaluate.py first** — it is both the interface contract
and the test suite.

## Running

```bash
pip install -r requirements.txt
cp .env.example .env   # add your OpenAI API key
python evaluate.py your_module.YourOrchestratorClass
```

The evaluator imports your class by dotted path, instantiates it, and runs
behavioral scenarios. It tests structural properties — not exact LLM outputs.

## OpenAI API Key

You have been provided a rate-limited OpenAI API key. Use it for the
LLM-based planning step. Budget your calls — the evaluator runs a small
number of scenarios.

## What We Care About

This is a **design-heavy** assessment. We care more about your system
architecture than a polished implementation:

- How you model events, actions, and policies
- How you separate concerns
- How you handle edge cases (duplicate events, stale approvals, etc.)
- Whether your system is auditable

## Deliverables

- Your implementation (Python modules)
- A passing `python evaluate.py your_module.YourClass` run
- **(Bonus)** A `DESIGN.md` explaining your decisions, tradeoffs, and any
  diagrams you want to include

## Constraints

- Python 3.11+
- You may add dependencies to `requirements.txt`
- Do not modify `evaluate.py`
- No Docker or external services (besides the OpenAI API)
