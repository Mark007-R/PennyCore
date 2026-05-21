"""Loader for the Phase-3 orchestrator benchmark dataset.

Reads ``benchmarks/data/orchestrator/scenarios.jsonl`` + ``tenant_policies.
json`` and yields :class:`OrchestratorScenario` instances. The loader is
the only module the Day-17 comparison-study script should import for the
orchestrator side — it owns JSONL parsing, type construction, and the
deterministic iteration order (sorted by ``scenario_id``).

## Why a separate loader from ``benchmarks.dataset_loader``

The context-engine dataset (Day 12) and the orchestrator dataset
(Day 16) measure orthogonal things. The shapes barely overlap
(``BenchmarkPair`` carries a long customer history; ``Orchestrator
Scenario`` carries an Event envelope and a tenant policy table). Trying
to fold them into one loader would force a ``kind`` discriminator on
every read and obscure each study's domain. Two small loaders read
better than one polymorphic one.

## Construction shape mirrors :mod:`contracts.events`

``scenario.event`` is shaped to match the inbound ``Event`` Pydantic
model exactly (same field names, same ChannelType / EventType enum
values). The loader does NOT construct a real ``Event`` instance — the
Day-17 benchmark cares about the envelope dict and avoiding a Pydantic
validation pass per scenario keeps the benchmark fast. The fields ARE
validated by the dataset tests, so the shape is enforced upstream.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Iterator

DATA_DIR = Path(__file__).resolve().parent / "data" / "orchestrator"
SCENARIOS_PATH = DATA_DIR / "scenarios.jsonl"
POLICIES_PATH = DATA_DIR / "tenant_policies.json"


@dataclasses.dataclass(frozen=True)
class OrchestratorScenario:
    """One ``(event, tenant, expected_action_type, expected_decision)`` row.

    ``event`` is a dict matching the :class:`contracts.events.Event`
    field shape — the benchmark passes it through to the planner and
    policy engines as-is. ``brief_text`` is a short canned context
    stub (the orchestrator study isn't measuring retrieval quality;
    that's the context-engine study's job).
    """

    scenario_id: str
    tenant_id: str
    event: dict[str, Any]
    expected_action_type: str
    expected_decision: str
    intent: str
    difficulty: str
    tags: tuple[str, ...]
    rationale: str
    brief_text: str


def load_scenarios() -> list[OrchestratorScenario]:
    """Return all 200 scenarios sorted by ``scenario_id``."""
    out: list[OrchestratorScenario] = []
    with SCENARIOS_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            out.append(
                OrchestratorScenario(
                    scenario_id=row["scenario_id"],
                    tenant_id=row["tenant_id"],
                    event=dict(row["event"]),
                    expected_action_type=row["expected_action_type"],
                    expected_decision=row["expected_decision"],
                    intent=row["intent"],
                    difficulty=row["difficulty"],
                    tags=tuple(row.get("tags") or ()),
                    rationale=row["rationale"],
                    brief_text=row.get("brief_text", ""),
                )
            )
    out.sort(key=lambda s: s.scenario_id)
    return out


def load_tenant_policies() -> dict[str, dict[str, str]]:
    """Return the per-tenant policy tables.

    Shape: ``{tenant_id: {action_type_or_special_key: decision_str}}``.
    Identical to the dict the Day-10
    :class:`orchestrator.policy.declarative.DeclarativePolicyEngine`
    accepts via :meth:`set_policies`, so the Day-17 harness can plug
    this straight into every engine variant.
    """
    return json.loads(POLICIES_PATH.read_text(encoding="utf-8"))


def iter_scenarios(
    *,
    tenant_id: str | None = None,
    event_type: str | None = None,
    difficulty: str | None = None,
) -> Iterator[OrchestratorScenario]:
    """Iterate scenarios with optional single-axis filters.

    Multiple filters are AND-ed. For complex slices, call
    :func:`load_scenarios` and filter in user code — this helper covers
    the common cases (per-tenant correctness rollup, per-difficulty
    correctness rollup).
    """
    for s in load_scenarios():
        if tenant_id is not None and s.tenant_id != tenant_id:
            continue
        if event_type is not None and s.event.get("event_type") != event_type:
            continue
        if difficulty is not None and s.difficulty != difficulty:
            continue
        yield s
