# Takehome adapter — context-engine

This directory exists ONLY to grade against the external take-home
assessment scorecard. The evaluator (`evaluate.py`) is the upstream version,
unchanged.

The adapter `memory_system.py` (added Day 7) wraps the production
`context_engine` package to expose the `MemorySystem` Protocol that
`evaluate.py` imports. No business logic lives here — adapter-only.

The adapter is locked to `LLM_PROVIDER=azure` (see `.env`) so the spec's
"OpenAI API" requirement is satisfied even when the main PennyCore system is
running on Anthropic Claude.
