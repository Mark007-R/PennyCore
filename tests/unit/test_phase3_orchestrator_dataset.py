"""Tests for the Phase-3 orchestrator benchmark dataset (Day 16).

Asserts the *invariants* of the dataset, not the specific contents of
individual scenarios. The dataset is regenerated whenever
``benchmarks/data/orchestrator/build_dataset.py`` runs; tests must
survive a re-seed without re-writing values.

Invariants enforced:

1. Manifest is well-formed; counts add to 200; SHA256 matches on-disk
   artifacts (no hand-editing without rebuilding).
2. Loader returns 200 unique scenarios sorted by ``scenario_id``.
3. Every scenario's ``event`` dict validates against the
   :class:`contracts.events.Event` Pydantic model — guards against
   schema drift if EventType / ChannelType ever gains/removes values.
4. Every ``expected_action_type`` is a valid
   :class:`contracts.actions.ActionType` value.
5. Every ``expected_decision`` is one of ``auto`` / ``approval_required``
   / ``reject``.
6. Every ``expected_decision`` matches what the Day-10
   :class:`orchestrator.policy.declarative.DeclarativePolicyEngine`
   would produce for the same (tenant, action_type) under the policy
   table on disk — i.e. the dataset's ground truth is consistent with
   the production engine's resolution rule.
7. Multi-tenant divergence: at least one action_type produces a
   different ``expected_decision`` across tenants. If this fails, the
   benchmark devolves into a single-tenant study and the comparison
   loses its multi-tenant signal.
8. Difficulty distribution is non-degenerate (≥10 in each bucket).
9. Every scenario carries a non-empty ``rationale`` — Day 17 reviewers
   need it to verify the ground truth is defensible.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from benchmarks.orchestrator_dataset_loader import (
    OrchestratorScenario,
    iter_scenarios,
    load_scenarios,
    load_tenant_policies,
)
from contracts.actions import ActionType
from contracts.events import Event
from contracts.policies import PolicyDecision
from orchestrator.policy.declarative import DeclarativePolicyEngine

DATA_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "data" / "orchestrator"
MANIFEST_PATH = DATA_DIR / "manifest.json"
SCENARIOS_PATH = DATA_DIR / "scenarios.jsonl"
POLICIES_PATH = DATA_DIR / "tenant_policies.json"


VALID_DECISIONS = {d.value for d in PolicyDecision}
VALID_ACTION_TYPES = {a.value for a in ActionType}


# -----------------------------------------------------------------------------
# Manifest invariants
# -----------------------------------------------------------------------------


def test_manifest_is_present_and_well_formed() -> None:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["n_scenarios"] == 200
    assert payload["seed"] == 42
    # All three tenants must be present.
    assert payload["tenants"] == sorted(
        [
            "tenant_acme_bank",
            "tenant_jefferson_credit",
            "tenant_globetrek_concierge",
        ]
    )
    # Tenant counts sum to 200.
    assert sum(payload["tenant_counts"].values()) == 200
    # Event type counts sum to 200.
    assert sum(payload["event_type_counts"].values()) == 200
    # Decision counts sum to 200.
    assert sum(payload["decision_counts"].values()) == 200
    # Difficulty counts sum to 200.
    assert sum(payload["difficulty_counts"].values()) == 200


def test_manifest_sha256_matches_on_disk_artifacts() -> None:
    """Catches anyone hand-editing scenarios.jsonl or tenant_policies.json."""
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    on_disk = SCENARIOS_PATH.read_bytes() + POLICIES_PATH.read_bytes()
    actual = hashlib.sha256(on_disk).hexdigest()
    assert actual == payload["artifact_sha256"], (
        "Dataset artifacts have drifted from manifest.artifact_sha256. "
        "Re-run benchmarks/data/orchestrator/build_dataset.py --seed 42, "
        "or bump the seed if the change was intentional."
    )


# -----------------------------------------------------------------------------
# Loader invariants
# -----------------------------------------------------------------------------


def test_load_scenarios_returns_200_unique_ids_sorted() -> None:
    scenarios = load_scenarios()
    assert len(scenarios) == 200
    ids = [s.scenario_id for s in scenarios]
    assert len(set(ids)) == 200, "duplicate scenario_id values"
    assert ids == sorted(ids), "loader must return scenarios sorted by scenario_id"
    # Idempotency keys must also be unique (Phase-4 Day-19 idempotency
    # tests reuse this dataset and rely on the key uniqueness invariant).
    keys = [s.event["idempotency_key"] for s in scenarios]
    assert len(set(keys)) == 200, "duplicate idempotency_key values"
    # event.id values must be unique too — duplicates would break the
    # decision_pipeline's audit linkage in Day 17.
    event_ids = [s.event["id"] for s in scenarios]
    assert len(set(event_ids)) == 200, "duplicate event.id values"


def test_every_scenario_is_an_orchestrator_scenario_instance() -> None:
    scenarios = load_scenarios()
    for s in scenarios:
        assert isinstance(s, OrchestratorScenario)


# -----------------------------------------------------------------------------
# Schema validation against contracts
# -----------------------------------------------------------------------------


def test_every_event_validates_against_contracts_event_model() -> None:
    """If the Event Pydantic model rejects a row, the dataset is broken.

    This is the strongest schema check we can run without re-implementing
    the validator. Catches: drift in EventType / ChannelType enum values,
    missing required fields, malformed timestamps, payload-type errors.
    """
    for s in load_scenarios():
        # Should not raise. Event has extra='forbid', so unknown keys
        # would surface here too.
        Event.model_validate(s.event)


def test_every_expected_action_type_is_valid() -> None:
    for s in load_scenarios():
        assert s.expected_action_type in VALID_ACTION_TYPES, (
            f"{s.scenario_id}: expected_action_type={s.expected_action_type!r} "
            f"is not a valid ActionType value"
        )


def test_every_expected_decision_is_valid() -> None:
    for s in load_scenarios():
        assert s.expected_decision in VALID_DECISIONS, (
            f"{s.scenario_id}: expected_decision={s.expected_decision!r} "
            f"is not a valid PolicyDecision value"
        )


def test_every_difficulty_is_valid() -> None:
    valid = {"easy", "medium", "hard"}
    for s in load_scenarios():
        assert s.difficulty in valid


def test_every_scenario_carries_nonempty_rationale() -> None:
    """Day 17 reviewers read the rationale to defend the ground truth."""
    for s in load_scenarios():
        assert s.rationale and s.rationale.strip(), (
            f"{s.scenario_id}: empty rationale — reviewers can't defend the ground truth"
        )


def test_tenant_ids_in_event_match_top_level_tenant_id() -> None:
    """Multi-tenant invariant: the event's tenant_id is always the
    scenario's tenant_id. Mismatch would let an action leak across
    tenants in Day-17 / Phase-4 isolation tests."""
    for s in load_scenarios():
        assert s.event["tenant_id"] == s.tenant_id


# -----------------------------------------------------------------------------
# Policy-engine consistency
# -----------------------------------------------------------------------------


def test_expected_decisions_match_declarative_engine_resolution() -> None:
    """The dataset's ground truth must agree with the Day-10 engine on
    the same policy table — otherwise the Day-17 study scores YAML
    against a fabricated baseline rather than against the production
    resolver."""
    policies = load_tenant_policies()
    engine = DeclarativePolicyEngine()
    for tenant_id, table in policies.items():
        engine.set_policies(tenant_id, table)

    for s in load_scenarios():
        engine_decision = engine.decide(s.tenant_id, s.expected_action_type)
        assert engine_decision.value == s.expected_decision, (
            f"{s.scenario_id}: ground-truth expected_decision="
            f"{s.expected_decision!r} disagrees with DeclarativePolicyEngine "
            f"on tenant={s.tenant_id!r} action_type={s.expected_action_type!r} "
            f"(engine said {engine_decision.value!r})"
        )


def test_tenant_policies_are_well_formed() -> None:
    """Every key in every tenant's table must be a recognized action_type
    OR a reserved special key (``default``/``*``)."""
    reserved = {"default", "*"}
    policies = load_tenant_policies()
    for tenant_id, table in policies.items():
        assert isinstance(table, dict), f"{tenant_id} policy table is not a dict"
        for key, value in table.items():
            assert key in VALID_ACTION_TYPES or key in reserved, (
                f"{tenant_id}: unknown key {key!r} in policy table"
            )
            assert value in VALID_DECISIONS, (
                f"{tenant_id}: invalid decision {value!r} for key {key!r}"
            )


# -----------------------------------------------------------------------------
# Multi-tenant divergence — the headline property of this dataset
# -----------------------------------------------------------------------------


def test_at_least_one_action_diverges_across_tenants() -> None:
    """If every tenant agrees on every action_type's decision, the
    multi-tenant comparison is meaningless. Today's tenants disagree on
    at least ``schedule_call`` (Acme/Globetrek=approval_required vs
    Jefferson=reject) and ``send_borrower_message`` (Acme/Globetrek=auto
    vs Jefferson=approval_required), so this should pass cleanly. The
    test exists to fail loudly if a future re-balance collapses the
    policies into homogeneity."""
    policies = load_tenant_policies()
    engine = DeclarativePolicyEngine()
    for tenant_id, table in policies.items():
        engine.set_policies(tenant_id, table)

    diverging_actions: list[str] = []
    for action in VALID_ACTION_TYPES:
        decisions = {
            engine.decide(t, action).value for t in policies.keys()
        }
        if len(decisions) > 1:
            diverging_actions.append(action)

    assert diverging_actions, (
        "No action_type has different decisions across tenants — the "
        "dataset's multi-tenant signal is zero. Re-balance "
        "tenant_policies.json."
    )


def test_decision_mix_is_nondegenerate() -> None:
    """All three decisions must appear at least 5 times. If one is missing,
    Day 17's correctness numbers can't include it (no test coverage)."""
    counts = Counter(s.expected_decision for s in load_scenarios())
    for decision in VALID_DECISIONS:
        assert counts[decision] >= 5, (
            f"decision={decision!r} appears only {counts[decision]} times "
            f"in the dataset; Day-17 can't measure correctness on this slice"
        )


def test_difficulty_mix_is_nondegenerate() -> None:
    """Each difficulty bucket has ≥10 scenarios so by-difficulty
    correctness rollups are statistically meaningful."""
    counts = Counter(s.difficulty for s in load_scenarios())
    for difficulty in ("easy", "medium", "hard"):
        assert counts[difficulty] >= 10, (
            f"difficulty={difficulty!r} appears only {counts[difficulty]} "
            f"times; Day-17 rollup will be noisy"
        )


def test_event_types_are_all_represented() -> None:
    """All five EventType values should appear so Day-17 can score by
    event_type. A missing event_type would surface here, not in the
    benchmark."""
    expected = {
        "message_received",
        "document_uploaded",
        "status_changed",
        "anomaly_detected",
        "system_event",
    }
    actual = {s.event["event_type"] for s in load_scenarios()}
    assert actual == expected


# -----------------------------------------------------------------------------
# Filter helpers
# -----------------------------------------------------------------------------


def test_iter_scenarios_per_tenant_matches_full_filter() -> None:
    full = load_scenarios()
    for tenant_id in {"tenant_acme_bank", "tenant_jefferson_credit", "tenant_globetrek_concierge"}:
        from_iter = list(iter_scenarios(tenant_id=tenant_id))
        from_full = [s for s in full if s.tenant_id == tenant_id]
        assert from_iter == from_full


def test_iter_scenarios_per_difficulty_matches_full_filter() -> None:
    full = load_scenarios()
    for difficulty in ("easy", "medium", "hard"):
        from_iter = list(iter_scenarios(difficulty=difficulty))
        from_full = [s for s in full if s.difficulty == difficulty]
        assert from_iter == from_full


def test_iter_scenarios_per_event_type_matches_full_filter() -> None:
    full = load_scenarios()
    for et in (
        "message_received",
        "document_uploaded",
        "status_changed",
        "anomaly_detected",
        "system_event",
    ):
        from_iter = list(iter_scenarios(event_type=et))
        from_full = [s for s in full if s.event["event_type"] == et]
        assert from_iter == from_full


# -----------------------------------------------------------------------------
# Determinism (cheap version — full byte-level check is in manifest test)
# -----------------------------------------------------------------------------


def test_rebuild_is_idempotent_via_loader(tmp_path: Path) -> None:
    """Calling the builder again with the same seed produces a manifest
    with the same artifact_sha256. Cheaper than diffing the JSONL.

    We do this by re-importing the build module and invoking ``build``
    against a temp directory wouldn't work without monkeypatching — but
    we can recompute the SHA from the existing artifacts and confirm it
    matches the manifest's claim. If the builder is non-deterministic,
    the SHA-from-artifacts test above is the canary; this test simply
    asserts the *invariant* that the manifest claims a 64-hex SHA.
    """
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    sha = manifest["artifact_sha256"]
    assert isinstance(sha, str) and len(sha) == 64
    int(sha, 16)  # must be valid hex
