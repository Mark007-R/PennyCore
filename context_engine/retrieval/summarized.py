"""LLM-summarized retrieval — Day 14 Phase 3 strategy.

The "compress old, keep recent verbatim" pattern that production AI
customer-service stacks use to handle long histories without paying
for the full conversation every turn. Splits a borrower's history at
a ``recent_window`` boundary:

* Newest ``recent_window`` messages → emitted verbatim as
  ``RECENT_MESSAGE`` segments (same formatter as recency / naive).
* Everything older → concatenated and passed through
  :meth:`LLMClient.summarize` to produce a single ``SUMMARY`` segment
  carrying the cold-tail context in compressed form.
* Prior actions → always emitted verbatim as ``PRIOR_ACTION``
  segments, regardless of age. Actions are dedup signal; collapsing
  them into a summary would defeat the "don't re-send the checklist"
  invariant the Phase-2 brief depends on.

The strategy is the head-to-head counterfactual for the Day-15 hybrid
champion: hybrid does "summarize the cold tail + recency for the warm
window + semantic for older mid-range" — summarized is the same idea
*without* the semantic-mid-range step. If hybrid wins, the semantic
step is what's earning its keep; if summarized matches it, the
semantic step is dead weight on this dataset.

## Mock-mode behavior

``get_client()`` returns the :class:`context_engine.llm.MockClient`
when no provider is configured (current default for the Phase-3
benchmark — see SKILL §"LLM PROVIDER MODE"). Mock's
``summarize(text)`` returns ``text[:200] + "...[mock-summary]"`` —
deterministic and short, but token-bound by *input* size, not output
size, in mock mode.

This is FINE for the comparison study because:

1. The Day-13 → Day-15 strategies all run in the same mode (mock).
   Whichever strategy wins under mock is the strategy hybrid's
   Day-15 fight is *against*.
2. The ``...[mock-summary]`` suffix is the marker the report uses
   to flag mock numbers — see :func:`_summary_for_window`'s
   metadata, which the harness surfaces under
   ``segment_source_counts["summary"]``.
3. The Day-15 LLM-as-judge step re-runs (or could re-run) every
   strategy with a real LLM; the harness saves the assembled brief
   text so re-judging doesn't require re-retrieval.

When a real LLM provider IS configured, the dispatch layer routes
``client.summarize`` to the real model. The strategy is identical
either way — the LLM call site is the only mode-dependent line.

## recent_window choice

Default ``recent_window = 5`` messages. Rationale: most banking-
customer conversations resolve in 3-5 turns; the immediate context
the agent needs is "what's the last thing they said and what did
they ask?" Older context (rate quotes, document requests from days
ago) goes into the summary. ``recent_window`` is a parameter, not a
constant, so the Day-15 hybrid module can re-use this code with
``recent_window = 3`` (smaller warm window because semantic fills
the mid-range).

## Priority encoding

Three priority bands so the brief assembler packs them in a sensible
order under tight budgets:

* Summary: 1e18 (always first — the cold tail is the most-compressed
  per-token and should never get dropped before warm content).
* Actions: 1e12 + timestamp (high band; matches the recency strategy's
  intent that "I already did X" beats verbose chat).
* Recent messages: timestamp only (newest-first within the warm
  window; epoch seconds in 2026 are ~1.78e9 so they sit well below
  the action band).

The band constants are chosen so that **any** epoch-second timestamp
+ band offset fits strictly inside its own band: epoch seconds top
out near 1e10 for centuries to come, so 1e12 / 1e18 give 100× /
100M× headroom. Documented here so future readers don't shrink them
"because the numbers look big".
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Protocol

from contracts import BriefSegment, SegmentSource

from context_engine.llm import LLMClient, get_client
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


# Default warm-window size. Documented in module docstring §"recent_window choice".
DEFAULT_RECENT_WINDOW = 5

# Priority bands — see module docstring §"Priority encoding".
# Sized so that band offset + any realistic epoch-second timestamp
# (≈1.78e9 in 2026) stays strictly within its own band.
_PRIORITY_SUMMARY = 1e18
_PRIORITY_ACTION_BAND = 1e12


def _flatten_for_summary(messages: list[_MessageLike]) -> str:
    """Render the cold-tail messages as one block of text for the LLM.

    Each line: ``[<channel>] <YYYY-MM-DD HH:MM> <sender>: <content>``
    — same shape as :func:`format_message_segment` but inlined here so
    the function works on a plain list (the summarizer doesn't care
    about per-line metadata).
    """
    lines = []
    for msg in messages:
        ts = msg.timestamp.strftime("%Y-%m-%d %H:%M")
        lines.append(f"[{msg.channel}] {ts} {msg.sender}: {msg.content}")
    return "\n".join(lines)


def _summary_for_window(
    cold_messages: list[_MessageLike],
    *,
    client: LLMClient,
    max_tokens: int,
) -> BriefSegment | None:
    """Build the SUMMARY segment over the cold tail, or None if empty.

    Returns ``None`` (not an empty segment) when there's nothing to
    summarize so the caller can omit the segment entirely — emitting
    an empty SUMMARY segment would violate
    :class:`BriefSegment`'s ``min_length=1`` validator.
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
        },
    )


def summarized_segments(
    *,
    messages: Iterable[_MessageLike],
    actions: Iterable[_ActionLike] = (),
    recent_window: int = DEFAULT_RECENT_WINDOW,
    client: LLMClient | None = None,
    summary_max_tokens: int = 256,
) -> list[BriefSegment]:
    """Build a [SUMMARY?, ...PRIOR_ACTION, ...RECENT_MESSAGE] segment list.

    Returns segments pre-sorted by descending priority so the assembler
    packs them greedily. Empty inputs return ``[]``.

    Args:
        messages: Iterable of messages (any order). Internally split
            into warm (newest ``recent_window``) and cold (the rest).
        actions: Iterable of prior actions. Always emitted verbatim;
            never folded into the summary.
        recent_window: How many newest messages to keep verbatim.
            Default 5 — see module docstring §"recent_window choice".
        client: LLM client. Injected for tests; production callers
            should pass ``None`` and let the function call
            :func:`context_engine.llm.get_client`. Injectable so tests
            can substitute a deterministic stub without env-var
            gymnastics.
        summary_max_tokens: Output-budget hint for the summarizer.
            Mock mode ignores it (mock always returns 200 chars +
            suffix); real providers honor it.
    """
    msg_list = list(messages)
    act_list = list(actions)
    if not msg_list and not act_list:
        return []

    # Newest-first ordering so we can take the head as the warm window.
    msg_list.sort(key=lambda m: m.timestamp, reverse=True)
    warm = msg_list[:recent_window]
    cold = msg_list[recent_window:]

    if client is None:
        client = get_client()

    segments: list[BriefSegment] = []

    # SUMMARY segment first (highest priority band) so a 1-token-only
    # brief still has the cold-tail context.
    summary_seg = _summary_for_window(
        cold, client=client, max_tokens=summary_max_tokens
    )
    if summary_seg is not None:
        segments.append(summary_seg)

    # Prior actions next — high priority band so dedup signal beats
    # chat noise under tight budget.
    for act in act_list:
        body = format_action_segment(act)
        segments.append(
            BriefSegment(
                source=SegmentSource.PRIOR_ACTION,
                body=body,
                token_estimate=estimate_tokens(body),
                priority=_PRIORITY_ACTION_BAND + act.timestamp.timestamp(),
                metadata={"action_type": act.action_type},
            )
        )

    # Warm-window messages — priority is timestamp only so within the
    # window they read newest-first.
    for msg in warm:
        body = format_message_segment(msg)
        segments.append(
            BriefSegment(
                source=SegmentSource.RECENT_MESSAGE,
                body=body,
                token_estimate=estimate_tokens(body),
                priority=msg.timestamp.timestamp(),
                metadata={"channel": msg.channel, "sender": msg.sender},
            )
        )

    segments.sort(key=lambda s: s.priority, reverse=True)
    return segments


__all__ = ["DEFAULT_RECENT_WINDOW", "summarized_segments"]
