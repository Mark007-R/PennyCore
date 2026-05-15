"""Loader for the Phase-3 benchmark dataset.

Joins ``queries.jsonl`` to ``customer_histories.jsonl`` by
``customer_id`` and yields one :class:`BenchmarkPair` per query. The
loader is the only module the comparison-study scripts (Days 13-15)
should import — it owns the JSONL parsing, type construction, and the
deterministic iteration order (sorted by ``pair_id``).

Two layers worth keeping straight:

* :class:`HistoryMessage` / :class:`HistoryAction` — the loader's
  duck-typed shapes. They satisfy the same Protocols
  (``_MessageLike`` / ``_ActionLike``) that
  ``context_engine.retrieval.recency`` already accepts, so the
  benchmark feeds these straight into the production retrieval code
  without an adapter layer. We intentionally *don't* construct
  ``contracts.Message`` because the production model requires a
  ``MessageDirection`` enum that the dataset doesn't carry — a
  benchmark-only field would compromise the contract.
* :class:`BenchmarkPair` — the joined view (history + query + truth +
  bucket). One per query; iteration order is by ``pair_id``.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

DATA_DIR = Path(__file__).resolve().parent / "data"
HISTORIES_PATH = DATA_DIR / "customer_histories.jsonl"
QUERIES_PATH = DATA_DIR / "queries.jsonl"


@dataclasses.dataclass(frozen=True)
class HistoryMessage:
    """One message in a customer's history.

    Field names match the takehome evaluator's ``Message`` dataclass and
    the ``_MessageLike`` Protocol in
    ``context_engine.retrieval.recency`` so this type drops straight
    into the production retrieval call without a wrapper.
    """

    channel: str
    sender: str
    content: str
    timestamp: datetime
    metadata: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class HistoryAction:
    """Prior action in a customer's history.

    Matches ``_ActionLike`` in
    ``context_engine.retrieval.recency`` and the takehome evaluator's
    ``Action`` shape. ``borrower_id`` and ``action_id`` are NOT carried
    here because the recency formatter never reads them and adding them
    would invent fields not present in the source dataset.
    """

    action_type: str
    details: dict[str, Any]
    timestamp: datetime


@dataclasses.dataclass(frozen=True)
class CustomerHistoryView:
    """A customer's full history as the benchmark sees it."""

    customer_id: str
    tenant_id: str
    messages: tuple[HistoryMessage, ...]
    actions: tuple[HistoryAction, ...]
    source: str
    source_ids: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class BenchmarkPair:
    """One ``(history, query, ground_truth)`` benchmark row."""

    pair_id: str
    customer_id: str
    tenant_id: str
    query: str
    ground_truth: str
    history_bucket: str
    history_message_count: int
    source: str
    intent: str
    tags: tuple[str, ...]
    history: CustomerHistoryView


def _parse_message(d: dict[str, Any]) -> HistoryMessage:
    return HistoryMessage(
        channel=d["channel"],
        sender=d["sender"],
        content=d["content"],
        timestamp=datetime.fromisoformat(d["timestamp"]),
        metadata=dict(d.get("metadata") or {}),
    )


def _parse_action(d: dict[str, Any]) -> HistoryAction:
    return HistoryAction(
        action_type=d["action_type"],
        details=dict(d.get("details") or {}),
        timestamp=datetime.fromisoformat(d["timestamp"]),
    )


def _load_histories() -> dict[str, CustomerHistoryView]:
    out: dict[str, CustomerHistoryView] = {}
    with HISTORIES_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            cid = row["customer_id"]
            out[cid] = CustomerHistoryView(
                customer_id=cid,
                tenant_id=row["tenant_id"],
                messages=tuple(_parse_message(m) for m in row["channel_messages"]),
                actions=tuple(_parse_action(a) for a in row["prior_actions"]),
                source=row["source"],
                source_ids=tuple(row["source_ids"]),
            )
    return out


def load_pairs() -> list[BenchmarkPair]:
    """Return all 200 pairs sorted by ``pair_id``.

    Joins queries to histories in memory (the dataset is ~2.5MB). If
    a query references a customer not present in the history file, the
    loader raises — that's a build-time invariant the benchmark can
    rely on.
    """
    histories = _load_histories()
    pairs: list[BenchmarkPair] = []
    with QUERIES_PATH.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            cid = row["customer_id"]
            if cid not in histories:
                raise KeyError(
                    f"queries.jsonl references customer_id={cid!r} which has no "
                    f"history in customer_histories.jsonl — rebuild the dataset"
                )
            pairs.append(
                BenchmarkPair(
                    pair_id=row["pair_id"],
                    customer_id=cid,
                    tenant_id=row["tenant_id"],
                    query=row["query"],
                    ground_truth=row["ground_truth"],
                    history_bucket=row["history_bucket"],
                    history_message_count=row["history_message_count"],
                    source=row["source"],
                    intent=row["intent"],
                    tags=tuple(row.get("tags") or ()),
                    history=histories[cid],
                )
            )
    pairs.sort(key=lambda p: p.pair_id)
    return pairs


def iter_pairs(*, bucket: str | None = None) -> Iterator[BenchmarkPair]:
    """Iterate pairs, optionally filtered to a single length bucket.

    The bucket filter is the common case for "did short pairs win?"
    style slicing. For arbitrary filters, call :func:`load_pairs` and
    filter in user code.
    """
    for p in load_pairs():
        if bucket is None or p.history_bucket == bucket:
            yield p
