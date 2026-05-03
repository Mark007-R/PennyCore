# Takehome adapter — orchestrator

Grading-only directory for the external take-home assessment. The evaluator
(`evaluate.py`) is the upstream version, unchanged.

The adapter `orchestrator_impl.py` (added Day 10) wraps the production
`orchestrator` package to expose the `AgentOrchestrator` ABC that
`evaluate.py` imports. No business logic here — adapter-only.

Locked to `LLM_PROVIDER=azure` (see `.env`) for the spec's "OpenAI API"
requirement.
