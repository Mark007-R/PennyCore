"""Brief assembler — Day 7 Phase 2.

Given a `list[BriefSegment]` (already priority-sorted descending by the
caller) and a `token_budget`, return a string that fits within the
budget. The assembler is strategy-agnostic — it doesn't know whether
the segments came from recency, semantic, summarized, or hybrid
retrieval. That separation is what lets Phase 3's comparison study swap
strategies without touching the packer.

Token-budget invariant (matches `Brief.token_count <= token_budget` in
the contract): the returned string's `len(text) // 4` is always
`<= token_budget`. The estimate matches the takehome evaluator's
`_estimate_tokens` so that a brief packed under our budget is also
under the evaluator's budget — see the `estimate_tokens` docstring in
`context_engine.retrieval.recency` for why we don't use `tiktoken`
here.

Greedy newest-first is the right default for a recency strategy:
"oldest message I'm willing to drop" comes for free from the priority
sort. Phase 3 will introduce stratified packing (reserve N% for
summary, N% for recent, N% for actions) — but greedy beats stratified
on the simplest scenarios, so we ship greedy as the Day 7 baseline and
add stratification when a benchmark proves the win.
"""

from __future__ import annotations

from typing import Iterable

from contracts import BriefSegment

from context_engine.retrieval.recency import estimate_tokens

# Newline overhead per segment, in tokens. One newline (`"\n"`) is
# `len("\n") // 4 == 0` under the evaluator's estimator, but real
# tokenizers count it as 1, so we charge 1 to stay safe across
# tokenizers AND avoid edge-case overruns when many short segments
# concatenate. The end-to-end check at the bottom is the real guard;
# this is a soft margin during packing.
_NEWLINE_TOKEN_COST = 1


def pack_segments(
    segments: Iterable[BriefSegment],
    *,
    token_budget: int,
    header: str | None = None,
) -> str:
    """Greedy-pack pre-sorted segments into a budgeted string.

    Iteration order: the caller's order is preserved. Recency strategy
    (`context_engine.retrieval.recency.recency_segments`) sorts newest-
    first by priority before returning, so the assembler ends up
    packing the most-recent thing first, which is the desired behavior
    under tight budgets.

    Skip-don't-stop semantics: if a segment is too large to fit, we
    `continue` to the next rather than `break`. That means a long old
    message can be skipped while a short older one still fits — useful
    in pathological cases (one giant message followed by a short
    one). For the recency strategy this rarely matters; for hybrid
    Phase 3 strategies that interleave priorities, skip-don't-stop is
    the only correct semantic.

    Args:
        segments: Pre-sorted `BriefSegment` iterable. Caller owns the
            sort order — this function trusts it.
        token_budget: Hard upper bound on the returned string's tokens
            under the `len(text) // 4` estimator. Must be `> 0`.
        header: Optional header line that's *always* included if it
            fits (e.g. `"Borrower: cust_abc123"`). Header is reserved
            against the budget BEFORE any segments are considered, so
            it's effectively prioritized above all segments. Empty
            string is treated as "no header".

    Returns:
        The assembled brief as a newline-joined string. Empty string
        when no header is supplied AND no segment fits the budget.

    Raises:
        ValueError: if `token_budget <= 0`.
    """
    if token_budget <= 0:
        raise ValueError(f"token_budget must be positive, got {token_budget}")

    parts: list[str] = []
    used_tokens = 0

    if header:
        header_cost = estimate_tokens(header) + _NEWLINE_TOKEN_COST
        if header_cost <= token_budget:
            parts.append(header)
            used_tokens += header_cost
        # If even the header doesn't fit, drop it silently — the
        # contract is "fit the budget", not "fit the header at all
        # costs". A 0-budget brief for a 1-token-budget call is
        # honest.

    for seg in segments:
        cost = seg.token_estimate + _NEWLINE_TOKEN_COST
        if used_tokens + cost > token_budget:
            continue  # skip-don't-stop (see docstring)
        parts.append(seg.body)
        used_tokens += cost

    brief = "\n".join(parts)

    # Defensive end-to-end check: if our segment-level accounting
    # somehow undercounted (shouldn't happen with the same estimator
    # everywhere), trim from the end until we're under budget. This
    # is the invariant that makes `Brief.token_count <=
    # Brief.token_budget` provable from the assembler's output alone.
    while estimate_tokens(brief) > token_budget and parts:
        parts.pop()
        brief = "\n".join(parts)

    return brief
