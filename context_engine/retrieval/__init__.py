"""Retrieval strategies — emit `BriefSegment` lists for the brief assembler.

Day 7 ships `recency` (the naive baseline). Phase 3 (Days 13-15) adds
`semantic`, `summarized`, and `hybrid` modules that satisfy the same
shape: `(messages, actions, ...) -> list[BriefSegment]`. The assembler
in `context_engine.brief_assembly` is strategy-agnostic — it packs
whichever segment list it's handed.
"""

from context_engine.retrieval.recency import recency_segments

__all__ = ["recency_segments"]
