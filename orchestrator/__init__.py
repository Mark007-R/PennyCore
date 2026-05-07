"""PennyCore orchestrator — the decision layer.

Listens to events from context-engine, asks the LLM what to do, applies
tenant-specific policy rules, manages a human-approval queue, prevents
double-action on duplicate events, and writes a full audit trail. Day 4
ships the FastAPI scaffold (`api.py`); Day 8-10 wires the event listener,
LLM planner, policy engine, and approval queue.
"""

__version__ = "0.1.0"
