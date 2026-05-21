"""Tests for the Phase-3 benchmark dataset + harness skeleton (Day 12).

These tests assert the *invariants* of the dataset, not the values of
specific pairs — that way the dataset can be regenerated with a
different seed without breaking the suite, while still catching the
classes of bugs we care about (missing customer reference, schema
drift, harness-can't-load-the-data).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from benchmarks import context_engine_bench
from benchmarks.context_engine_bench import (
    DEFAULT_TOKEN_BUDGET,
    list_strategies,
    register_strategy,
    run,
)
from benchmarks.dataset_loader import (
    BenchmarkPair,
    CustomerHistoryView,
    HistoryAction,
    HistoryMessage,
    iter_pairs,
    load_pairs,
)
from contracts import BriefSegment, SegmentSource

DATA_DIR = Path(__file__).resolve().parents[2] / "benchmarks" / "data"
MANIFEST_PATH = DATA_DIR / "manifest.json"


# -----------------------------------------------------------------------------
# Manifest invariants
# -----------------------------------------------------------------------------


def test_manifest_is_present_and_well_formed() -> None:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["n_pairs"] == 200
    assert payload["n_histories"] == 200
    assert payload["source_counts"] == {"banking_template": 50, "multiwoz": 150}
    assert payload["bucket_counts"] == {
        "short": 100,
        "medium": 60,
        "long": 30,
        "very_long": 10,
    }
    # The artifacts SHA should match the bytes on disk — guards against
    # somebody hand-editing the JSONL files.
    payload_bytes = (
        (DATA_DIR / "customer_histories.jsonl").read_bytes()
        + (DATA_DIR / "queries.jsonl").read_bytes()
    )
    import hashlib

    actual = hashlib.sha256(payload_bytes).hexdigest()
    assert actual == payload["artifact_sha256"], (
        "Dataset artifacts have drifted from manifest.artifact_sha256. "
        "Re-run benchmarks/data/build_dataset.py --seed 42, or update the seed "
        "if the change was intentional."
    )


# -----------------------------------------------------------------------------
# Loader invariants
# -----------------------------------------------------------------------------


def test_load_pairs_returns_200_unique_pair_ids() -> None:
    pairs = load_pairs()
    assert len(pairs) == 200
    pair_ids = [p.pair_id for p in pairs]
    assert len(set(pair_ids)) == 200
    # Sorted iteration order is part of the loader's contract.
    assert pair_ids == sorted(pair_ids)


def test_every_pair_resolves_to_a_history() -> None:
    pairs = load_pairs()
    for p in pairs:
        assert isinstance(p.history, CustomerHistoryView)
        assert p.history.customer_id == p.customer_id
        assert p.history.tenant_id == p.tenant_id


def test_messages_are_chronological_within_each_history() -> None:
    """Older first → newest last is the contract the recency strategy assumes.

    This is what the builder produces: ascending timestamps. If the
    builder ever ships out-of-order messages, the recency strategy will
    silently sort them anyway, but we want the invariant asserted at
    the dataset boundary so we catch the regression here, not in a
    head-scratching benchmark.
    """
    for p in load_pairs():
        ts = [m.timestamp for m in p.history.messages]
        assert ts == sorted(ts), (
            f"customer_id={p.customer_id} has out-of-order timestamps"
        )


def test_message_counts_match_their_bucket() -> None:
    bands = {
        "short": (10, 30),
        "medium": (31, 100),
        "long": (101, 300),
        "very_long": (301, 500),
    }
    for p in load_pairs():
        lo, hi = bands[p.history_bucket]
        assert lo <= p.history_message_count <= hi, (
            f"{p.pair_id}: bucket={p.history_bucket} but "
            f"n_messages={p.history_message_count} is out of [{lo},{hi}]"
        )
        # And the recorded count must equal the actual list length.
        assert p.history_message_count == len(p.history.messages)


def test_queries_and_ground_truths_are_nonempty_strings() -> None:
    for p in load_pairs():
        assert isinstance(p.query, str) and p.query.strip()
        assert isinstance(p.ground_truth, str) and p.ground_truth.strip()


def test_message_and_action_shapes_are_protocol_compatible() -> None:
    """The recency strategy duck-types on attributes — assert them here."""
    pairs = load_pairs()
    sample_msg = pairs[0].history.messages[0]
    assert isinstance(sample_msg, HistoryMessage)
    for attr in ("channel", "sender", "content", "timestamp", "metadata"):
        assert hasattr(sample_msg, attr)
    # The first 50 (banking-template) pairs always carry actions.
    banking = next(p for p in pairs if p.source == "banking_template" and p.history.actions)
    sample_act = banking.history.actions[0]
    assert isinstance(sample_act, HistoryAction)
    for attr in ("action_type", "details", "timestamp"):
        assert hasattr(sample_act, attr)


def test_iter_pairs_filter_matches_load_pairs() -> None:
    full = load_pairs()
    for bucket in ("short", "medium", "long", "very_long"):
        from_iter = list(iter_pairs(bucket=bucket))
        from_full = [p for p in full if p.history_bucket == bucket]
        assert from_iter == from_full


def test_tenants_are_covered_for_multitenant_isolation_use() -> None:
    """Phase 4 isolation tests reuse this dataset — assert ≥2 tenants."""
    tenants = {p.tenant_id for p in load_pairs()}
    # Three is what the builder produces today. Asserting ≥ 2 keeps the
    # test stable if the builder later collapses concierge into a bank
    # tenant or vice versa.
    assert len(tenants) >= 2


# -----------------------------------------------------------------------------
# Harness invariants
# -----------------------------------------------------------------------------


def test_recency_strategy_is_registered_at_import_time() -> None:
    assert "recency" in list_strategies()


def test_register_strategy_is_idempotent_per_name() -> None:
    """Last-write-wins re-registration so notebook re-imports don't blow up."""
    sentinel: list[BriefSegment] = []

    def _stub(pair: BenchmarkPair, budget: int) -> list[BriefSegment]:
        return sentinel

    try:
        register_strategy("recency_test_stub", _stub)
        assert "recency_test_stub" in list_strategies()
        register_strategy("recency_test_stub", _stub)  # second time = no error
    finally:
        # Clean up the registry to keep test isolation.
        context_engine_bench._STRATEGIES.pop("recency_test_stub", None)


def test_run_unknown_strategy_raises() -> None:
    pairs = load_pairs()[:1]
    with pytest.raises(KeyError, match="not_a_strategy"):
        run(pairs=pairs, strategies=["not_a_strategy"])


def test_smoke_run_recency_baseline_on_5_pairs() -> None:
    """Day 12 acceptance test: harness boots end-to-end with the baseline."""
    pairs = load_pairs()[:5]
    results = run(pairs=pairs, strategies=["recency"], token_budget=8000)

    assert len(results) == 5
    for r in results:
        assert r.strategy == "recency"
        assert r.brief_tokens_estimate <= r.token_budget, (
            "Brief assembler invariant violated: brief exceeds token budget"
        )
        assert r.brief_text, "Empty brief — recency baseline should always pack something"
        assert r.retrieval_latency_ms >= 0
        # Source mix is recorded; recency should never use semantic_match
        # or summary because those are different strategies.
        seen_sources = set(r.segment_source_counts)
        assert seen_sources <= {
            SegmentSource.RECENT_MESSAGE.value,
            SegmentSource.PRIOR_ACTION.value,
        }, f"recency strategy produced unexpected sources: {seen_sources}"


def test_smoke_run_summary_records_p50_and_p95() -> None:
    pairs = load_pairs()[:5]
    results = run(pairs=pairs, strategies=["recency"], token_budget=8000)
    summary = context_engine_bench._summarize(results)
    assert "recency" in summary
    s = summary["recency"]
    assert s["n_pairs"] == 5
    assert "p50" in s["retrieval_latency_ms"]
    assert "p95" in s["retrieval_latency_ms"]


def test_default_token_budget_is_documented_8k() -> None:
    """Document the contract: the bench's default budget is 8K tokens.

    Days 13-15 will report cost numbers indexed against this budget.
    Changing it later means re-running every strategy, so we lock it
    here as a regression alarm.
    """
    assert DEFAULT_TOKEN_BUDGET == 8000
