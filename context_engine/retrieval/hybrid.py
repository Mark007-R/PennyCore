"""Hybrid retrieval — Day 15 Phase 3 champion strategy.

Combines the three Day-7 through Day-14 strategies along the *time
axis* rather than as alternatives:

* **Warm window (≤ ``warm_cutoff_hours`` old):** every message emitted
  verbatim, newest-first. Same shape as
  :func:`context_engine.retrieval.recency.recency_segments` for this
  slice.
* **Mid range (between warm and cold cutoffs):** every message scored
  by cosine similarity against the borrower's query and emitted as
  ``SEMANTIC_MATCH`` segments. The relevance signal does work here
  because mid-range messages are old enough that "newest wins" is
  a poor proxy and young enough that summarization would discard
  useful detail.
* **Cold tail (> ``cold_cutoff_hours`` old):** concatenated into one
  block, passed through :meth:`LLMClient.summarize`, emitted as a
  single ``SUMMARY`` segment. Same compression mechanism as the
  Day-14 :mod:`~context_engine.retrieval.summarized` strategy, but
  scoped to truly cold content instead of "everything beyond the
  recent_window".
* **Prior actions:** always verbatim, regardless of age. Dedup signal
  beats chat (same rule as Day-7 recency and Day-14 summarized).

The "now" reference for the cutoffs is the **timestamp of the newest
message in the history**, not the wall clock. This anchors the
windowing to the conversation itself — a benchmark pair recorded six
months ago still gets the same hybrid split as a live one. It also
keeps the strategy deterministic for the benchmark (no clock
dependence inside scoring).

## Why time-based windows instead of a message-count split

The Day-14 :func:`~context_engine.retrieval.summarized.summarized_segments`
uses ``recent_window = 5`` messages as the warm/cold boundary. That
works for conversation-length variance but loses the *temporal*
signal that customer-service agents actually reason about: "what did
they say yesterday" beats "what did they say five turns ago" when the
five turns happen to be a single back-and-forth in one minute.
Time-based windows recover the temporal reasoning and make the
hybrid strategy's "recency for the last day, semantic for the past
week, summary for everything older" framing literal.

## Cutoff defaults

* ``warm_cutoff_hours = 24`` — one day. Matches how customer-service
  teams talk about "yesterday's conversation". On the Phase-3
  benchmark, this captures all of ``short`` (4h spans), about half
  of ``medium`` (48h), the newest ~1/6 of ``long`` (144h), and the
  newest ~5% of ``very_long`` (481h).
* ``cold_cutoff_hours = 72`` — three days. The intent is "stuff old
  enough that the exact wording stopped mattering; the *gist* is
  what the LLM needs". For ``medium`` histories this means no cold
  tail; for ``long`` the first 3 days summarize; for ``very_long``
  the first 17 days summarize.

Both are parameters, not constants, so the Phase-5 re-rank
experiments can sweep them. Sweeping them is the cheap way to find
the quality/cost frontier without re-implementing the strategy.

## Priority encoding

Four priority bands so the brief assembler packs them in a sensible
order under tight budgets:

* Summary: ``1e18`` — always first (the cold tail is the most
  compressed per token; should never get dropped before warm content).
* Actions: ``1e12 + timestamp`` — high band; "I already did X"
  beats verbose chat (same as Day-7 recency's ``+1.0`` rationale,
  encoded as a band here so it dominates *every* age class).
* Warm messages: ``1e6 + timestamp`` — mid band; newest-first
  within the warm window, but the whole window dominates the
  mid-range semantic matches.
* Mid-range semantic matches: ``score + timestamp * 1e-12`` —
  cosine score in [0, 1] dominates within the band; timestamp
  breaks ties so zero-similarity ties degrade to recency.

Sized so that **any** epoch-second timestamp + band offset stays
strictly within its band: epoch seconds top out near 1.78e9 in
2026, so 1e6 / 1e12 / 1e18 give 530× / 560,000× / 5.6e8× headroom
respectively. Encoded as constants so future readers don't shrink
them "because the numbers look big".

## Mock-mode behavior

The cold-tail summary uses the same :func:`get_client` dispatch path
as :mod:`~context_engine.retrieval.summarized`. Under mock mode the
summary is the deterministic ``text[:200] + "...[mock-summary]"``
marker — flagged in the comparison report so the Day-15 LLM-as-judge
re-run with a real LLM can re-fit the number. Mid-range semantic
scoring is dependency-free (hashed BoW, identical to
:mod:`~context_engine.retrieval.semantic`) so the mid range is
provider-independent.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable, Protocol

from contracts import BriefSegment, SegmentSource

from context_engine.llm import LLMClient, get_client
from context_engine.retrieval.recency import (
    estimate_tokens,
    format_action_segment,
    format_message_segment,
)
from context_engine.retrieval.semantic import _embed, _cosine
from context_engine.retrieval.summarized import _flatten_for_summary


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


DEFAULT_WARM_CUTOFF_HOURS = 24
DEFAULT_COLD_CUTOFF_HOURS = 72

# Priority bands — see module docstring §"Priority encoding".
_PRIORITY_SUMMARY = 1e18
_PRIORITY_ACTION_BAND = 1e12
_PRIORITY_WARM_BAND = 1e6
# Mid-range semantic matches sit at score + tiny tiebreaker (no band offset).


def _partition_messages(
    messages: list[_MessageLike],
    *,
    warm_cutoff: datetime,
    cold_cutoff: datetime,
) -> tuple[list[_MessageLike], list[_MessageLike], list[_MessageLike]]:
    """Split messages into (warm, mid, cold) by timestamp.

    Warm: ``timestamp >= warm_cutoff``.
    Mid: ``cold_cutoff <= timestamp < warm_cutoff``.
    Cold: ``timestamp < cold_cutoff``.

    Returns three lists in newest-first order each. Returns three empty
    lists for empty input — the caller short-circuits this case before
    sorting, but the function is defensive in case it's reused.
    """
    warm: list[_MessageLike] = []
    mid: list[_MessageLike] = []
    cold: list[_MessageLike] = []
    for msg in messages:
        ts = msg.timestamp
        if ts >= warm_cutoff:
            warm.append(msg)
        elif ts >= cold_cutoff:
            mid.append(msg)
        else:
            cold.append(msg)
    warm.sort(key=lambda m: m.timestamp, reverse=True)
    mid.sort(key=lambda m: m.timestamp, reverse=True)
    cold.sort(key=lambda m: m.timestamp, reverse=True)
    return warm, mid, cold


def _build_summary_segment(
    cold_messages: list[_MessageLike],
    *,
    client: LLMClient,
    max_tokens: int,
) -> BriefSegment | None:
    """Compress the cold tail into one SUMMARY segment, or None if empty.

    Mirrors :func:`context_engine.retrieval.summarized._summary_for_window`
    but lives here so the hybrid module can carry hybrid-specific
    metadata (``n_messages_summarized`` + ``cold_cutoff_hours``).
    """
    if not cold_messages:
        return None
    raw_block = _flatten_for_summary(cold_messages)
    summary_text = client.summarize(raw_block, max_tokens=max_tokens)
    if not summary_text:
        return None
    body = f"[summary] {summary_text}"
    return BriefSegment(
        source=SegmentSource.SUMMARY,
        body=body,
        token_estimate=estimate_tokens(body),
        priority=_PRIORITY_SUMMARY,
        metadata={
            "n_messages_summarized": len(cold_messages),
            "llm_provider": client.name,
            "llm_model": client.model,
            "tier": "cold",
        },
    )


def hybrid_segments(
    *,
    query: str,
    messages: Iterable[_MessageLike],
    actions: Iterable[_ActionLike] = (),
    warm_cutoff_hours: int = DEFAULT_WARM_CUTOFF_HOURS,
    cold_cutoff_hours: int = DEFAULT_COLD_CUTOFF_HOURS,
    client: LLMClient | None = None,
    summary_max_tokens: int = 256,
) -> list[BriefSegment]:
    """Build a [SUMMARY?, ...PRIOR_ACTION, ...RECENT_MESSAGE, ...SEMANTIC_MATCH] list.

    Returns segments pre-sorted by descending priority so the assembler
    packs them greedily. Empty inputs return ``[]``.

    Args:
        query: The borrower's question. Used to score mid-range
            messages. Empty query → mid-range messages all score 0
            and degrade to recency-newest-first inside the band.
        messages: Iterable of messages (any order). Internally
            partitioned into warm / mid / cold by timestamp relative
            to the newest message.
        actions: Iterable of prior actions. Always emitted verbatim
            with the high-priority action band.
        warm_cutoff_hours: How old (in hours, relative to the newest
            message) a message can be and still count as "warm".
            Default 24. See module docstring §"Cutoff defaults".
        cold_cutoff_hours: How old a message must be (relative to
            the newest message) to be considered cold-tail. Messages
            between ``warm_cutoff_hours`` and this value are
            mid-range. Default 72.
        client: LLM client for cold-tail summarization. Injected for
            tests; production callers should pass ``None`` and let
            the function resolve via :func:`get_client`.
        summary_max_tokens: Output-budget hint for the summarizer.
            Mock mode ignores it; real providers honor it.

    Raises:
        ValueError: if ``cold_cutoff_hours <= warm_cutoff_hours`` —
            cold must be strictly older than warm, otherwise the mid
            range is undefined.
    """
    if cold_cutoff_hours <= warm_cutoff_hours:
        raise ValueError(
            f"cold_cutoff_hours ({cold_cutoff_hours}) must be > "
            f"warm_cutoff_hours ({warm_cutoff_hours})"
        )

    msg_list = list(messages)
    act_list = list(actions)
    if not msg_list and not act_list:
        return []

    # Reference time = newest message timestamp (or newest action if no
    # messages). Anchoring on the conversation, not the wall clock, keeps
    # the windowing deterministic across benchmark re-runs.
    candidates = [m.timestamp for m in msg_list] + [a.timestamp for a in act_list]
    reference = max(candidates)
    warm_cutoff = reference - timedelta(hours=warm_cutoff_hours)
    cold_cutoff = reference - timedelta(hours=cold_cutoff_hours)

    warm, mid, cold = _partition_messages(
        msg_list, warm_cutoff=warm_cutoff, cold_cutoff=cold_cutoff
    )

    if client is None:
        client = get_client()

    segments: list[BriefSegment] = []

    # 1. Cold-tail summary (highest band).
    summary_seg = _build_summary_segment(
        cold, client=client, max_tokens=summary_max_tokens
    )
    if summary_seg is not None:
        segments.append(summary_seg)

    # 2. Prior actions (action band).
    for act in act_list:
        body = format_action_segment(act)
        segments.append(
            BriefSegment(
                source=SegmentSource.PRIOR_ACTION,
                body=body,
                token_estimate=estimate_tokens(body),
                priority=_PRIORITY_ACTION_BAND + act.timestamp.timestamp(),
                metadata={"action_type": act.action_type, "tier": "action"},
            )
        )

    # 3. Warm messages (warm band, newest-first inside the band).
    for msg in warm:
        body = format_message_segment(msg)
        segments.append(
            BriefSegment(
                source=SegmentSource.RECENT_MESSAGE,
                body=body,
                token_estimate=estimate_tokens(body),
                priority=_PRIORITY_WARM_BAND + msg.timestamp.timestamp(),
                metadata={"channel": msg.channel, "sender": msg.sender, "tier": "warm"},
            )
        )

    # 4. Mid-range semantic matches (score + tiny timestamp tiebreaker).
    if mid:
        query_vec = _embed(query)
        for msg in mid:
            body = format_message_segment(msg)
            score = _cosine(query_vec, _embed(msg.content))
            segments.append(
                BriefSegment(
                    source=SegmentSource.SEMANTIC_MATCH,
                    body=body,
                    token_estimate=estimate_tokens(body),
                    priority=score + msg.timestamp.timestamp() * 1e-12,
                    metadata={
                        "channel": msg.channel,
                        "sender": msg.sender,
                        "similarity": round(score, 6),
                        "tier": "mid",
                    },
                )
            )

    segments.sort(key=lambda s: s.priority, reverse=True)
    return segments


__all__ = [
    "DEFAULT_COLD_CUTOFF_HOURS",
    "DEFAULT_WARM_CUTOFF_HOURS",
    "hybrid_segments",
]
