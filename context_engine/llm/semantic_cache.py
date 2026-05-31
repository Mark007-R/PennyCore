"""Semantic response cache (Day 25, Phase 5).

Caches LLM responses keyed by the **embedding similarity** of the
prompt (``query + assembled brief``) rather than by exact-string
equality. The headline Phase-5 question: an LLM call costs money and
latency; a cache hit costs neither. An *exact-string* cache only hits
when two prompts are byte-identical — which, in a customer-service
system, is rare (every borrower's brief differs). A *semantic* cache
hits when two prompts are close enough in embedding space, so
paraphrased or lightly-varying queries against a similar brief can
reuse a prior answer. Day-25 measures how much cost that buys, and at
what risk.

## What gets cached, and the cache key

The cache key is an embedding of arbitrary "key text" — in the
production flow that's ``query + "\\n" + brief``. We reuse the *exact
same* deterministic hashed-BoW embedder the retrieval layer uses
(:func:`context_engine.retrieval.semantic._embed`) so:

1. The cache lives in the same vector space as retrieval — a future
   optimization can embed the brief once and use the vector for both
   ranking and cache-keying.
2. Benchmark numbers are byte-stable across runs (FNV-1a hashing, no
   neural model, no FP drift) — the same reproducibility constraint
   that drove the retrieval embedder choice (see that module's
   docstring §"Why hashed BoW, not sentence-transformers"). The
   production swap-in is the same one-line change: replace ``_embed``
   with a sentence-transformer encoder and the cache logic is
   unchanged.

The hashed-BoW signal catches **lexical** paraphrases (shared content
words, reordered, padded with filler) — which is the bulk of real
"ask the same thing again" traffic — but NOT deep semantic
paraphrases that share meaning without sharing words. That limitation
is honest and documented; a neural embedder closes the gap and drops
into the same interface.

## Multi-tenant isolation (SKILL HARD RULE 15)

The cache is **tenant-scoped**. Bank A's cached answer is NEVER
returned for Bank B, even when the two send byte-identical prompts.
Entries live in per-tenant namespaces and lookups never cross them.
This is a data-isolation guarantee, verified by tests and by the
benchmark's cross-tenant probe. Per-tenant namespaces also give each
tenant its own eviction budget — a noisy tenant can't evict a quiet
tenant's hot entries.

## Two-layer lookup: exact fast path + vector fallback

Lookup checks an exact-string index first, then falls back to a cosine
nearest-neighbor scan. This mirrors production semantic caches
(GPTCache et al.): an exact-prompt repeat is an O(1) dict hit that
ALWAYS returns the cached answer (it's literally the same prompt, so
the near-hit threshold doesn't apply); only genuinely-different
prompts pay the linear vector scan. The fast path also sidesteps a
float-precision trap — cosine of a vector with itself computes to
``0.999…998`` under ``dot / (‖a‖·‖b‖)``, so a hard ``threshold=1.0``
gate would otherwise *miss* an identical prompt. Re-storing an
existing key refreshes that entry in place rather than inserting a
duplicate.

## Eviction

Per-tenant LRU with a configurable ``max_entries`` cap. On store, if a
tenant's namespace exceeds the cap, the least-recently-used entry is
evicted (and dropped from the exact index). A lookup hit touches
(promotes) the matched entry.

## Cost accounting

Each hit avoids one LLM call. The avoided cost = input tokens (the
prompt) priced at :data:`INPUT_USD_PER_MTOK` + output tokens (the
cached response, which would have been regenerated) priced at
:data:`OUTPUT_USD_PER_MTOK`. Prices default to Claude Sonnet 4.x list
(input $3 / MTok, output $15 / MTok) and are constructor-overridable
so the benchmark can re-price under any model. Token counts use the
same ``len(text) // 4`` estimator as the brief assembler
(:func:`context_engine.retrieval.recency.estimate_tokens`) so cost
numbers reconcile with the Phase-3 token columns.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Protocol

from context_engine.retrieval.recency import estimate_tokens
from context_engine.retrieval.semantic import _cosine, _embed

# Claude Sonnet 4.x list pricing (USD per million tokens). A cache hit
# saves BOTH the input (prompt) and output (regenerated response)
# tokens, so we price both — the Phase-3 cost columns only counted
# input because no response was generated there. Override via the
# SemanticCache constructor to re-price for another model.
INPUT_USD_PER_MTOK = 3.0
OUTPUT_USD_PER_MTOK = 15.0

# Default near-hit threshold. 0.95 is deliberately conservative: it
# behaves almost like an exact cache (only very-close prompts hit) so
# the default never serves a surprising wrong answer. The Day-25
# benchmark sweeps lower thresholds to chart the savings-vs-false-hit
# trade-off; production picks a point on that curve per its risk
# tolerance.
DEFAULT_THRESHOLD = 0.95

# Default per-tenant entry cap. Sized so a busy borrower-support tenant
# (hundreds of distinct briefs in flight) fits without eviction churn,
# small enough that the linear NN scan per lookup stays sub-millisecond.
DEFAULT_MAX_ENTRIES = 1024

# Similarity at/above which a hit is classified "exact" rather than
# "near". Identical key text → identical embedding → cosine 1.0; the
# epsilon absorbs float round-off.
_EXACT_SIM = 1.0 - 1e-9


class LLMClientLike(Protocol):
    """Minimal surface a wrapped client must expose (mirrors
    :class:`context_engine.llm.LLMClient`)."""

    name: str
    model: str

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str: ...


@dataclass
class CacheEntry:
    """One cached (prompt → response) pair within a tenant namespace."""

    key_text: str
    embedding: dict[int, float]
    response: str
    input_tokens: int
    output_tokens: int
    hits: int = 0


@dataclass
class CacheResult:
    """Outcome of a single :meth:`SemanticCache.lookup`.

    ``hit`` is the gate decision. ``similarity`` is the best-matching
    entry's cosine score regardless of hit/miss — a miss with a high
    similarity (just under threshold) is diagnostically interesting, so
    we always carry it. ``kind`` is ``"exact"`` / ``"near"`` / ``"miss"``.
    """

    hit: bool
    response: str | None
    similarity: float
    kind: str


@dataclass
class CacheStats:
    """Append-only counters for one cache's lifetime.

    All cost figures are USD; token figures are under the ``len//4``
    estimator. ``cost_saved_usd`` is the sum of avoided input+output
    cost across every hit — the headline "cost reduction" number.
    """

    lookups: int = 0
    hits: int = 0
    misses: int = 0
    exact_hits: int = 0
    near_hits: int = 0
    stores: int = 0
    evictions: int = 0
    input_tokens_saved: int = 0
    output_tokens_saved: int = 0
    cost_saved_usd: float = 0.0

    @property
    def hit_rate(self) -> float:
        """Fraction of lookups that hit. 0.0 when no lookups yet."""
        return self.hits / self.lookups if self.lookups else 0.0

    def snapshot(self) -> dict[str, float | int]:
        """JSON-serializable view for results artifacts / reports."""
        return {
            "lookups": self.lookups,
            "hits": self.hits,
            "misses": self.misses,
            "exact_hits": self.exact_hits,
            "near_hits": self.near_hits,
            "stores": self.stores,
            "evictions": self.evictions,
            "hit_rate": round(self.hit_rate, 6),
            "input_tokens_saved": self.input_tokens_saved,
            "output_tokens_saved": self.output_tokens_saved,
            "cost_saved_usd": round(self.cost_saved_usd, 6),
        }


class SemanticCache:
    """Tenant-scoped, embedding-keyed LLM response cache.

    Not thread-safe by itself — the production wiring holds one cache
    per process and serializes access through the request path, or
    wraps it in a lock at the call site. The benchmark is
    single-threaded. Documented here so a future concurrent caller
    adds the lock deliberately rather than discovering the gap.
    """

    def __init__(
        self,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        input_usd_per_mtok: float = INPUT_USD_PER_MTOK,
        output_usd_per_mtok: float = OUTPUT_USD_PER_MTOK,
    ) -> None:
        if not 0.0 < threshold <= 1.0:
            raise ValueError(
                f"threshold must be in (0, 1], got {threshold}"
            )
        if max_entries <= 0:
            raise ValueError(f"max_entries must be positive, got {max_entries}")
        self.threshold = threshold
        self.max_entries = max_entries
        self.input_usd_per_mtok = input_usd_per_mtok
        self.output_usd_per_mtok = output_usd_per_mtok
        # tenant_id -> LRU-ordered {entry_id: CacheEntry}. OrderedDict
        # gives O(1) move_to_end (LRU touch) and popitem(last=False)
        # (evict oldest). entry_id is a per-cache monotonic counter.
        self._store: dict[str, "OrderedDict[int, CacheEntry]"] = {}
        # tenant_id -> {key_text: entry_id} exact-match fast-path index.
        # Kept in sync with _store on store / refresh / evict / clear.
        self._exact: dict[str, dict[str, int]] = {}
        self._next_id = 0
        self.stats = CacheStats()

    def lookup(self, *, tenant_id: str, key_text: str) -> CacheResult:
        """Two-layer lookup within ``tenant_id``'s namespace.

        Exact-string fast path first (O(1), bypasses the threshold —
        an identical prompt always reuses its answer). On an
        exact-index miss, falls back to a cosine nearest-neighbor scan
        gated by ``threshold``. Either kind of hit promotes the matched
        entry (LRU touch) and accrues the avoided cost into
        :attr:`stats`. A miss still reports the best (sub-threshold)
        similarity for diagnostics.
        """
        self.stats.lookups += 1
        ns = self._store.get(tenant_id)
        if not ns:
            self.stats.misses += 1
            return CacheResult(hit=False, response=None, similarity=0.0, kind="miss")

        # Layer 1: exact-string fast path.
        exact_eid = self._exact.get(tenant_id, {}).get(key_text)
        if exact_eid is not None:
            return self._record_hit(ns, exact_eid, similarity=1.0, kind="exact")

        # Layer 2: cosine nearest-neighbor scan.
        query_vec = _embed(key_text)
        best_id: int | None = None
        best_sim = 0.0
        for eid, entry in ns.items():
            sim = _cosine(query_vec, entry.embedding)
            # Strict `>` so ties resolve to the earliest-inserted entry —
            # deterministic given insertion order.
            if sim > best_sim:
                best_sim = sim
                best_id = eid

        if best_id is not None and best_sim >= self.threshold:
            # A vector match at ~1.0 that wasn't caught by the exact
            # index means a different key_text embedded identically
            # (hash collision / token-identical-but-not-byte-identical);
            # classify by similarity so the float-noisy 0.999…998 case
            # still reads as "exact".
            kind = "exact" if best_sim >= _EXACT_SIM else "near"
            return self._record_hit(ns, best_id, similarity=best_sim, kind=kind)

        self.stats.misses += 1
        return CacheResult(
            hit=False, response=None, similarity=best_sim, kind="miss"
        )

    def _record_hit(
        self,
        ns: "OrderedDict[int, CacheEntry]",
        eid: int,
        *,
        similarity: float,
        kind: str,
    ) -> CacheResult:
        entry = ns[eid]
        ns.move_to_end(eid)  # LRU touch
        entry.hits += 1
        self.stats.hits += 1
        if kind == "exact":
            self.stats.exact_hits += 1
        else:
            self.stats.near_hits += 1
        self.stats.input_tokens_saved += entry.input_tokens
        self.stats.output_tokens_saved += entry.output_tokens
        self.stats.cost_saved_usd += self._entry_cost(entry)
        return CacheResult(
            hit=True, response=entry.response, similarity=similarity, kind=kind
        )

    def store(self, *, tenant_id: str, key_text: str, response: str) -> None:
        """Insert (or refresh) a (prompt → response) pair in ``tenant_id``.

        The input-token count is estimated from ``key_text`` (the
        prompt) and the output-token count from ``response`` — both
        under the ``len//4`` estimator so cost accounting reconciles
        with the Phase-3 token columns. Re-storing an existing
        ``key_text`` refreshes the entry in place (no duplicate row).
        Eviction (per-tenant LRU) fires if the namespace exceeds
        ``max_entries``.
        """
        ns = self._store.setdefault(tenant_id, OrderedDict())
        exact_index = self._exact.setdefault(tenant_id, {})

        existing_eid = exact_index.get(key_text)
        if existing_eid is not None:
            # Refresh in place — update response, keep the row, promote.
            entry = ns[existing_eid]
            entry.response = response
            entry.output_tokens = estimate_tokens(response)
            ns.move_to_end(existing_eid)
            self.stats.stores += 1
            return

        eid = self._next_id
        self._next_id += 1
        ns[eid] = CacheEntry(
            key_text=key_text,
            embedding=_embed(key_text),
            response=response,
            input_tokens=estimate_tokens(key_text),
            output_tokens=estimate_tokens(response),
        )
        exact_index[key_text] = eid
        ns.move_to_end(eid)
        self.stats.stores += 1
        self._evict_if_needed(tenant_id, ns)

    def _evict_if_needed(
        self, tenant_id: str, ns: "OrderedDict[int, CacheEntry]"
    ) -> None:
        exact_index = self._exact.get(tenant_id, {})
        while len(ns) > self.max_entries:
            evicted_eid, evicted = ns.popitem(last=False)  # drop LRU
            # Keep the exact index in sync — only drop the mapping if it
            # still points at the evicted row (a later refresh could
            # have repointed it, though refresh promotes so that row
            # wouldn't be the LRU victim).
            if exact_index.get(evicted.key_text) == evicted_eid:
                del exact_index[evicted.key_text]
            self.stats.evictions += 1

    def _entry_cost(self, entry: CacheEntry) -> float:
        return (
            entry.input_tokens / 1_000_000 * self.input_usd_per_mtok
            + entry.output_tokens / 1_000_000 * self.output_usd_per_mtok
        )

    def entry_count(self, tenant_id: str | None = None) -> int:
        """Live entry count — for one tenant, or all tenants if ``None``."""
        if tenant_id is not None:
            ns = self._store.get(tenant_id)
            return len(ns) if ns else 0
        return sum(len(ns) for ns in self._store.values())

    def clear(self) -> None:
        """Drop all entries AND reset stats. For test isolation."""
        self._store.clear()
        self._exact.clear()
        self._next_id = 0
        self.stats = CacheStats()


class CachingLLMClient:
    """LLM client wrapper that consults a :class:`SemanticCache` first.

    Tenant is bound at construction (one wrapper per tenant-scoped
    request) so the ``complete`` signature stays Protocol-compatible
    with :class:`context_engine.llm.LLMClient` — callers that already
    hold an ``LLMClient`` can drop this in without changing call sites.
    The cache key defaults to the prompt itself; in the brief-assembly
    flow the prompt is ``query + brief``, which is exactly what we want
    to key on.

    On ``complete``: look up → hit returns the cached response (no
    delegate call, cost saved); miss delegates to the inner client and
    stores the result. ``summarize`` is delegated uncached — summaries
    feed back into briefs and caching them would couple two cache
    domains; out of scope for Day 25.
    """

    def __init__(
        self,
        inner: LLMClientLike,
        cache: SemanticCache,
        *,
        tenant_id: str,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._tenant_id = tenant_id
        self.name = f"cached:{inner.name}"
        self.model = inner.model

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        result = self._cache.lookup(tenant_id=self._tenant_id, key_text=prompt)
        if result.hit and result.response is not None:
            return result.response
        response = self._inner.complete(
            prompt, system=system, max_tokens=max_tokens, temperature=temperature
        )
        self._cache.store(
            tenant_id=self._tenant_id, key_text=prompt, response=response
        )
        return response

    def summarize(self, text: str, *, max_tokens: int = 256) -> str:
        return self._inner.summarize(text, max_tokens=max_tokens)


__all__ = [
    "INPUT_USD_PER_MTOK",
    "OUTPUT_USD_PER_MTOK",
    "DEFAULT_THRESHOLD",
    "DEFAULT_MAX_ENTRIES",
    "CacheEntry",
    "CacheResult",
    "CacheStats",
    "SemanticCache",
    "CachingLLMClient",
]
