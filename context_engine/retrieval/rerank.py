"""Two-stage retrieval — Day 24 Phase 5 strategy.

Re-rank the top-K candidates from
:func:`context_engine.retrieval.semantic.semantic_segments` with a
richer pairwise scoring function before the brief assembler packs.
This is the "retrieve-then-rerank" architecture used in production
semantic search (Cohere Rerank, Pinecone reranker, MS Marco
cross-encoders).

## Why two stages

The first stage (hashed-BoW cosine in :mod:`semantic`) is cheap:
``O(|history|)`` per query, deterministic, no model download. It's
right enough to surface the top ~30 candidates out of a 200-message
history. The second stage runs only on those ~30, so it can afford
features the first stage skips.

What the re-ranker adds over pure cosine:

1. **Bigram overlap.** Cosine over single tokens treats "mortgage
   rate" as two independent features. The re-ranker scores adjacent-
   word pairs too, so a segment that says "mortgage rate" outranks
   one that mentions "mortgage" and "rate" in separate sentences.
   This is the phrase-locality signal a real cross-encoder learns.
2. **Jaccard query-coverage.** Cosine on a hashed bag is dominated by
   segment-internal term frequency on long segments — a 200-word
   message about something tangential can outrank a 20-word message
   that's precisely on-topic if it happens to repeat one query token.
   Jaccard on sets pulls the signal back to "does this segment talk
   about the same things the query asks about", regardless of length.
3. **Light recency decay.** Exponential decay over message age in
   days, capped at 20% of the cosine weight. Customer-service
   conversations skew "recent matters more"; the cap keeps the
   re-rank from collapsing into recency on cold-tail histories.

Feature weights live in :data:`_RERANK_WEIGHTS` and are documented
with the rationale for each magnitude. Day 24 benchmark proves
whether the re-rank pays its (tiny) latency cost in quality.

## Why a deterministic proxy, not a real cross-encoder

``sentence-transformers/ms-marco-MiniLM-L-6-v2`` is the standard
production cross-encoder — ~80MB model, torch dependency, ~5-15ms
per pair. The same constraints that ruled out a neural first-stage
embedder (mock-mode reproducibility, container size, FP drift across
CPU/CUDA) apply to a neural re-ranker. We ship the deterministic
proxy today and document the swap-in path: replace
:func:`_pairwise_score` with a single
``model.predict([(query, segment_text)])`` call and the rest of the
module is unchanged.

## Top-K candidate pool

Default K = 30. Smaller than the 200-message max history so the
re-ranker is doing real work (not re-sorting everything). Past K=30
the first-stage cosine scores are near-noise, and re-ranking on
near-noise just amplifies whatever weak phrase-overlap signal the
tail happens to have — usually a quality loss, not a win.

Segments past the top K stay in the output (so the assembler can
fall back to them if the top-K doesn't fit the budget) but keep
their semantic-stage order. This means under tight budgets the
re-ranker dominates the head of the brief and recency-of-semantic
fills the tail.

## Token-budget contract

Same as :mod:`semantic`: this strategy returns every segment, sorted
by descending priority. Pack-budget enforcement is downstream in
:func:`context_engine.brief_assembly.pack_segments`.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable

from contracts import BriefSegment

from context_engine.retrieval.semantic import (
    _MessageLike,
    _ActionLike,
    semantic_segments,
)

# Default top-K candidate pool for re-ranking. Tuned so the K-th
# candidate's first-stage score is well below the top of the curve
# (typical: top score ~0.6, K=30 score ~0.05). Past K=30 we're
# re-ranking near-noise.
DEFAULT_TOP_K = 30

# Feature weights for the pairwise re-rank score. Documented per
# component so a tuning pass can adjust one without unintended
# cross-component side-effects.
#
# - cosine_carry: keep the first-stage signal as the dominant feature.
#   Re-rank shouldn't override the first-stage ranking on near-ties;
#   it should sharpen it.
# - bigram_overlap: the headline new signal. Weight 0.7 means a
#   perfect-bigram-match segment with weak cosine (e.g. 0.2) can
#   leapfrog a cosine-only-strong segment (e.g. 0.5), which is the
#   precise correction we want.
# - jaccard: 0.4 keeps it as a smoothing signal that lifts on-topic
#   short segments over long-tangential ones. Stronger weights make
#   the re-rank too lexical (loses cosine's stem-matching).
# - recency_decay: capped at 0.2 of total so it's a tiebreaker only.
#   Half-life 30 days — past 30 days the boost falls below 0.1 and
#   stops mattering against any positive content score.
_RERANK_WEIGHTS = {
    "cosine_carry": 1.0,
    "bigram_overlap": 0.7,
    "jaccard": 0.4,
    "recency_decay": 0.2,
}

# Recency half-life in days. Beyond this, the recency feature
# contributes <0.1 to the total, which is below the cosine signal's
# typical floor — so cold-tail segments aren't penalized just for
# being old.
_RECENCY_HALF_LIFE_DAYS = 30.0

_MIN_TOKEN_LEN = 2


def _tokens(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, drop very-short tokens.

    Order-preserving (returns a list, not a set) so bigram extraction
    can use it directly. Mirrors :func:`semantic._tokenize` but
    exposed at module-internal scope so changes here don't fork the
    first-stage tokenizer.
    """
    if not text:
        return []
    return [
        t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= _MIN_TOKEN_LEN
    ]


def _bigrams(tokens: list[str]) -> set[tuple[str, str]]:
    """Adjacent-word pairs from a token list. Empty for <2 tokens."""
    return {(tokens[i], tokens[i + 1]) for i in range(len(tokens) - 1)}


def _jaccard(a: set, b: set) -> float:
    """Set-Jaccard. Returns 0.0 for empty union."""
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _recency_score(seg_timestamp: datetime, *, now: datetime) -> float:
    """Exponential-decay recency feature, half-life 30 days.

    Returns 1.0 for a message timestamped *now*, 0.5 at 30 days old,
    ~0.06 at 120 days old. Naive-aware: if ``seg_timestamp`` is
    timezone-naive (which the benchmark data is, by design — see
    :mod:`benchmarks.dataset_loader`), we compare against the
    timezone-naive equivalent of ``now``. Negative ages (future
    timestamps) clamp to 0 days old, returning 1.0.
    """
    if seg_timestamp.tzinfo is None and now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    elif seg_timestamp.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_seconds = (now - seg_timestamp).total_seconds()
    age_days = max(0.0, age_seconds / 86400.0)
    return 0.5 ** (age_days / _RECENCY_HALF_LIFE_DAYS)


def _pairwise_score(
    *,
    cosine_carry: float,
    query_tokens_list: list[str],
    query_token_set: set[str],
    query_bigrams: set[tuple[str, str]],
    seg_text: str,
    seg_timestamp: datetime,
    now: datetime,
) -> float:
    """The feature-weighted re-rank score for one (query, segment) pair.

    This is the function a production swap would replace with
    ``cross_encoder.predict([(query, seg_text)])``. The signature is
    intentionally narrow — only the features the proxy needs — so
    the swap-in is mechanical.
    """
    seg_tokens_list = _tokens(seg_text)
    seg_token_set = set(seg_tokens_list)
    seg_bigrams = _bigrams(seg_tokens_list)

    jacc = _jaccard(query_token_set, seg_token_set)
    bigram_overlap = _jaccard(query_bigrams, seg_bigrams)
    recency = _recency_score(seg_timestamp, now=now)

    w = _RERANK_WEIGHTS
    return (
        w["cosine_carry"] * cosine_carry
        + w["bigram_overlap"] * bigram_overlap
        + w["jaccard"] * jacc
        + w["recency_decay"] * recency
    )


def _seg_timestamp_from_metadata(metadata: dict, *, fallback: datetime) -> datetime:
    """Read the segment's timestamp from its metadata.

    :func:`semantic.semantic_segments` stores the raw timestamp under
    ``metadata['timestamp']`` as an ISO-format string (added Day 24
    for this re-ranker). The semantic module's ``priority`` field
    encodes a 1e-12 timestamp tiebreaker but mixing it with a [0, 1]
    cosine score is lossy on decode — so we read from metadata where
    the timestamp is byte-exact.

    Fallback is used when a caller passes synthetic segments without
    a metadata timestamp (older tests, future strategies that don't
    chain through semantic). The fallback makes the recency feature
    a no-op (1.0) rather than a hard failure.
    """
    ts_str = metadata.get("timestamp")
    if not isinstance(ts_str, str):
        return fallback
    try:
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return fallback


def rerank_segments(
    *,
    query: str,
    messages: Iterable[_MessageLike],
    actions: Iterable[_ActionLike] = (),
    top_k: int = DEFAULT_TOP_K,
    now: datetime | None = None,
) -> list[BriefSegment]:
    """Two-stage retrieval: semantic top-K, then re-rank.

    Steps:

    1. Run :func:`semantic.semantic_segments` to get cosine-ranked
       candidates.
    2. Slice the top-K head; carry the tail unchanged.
    3. For each top-K segment, compute a richer pairwise score and
       overwrite its priority. The base segment's body and metadata
       are preserved — only the ordering changes.
    4. Re-sort the top-K by the new priorities; concatenate with the
       tail to produce the final list.

    The output is shape-compatible with :func:`semantic.semantic_segments`
    so callers (brief assembler, benchmark harness) treat it
    identically. The ``metadata['rerank_score']`` key on each top-K
    segment lets diagnostics distinguish re-ranked rows from carried-
    through tail rows.

    Args:
        query: Borrower's query string. Empty query → degenerate
            fallback (returns the semantic output unchanged; no
            re-ranking signal to compute).
        messages: Iterable of message-like objects (channel, sender,
            content, timestamp, metadata). Same shape semantic accepts.
        actions: Iterable of action-like objects. Same shape semantic
            accepts.
        top_k: How many top semantic candidates to re-rank. Default
            30 (see :data:`DEFAULT_TOP_K`). Capped at the candidate
            count so requesting K=50 on a 10-segment history is a
            no-op rather than an error.
        now: Reference timestamp for the recency feature. Defaults to
            :func:`datetime.now`. Tests pass an explicit ``now`` for
            determinism.

    Returns:
        List of :class:`BriefSegment` in re-rank order (top-K
        re-ranked head, semantic-order tail). Empty list for empty
        inputs.
    """
    semantic_out = semantic_segments(
        query=query, messages=messages, actions=actions
    )
    if not semantic_out or not query.strip():
        return semantic_out

    effective_k = min(top_k, len(semantic_out))
    head = semantic_out[:effective_k]
    tail = semantic_out[effective_k:]

    query_tokens_list = _tokens(query)
    query_token_set = set(query_tokens_list)
    query_bigrams = _bigrams(query_tokens_list)
    ref_now = now or datetime.now()

    rescored: list[BriefSegment] = []
    for seg in head:
        # The cosine signal lives in `metadata['similarity']` — the
        # priority field carries cosine + a tiny timestamp tiebreaker
        # (see semantic.py), but `metadata['similarity']` is the clean
        # signal we want to carry forward.
        cosine_carry = float(seg.metadata.get("similarity", 0.0))
        seg_ts = _seg_timestamp_from_metadata(seg.metadata, fallback=ref_now)
        new_score = _pairwise_score(
            cosine_carry=cosine_carry,
            query_tokens_list=query_tokens_list,
            query_token_set=query_token_set,
            query_bigrams=query_bigrams,
            seg_text=seg.body,
            seg_timestamp=seg_ts,
            now=ref_now,
        )
        # Build a new segment with updated priority + diagnostic metadata.
        # BriefSegment is frozen (Pydantic) — `model_copy` is the
        # documented way to rebuild with field overrides.
        new_metadata = dict(seg.metadata)
        new_metadata["rerank_score"] = round(new_score, 6)
        new_metadata["rerank_stage"] = "head"
        rescored.append(
            seg.model_copy(update={"priority": new_score, "metadata": new_metadata})
        )

    rescored.sort(key=lambda s: s.priority, reverse=True)

    # Annotate tail segments with their stage too so downstream
    # diagnostics can split head vs tail without parsing priorities.
    annotated_tail: list[BriefSegment] = []
    for seg in tail:
        tail_metadata = dict(seg.metadata)
        tail_metadata["rerank_stage"] = "tail"
        annotated_tail.append(seg.model_copy(update={"metadata": tail_metadata}))

    return rescored + annotated_tail


__all__ = [
    "DEFAULT_TOP_K",
    "rerank_segments",
]
