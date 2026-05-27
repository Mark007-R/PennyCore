"""Phase-3 retrieval strategies — one module per strategy.

Each module imports :func:`benchmarks.context_engine_bench.register_strategy`
and calls it at import time. Importing this subpackage is therefore the
single side-effect that the harness needs to populate its registry with
every Phase-3 strategy.

Ship order:

* Day 13 — :mod:`benchmarks.strategies.naive` (naive_dump) and
  :mod:`benchmarks.strategies.recency` (re-export Day-7 production
  recency as a first-class strategy row).
* Day 14 — :mod:`benchmarks.strategies.semantic` (cosine-similarity
  ranking on hashed BoW embeddings) and
  :mod:`benchmarks.strategies.summarized` (LLM-summarized cold tail
  + verbatim warm window).
* Day 15 — :mod:`benchmarks.strategies.hybrid` (recency for warm
  window + semantic for mid-range + summary for cold tail — the
  champion against which Day 18 picks the Phase-3 winner).
* Day 24 — :mod:`benchmarks.strategies.reranked` (Phase-5 two-stage
  retrieval: semantic top-K candidates re-ranked with a deterministic
  cross-encoder proxy — bigram + Jaccard + recency on top of the
  first-stage cosine signal).

Recency stays *also* registered directly inside
``benchmarks.context_engine_bench`` (it's the Day-12 baseline that
predates this subpackage). Re-registration is allowed, so the
strategy-module path overwrites the inline one with identical
behavior — keeps a single source of truth at the strategy-module
level for Day 14+.
"""

from __future__ import annotations

# Import each strategy module to trigger its register_strategy(...) call.
# Order doesn't matter (registration is keyed by name) but we list them
# in the order they shipped so the file reads like the project history.
from benchmarks.strategies import naive  # noqa: F401
from benchmarks.strategies import recency  # noqa: F401
from benchmarks.strategies import semantic  # noqa: F401
from benchmarks.strategies import summarized  # noqa: F401
from benchmarks.strategies import hybrid  # noqa: F401
from benchmarks.strategies import reranked  # noqa: F401

__all__ = ["naive", "recency", "semantic", "summarized", "hybrid", "reranked"]
