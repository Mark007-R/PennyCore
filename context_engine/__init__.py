"""PennyCore context-engine — the memory layer.

Stores every customer interaction across channels, links customers across
channels, and assembles a token-budgeted brief for the LLM before each
reply. Day 4 ships the FastAPI scaffold (`api.py`); Day 5 wires the
ingestion endpoint and the LLM dispatch layer (`llm/`).
"""

__version__ = "0.1.0"
