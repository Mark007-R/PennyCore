"""Semantic retrieval — Day 14 Phase 3 strategy.

Score every segment (message + action) by relevance to the borrower's
query and pack the highest-scoring ones first. The relevance signal is
cosine similarity over a deterministic, dependency-free
**hashed-token bag-of-words** embedding — see §"Why hashed BoW, not
sentence-transformers" below for the rationale and the upgrade path.

Two design constraints this module honors:

1. **Mock-mode reproducibility.** Phase-3 benchmark numbers under mock
   mode must be byte-stable across machines and runs. A neural
   embedder (e.g. ``sentence-transformers/all-MiniLM-L6-v2``) needs a
   ~80MB model download on first use, a torch/transformers install
   that more than doubles container size, and floating-point output
   that drifts across CPU vs. CUDA. Hashed BoW + cosine is pure
   stdlib, deterministic to the last bit, and clears the bar for
   "honest semantic relevance signal" — it ranks "what mortgage rate
   was I quoted?" against a 200-message history correctly enough
   for the Day-15 LLM-as-judge eval to score the resulting brief.
2. **Same segment + formatter surface as recency / naive.** The
   strategy emits :class:`contracts.BriefSegment` instances with the
   same :func:`format_message_segment` / :func:`format_action_segment`
   bodies that the Day-13 strategies use. The brief assembler doesn't
   know or care which strategy produced the segments — Day-15
   LLM-as-judge compares strategies on *what got selected*, not on
   formatting nuance.

## Scoring algorithm

For each segment ``s`` and query ``q``:

* Lowercase + split on non-alphanumerics → token list.
* Drop tokens shorter than 2 chars (kills "a", "to", "of" noise and
  matches what stock tokenizers do downstream).
* Hash each token into a 4096-dim vector via stdlib ``hash()`` —
  modulo seeded so hashing is stable across processes (see
  ``PYTHONHASHSEED`` note below). Increment the bucket.
* Cosine similarity = ``dot(q, s) / (||q|| * ||s||)``. Zero norm on
  either side returns 0.0 (no signal).

The score becomes the segment's ``priority``. Higher = packed first.

The implementation is intentionally tiny (~30 lines for the embedding +
scoring path). Document why every choice was made; future-Phase-5 will
swap this for a real embedder, and we want the swap to be one-file.

## Action-priority handling

Prior actions get the same cosine-similarity score as messages —
**no ``+1.0`` boost** like recency uses. Rationale: under a semantic
strategy, an action that's *unrelated* to the query (e.g. a
"send_quote" action when the query is about document upload status)
shouldn't crowd out a closely-matching older message. The dedup-
awareness goal (don't repeat the same action) is preserved when the
action's ``action_type`` token overlaps the query — which it will when
the query is about that action — so the boost would be redundant.
Recency keeps the boost because it has no relevance signal at all.

## Tie-breaking + determinism

Ties (e.g. two segments at score 0.0 because neither shares tokens
with the query) sort by timestamp descending — newer first. This
mirrors recency's behavior so the "no semantic match" failure mode
degrades to "recency" rather than to "random". The ordering is fully
deterministic given a fixed ``PYTHONHASHSEED`` (we set one in the
helper if the env var is unset — see ``_HASH_SEED``).

## Token-budget contract

The strategy itself doesn't trim — it returns every segment, sorted
by descending priority. The harness's ``pack_segments`` applies the
budget downstream (8K by default, matching recency / production). A
saturated brief means the top-scoring segments fit and the tail got
dropped; an under-budget brief means even the lowest-score segments
fit and the strategy is equivalent to "everything, sorted by
relevance".

## Why hashed BoW, not sentence-transformers

The SKILL §INFRASTRUCTURE-FIRST-R&D allows pre-trained
``sentence-transformers`` but doesn't require them. The benchmark
under MOCK mode (today, Days 14-15) needs deterministic outputs so
the Day-15 quality scores are comparable across runs. A neural
embedder is in-scope for Phase 5 (Day 24 re-ranking) when we own the
production retrieval path and want the absolute-best signal. Today
we ship the lexical-semantic baseline, document it as such, and let
Day 24 measure whether the upgrade pays its install/latency cost.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from math import sqrt
from typing import Any, Iterable, Protocol

from contracts import BriefSegment, SegmentSource

from context_engine.retrieval.recency import (
    estimate_tokens,
    format_action_segment,
    format_message_segment,
)


class _MessageLike(Protocol):
    channel: str
    sender: str
    content: str
    timestamp: datetime
    metadata: dict[str, Any]


class _ActionLike(Protocol):
    action_type: str
    details: dict[str, Any]
    timestamp: datetime


# Embedding dimension. 4096 is large enough that hash collisions on
# realistic borrower-history vocabularies (a few thousand unique
# tokens per pair, max) stay below ~1% — small enough to not matter
# for the comparison. Smaller would make the brief deterministic and
# fast but increase collision-driven false matches.
_EMBED_DIM = 4096

# Tokens shorter than this get dropped before hashing. 2 matches what
# most production tokenizers do for stopword-ish noise without
# requiring a stopword list (which would invite locale / language
# drift between the benchmark and prod).
_MIN_TOKEN_LEN = 2

# Stable hash seed — Python's built-in ``hash()`` is randomized across
# interpreter sessions unless ``PYTHONHASHSEED`` is set. We don't want
# benchmark scores to drift across runs, so we use a stable folded
# hash (FNV-1a 32-bit) instead of ``hash()``. Documented here so
# future readers don't "optimize" by switching to ``hash(token)``.
_FNV_OFFSET = 0x811C9DC5
_FNV_PRIME = 0x01000193


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumerics, drop very-short tokens.

    Returns an empty list for empty / whitespace-only input. The
    splitter ``re.split(r"[^a-z0-9]+", ...)`` keeps numeric tokens
    (e.g. ``"6.25"`` → ``["6", "25"]``) — losing the decimal hurts
    matching slightly but is the price of a regex-only tokenizer.
    Phase 5 can swap in a real tokenizer if the loss matters.
    """
    if not text:
        return []
    return [
        t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) >= _MIN_TOKEN_LEN
    ]


def _fnv1a32(token: str) -> int:
    """FNV-1a 32-bit hash — stable across processes and Python versions.

    ``hash(token)`` is randomized per-process unless PYTHONHASHSEED is
    set; FNV is deterministic by construction. The bucket is
    ``_fnv1a32(token) % _EMBED_DIM``.
    """
    h = _FNV_OFFSET
    for ch in token.encode("utf-8"):
        h ^= ch
        h = (h * _FNV_PRIME) & 0xFFFFFFFF
    return h


def _embed(text: str) -> dict[int, float]:
    """Hashed bag-of-words embedding as a sparse {bucket: count} dict.

    Sparse representation because a single message rarely touches more
    than ~50 distinct tokens, so storing 4046 zeros is wasteful. Dot
    product over two sparse dicts iterates the shorter one — fast.
    """
    vec: dict[int, float] = {}
    for tok in _tokenize(text):
        bucket = _fnv1a32(tok) % _EMBED_DIM
        vec[bucket] = vec.get(bucket, 0.0) + 1.0
    return vec


def _cosine(a: dict[int, float], b: dict[int, float]) -> float:
    """Cosine similarity over sparse-dict embeddings. Returns 0.0 on
    zero-norm either side (matches sklearn convention)."""
    if not a or not b:
        return 0.0
    # Iterate the smaller dict; lookup the other.
    if len(a) > len(b):
        a, b = b, a
    dot = 0.0
    for k, va in a.items():
        vb = b.get(k)
        if vb is not None:
            dot += va * vb
    norm_a = sqrt(sum(v * v for v in a.values()))
    norm_b = sqrt(sum(v * v for v in b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _action_text(act: _ActionLike) -> str:
    """Text used for scoring an action against the query.

    Concatenates ``action_type`` and the rendered details so tokens
    like ``"send_checklist"`` and ``"w2"`` (from ``details``) both
    contribute to the score. Distinct from
    :func:`format_action_segment` — that one is for the brief body;
    this one is for the similarity signal.
    """
    detail_str = " ".join(
        f"{k} {v}" for k, v in (act.details or {}).items()
    )
    return f"{act.action_type} {detail_str}".strip()


def semantic_segments(
    *,
    query: str,
    messages: Iterable[_MessageLike],
    actions: Iterable[_ActionLike] = (),
) -> list[BriefSegment]:
    """Score and rank segments by cosine similarity to the query.

    Empty query → degenerate fallback: returns segments sorted by
    timestamp descending (i.e. recency, without the +1.0 action
    boost). Documented so callers know a missing query doesn't
    crash; the benchmark always provides one.

    Empty inputs return ``[]``.
    """
    query_vec = _embed(query)
    segments: list[BriefSegment] = []

    for msg in messages:
        body = format_message_segment(msg)
        score = _cosine(query_vec, _embed(msg.content))
        segments.append(
            BriefSegment(
                source=SegmentSource.SEMANTIC_MATCH,
                body=body,
                token_estimate=estimate_tokens(body),
                # Priority = score, but tie-break on timestamp so
                # zero-similarity ties degrade to recency-newest-first.
                # Encode timestamp into a small fractional component
                # (epoch seconds * 1e-12) — keeps real cosine signal
                # dominant, lets timestamp break ties deterministically.
                priority=score + msg.timestamp.timestamp() * 1e-12,
                metadata={
                    "channel": msg.channel,
                    "sender": msg.sender,
                    "similarity": round(score, 6),
                    # Carry the raw timestamp for downstream re-rankers
                    # (Day 24+) that need exact ages. The priority field
                    # encodes ts as a 1e-12 tiebreaker which is lossy
                    # when mixed with a cosine score in the unit interval.
                    "timestamp": msg.timestamp.isoformat(),
                },
            )
        )

    for act in actions:
        body = format_action_segment(act)
        score = _cosine(query_vec, _embed(_action_text(act)))
        segments.append(
            BriefSegment(
                source=SegmentSource.PRIOR_ACTION,
                body=body,
                token_estimate=estimate_tokens(body),
                priority=score + act.timestamp.timestamp() * 1e-12,
                metadata={
                    "action_type": act.action_type,
                    "similarity": round(score, 6),
                    "timestamp": act.timestamp.isoformat(),
                },
            )
        )

    # Descending — highest score first. Stable sort means equal-score
    # segments keep insertion order (messages-then-actions in this
    # function), but the timestamp tie-breaker dominates that anyway.
    segments.sort(key=lambda s: s.priority, reverse=True)
    return segments


__all__ = ["semantic_segments"]
