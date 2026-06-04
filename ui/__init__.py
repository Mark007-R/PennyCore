"""PennyCore Streamlit surfaces (Phase 6).

`ui/` is the human-facing layer that sits on top of the orchestrator's
in-memory state. Two apps land here:

  * `approver_app.py`        — Day 31. The pending-action dashboard
                                 for compliance / operations.
  * `demo_scenario_app.py`   — Day 32. Jane's mortgage walkthrough.

The data layer for both apps is `ui.approver_data`, a thin wrapper
around `orchestrator.api._pipeline` that:

  * Surfaces the data each panel needs as plain dicts (Streamlit
    renders dicts cleanly; full Pydantic models leak internals).
  * Catches the pipeline's typed exceptions and turns them into a
    `ApproverActionError` carrying a user-readable message — keeps
    the Streamlit handler clean.
  * Is fully unit-testable without spinning up Streamlit (Streamlit
    is rendered at import time which makes direct UI tests fragile;
    we test the adapter exhaustively instead).
"""
