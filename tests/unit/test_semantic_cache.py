"""Day 25 — semantic LLM-response cache tests.

Covers:

* :class:`SemanticCache` core: exact hit, near hit, miss, threshold
  boundary (just-below misses, just-at hits), best-similarity
  reporting on miss, hit classification (exact vs near).
* Cost accounting: input+output tokens saved and USD cost saved
  accrue per hit; reconcile with the ``len//4`` estimator and the
  configured per-MTok prices.
* Multi-tenant isolation (SKILL HARD RULE 15): byte-identical prompts
  under two tenants never cross-hit; entry_count is per-namespace.
* Per-tenant LRU eviction: oldest entry drops at the cap; a lookup
  hit promotes (touches) an entry so it survives a subsequent
  eviction; eviction is per-tenant (one tenant's churn doesn't evict
  another's).
* Stats: hit_rate, snapshot shape, lookups/hits/misses accounting,
  clear() resets both store and stats.
* Constructor validation: threshold range, positive max_entries.
* :class:`CachingLLMClient`: delegates on miss + stores; returns
  cached on hit without re-calling the inner client; Protocol-shape
  (name/model surface, summarize passthrough); determinism.
"""
from __future__ import annotations

import pytest

from context_engine.llm.semantic_cache import (
    DEFAULT_THRESHOLD,
    CachingLLMClient,
    CacheStats,
    SemanticCache,
)


class _CountingClient:
    """Inner-client stub that counts complete() calls and echoes a
    deterministic response derived from the prompt."""

    name = "stub"
    model = "stub-model"

    def __init__(self) -> None:
        self.complete_calls = 0
        self.summarize_calls = 0

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        self.complete_calls += 1
        return f"answer:{prompt}"

    def summarize(self, text: str, *, max_tokens: int = 256) -> str:
        self.summarize_calls += 1
        return f"summary:{text[:20]}"


class TestConstructorValidation:
    def test_default_threshold(self) -> None:
        assert SemanticCache().threshold == DEFAULT_THRESHOLD == 0.95

    @pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
    def test_threshold_out_of_range_raises(self, bad: float) -> None:
        with pytest.raises(ValueError):
            SemanticCache(threshold=bad)

    def test_threshold_one_is_allowed(self) -> None:
        # threshold=1.0 is an exact-only cache — the floor of the sweep.
        assert SemanticCache(threshold=1.0).threshold == 1.0

    @pytest.mark.parametrize("bad", [0, -5])
    def test_nonpositive_max_entries_raises(self, bad: int) -> None:
        with pytest.raises(ValueError):
            SemanticCache(max_entries=bad)


class TestExactAndMiss:
    def test_first_lookup_is_miss(self) -> None:
        c = SemanticCache()
        r = c.lookup(tenant_id="acme", key_text="what is my mortgage rate")
        assert r.hit is False
        assert r.kind == "miss"
        assert r.response is None
        assert c.stats.misses == 1

    def test_store_then_exact_hit(self) -> None:
        c = SemanticCache()
        c.store(tenant_id="acme", key_text="what is my mortgage rate", response="6.25%")
        r = c.lookup(tenant_id="acme", key_text="what is my mortgage rate")
        assert r.hit is True
        assert r.kind == "exact"
        assert r.response == "6.25%"
        assert r.similarity == pytest.approx(1.0)
        assert c.stats.exact_hits == 1
        assert c.stats.near_hits == 0

    def test_unrelated_query_misses_but_reports_best_similarity(self) -> None:
        c = SemanticCache(threshold=0.5)
        c.store(tenant_id="acme", key_text="mortgage rate quote", response="A")
        r = c.lookup(tenant_id="acme", key_text="completely different topic entirely")
        assert r.hit is False
        # Best similarity is carried even on a miss for diagnostics.
        assert 0.0 <= r.similarity < 0.5


class TestNearHitAndThreshold:
    def test_near_paraphrase_hits_at_low_threshold(self) -> None:
        # A lexical paraphrase (extra filler words, reordering) shares
        # content tokens, so cosine is high but < 1.0 → near hit.
        c = SemanticCache(threshold=0.5)
        c.store(
            tenant_id="acme",
            key_text="what mortgage rate was i quoted",
            response="6.25%",
        )
        r = c.lookup(
            tenant_id="acme",
            key_text="can you tell me what mortgage rate i was quoted",
        )
        assert r.hit is True
        assert r.kind == "near"
        assert 0.5 <= r.similarity < 1.0
        assert c.stats.near_hits == 1

    def test_same_paraphrase_misses_at_strict_threshold(self) -> None:
        # The same paraphrase that hits at 0.5 misses at the default
        # 0.95 — this gap is the entire Day-25 threshold-sweep story.
        c = SemanticCache(threshold=0.95)
        c.store(
            tenant_id="acme",
            key_text="what mortgage rate was i quoted",
            response="6.25%",
        )
        r = c.lookup(
            tenant_id="acme",
            key_text="can you tell me what mortgage rate i was quoted",
        )
        assert r.hit is False

    def test_threshold_boundary_is_inclusive(self) -> None:
        # A self-lookup scores cosine 1.0; threshold=1.0 must hit it
        # (>= comparison, not >).
        c = SemanticCache(threshold=1.0)
        c.store(tenant_id="acme", key_text="exact text here", response="X")
        r = c.lookup(tenant_id="acme", key_text="exact text here")
        assert r.hit is True
        assert r.kind == "exact"


class TestCostAccounting:
    def test_cost_saved_accrues_per_hit(self) -> None:
        # input "aaaa bbbb" -> estimate_tokens = len//4. response "yyyy".
        c = SemanticCache(threshold=1.0, input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)
        key = "x" * 400  # 400 chars -> 100 input tokens
        resp = "y" * 200  # 200 chars -> 50 output tokens
        c.store(tenant_id="acme", key_text=key, response=resp)
        c.lookup(tenant_id="acme", key_text=key)
        assert c.stats.input_tokens_saved == 100
        assert c.stats.output_tokens_saved == 50
        expected = 100 / 1e6 * 3.0 + 50 / 1e6 * 15.0
        assert c.stats.cost_saved_usd == pytest.approx(expected)

    def test_miss_accrues_no_cost(self) -> None:
        c = SemanticCache()
        c.lookup(tenant_id="acme", key_text="nothing stored yet")
        assert c.stats.cost_saved_usd == 0.0
        assert c.stats.input_tokens_saved == 0

    def test_two_hits_double_the_saving(self) -> None:
        c = SemanticCache(threshold=1.0)
        key = "z" * 400
        c.store(tenant_id="acme", key_text=key, response="r" * 200)
        c.lookup(tenant_id="acme", key_text=key)
        first = c.stats.cost_saved_usd
        c.lookup(tenant_id="acme", key_text=key)
        assert c.stats.cost_saved_usd == pytest.approx(2 * first)
        assert c.stats.hits == 2


class TestMultiTenantIsolation:
    def test_identical_prompt_does_not_cross_tenant(self) -> None:
        c = SemanticCache(threshold=0.5)
        c.store(tenant_id="bank_a", key_text="what is my balance", response="A-SECRET")
        r = c.lookup(tenant_id="bank_b", key_text="what is my balance")
        # Byte-identical prompt under a different tenant must NOT hit.
        assert r.hit is False
        assert r.response is None

    def test_per_tenant_entry_counts(self) -> None:
        c = SemanticCache()
        c.store(tenant_id="bank_a", key_text="q1", response="a")
        c.store(tenant_id="bank_a", key_text="q2", response="b")
        c.store(tenant_id="bank_b", key_text="q3", response="c")
        assert c.entry_count("bank_a") == 2
        assert c.entry_count("bank_b") == 1
        assert c.entry_count() == 3

    def test_same_query_each_tenant_hits_own_answer(self) -> None:
        c = SemanticCache(threshold=1.0)
        c.store(tenant_id="bank_a", key_text="my rate", response="A-rate")
        c.store(tenant_id="bank_b", key_text="my rate", response="B-rate")
        assert c.lookup(tenant_id="bank_a", key_text="my rate").response == "A-rate"
        assert c.lookup(tenant_id="bank_b", key_text="my rate").response == "B-rate"


class TestEviction:
    def test_oldest_evicted_at_cap(self) -> None:
        c = SemanticCache(max_entries=2)
        c.store(tenant_id="acme", key_text="alpha one", response="1")
        c.store(tenant_id="acme", key_text="bravo two", response="2")
        c.store(tenant_id="acme", key_text="charlie three", response="3")
        # "alpha one" was oldest → evicted.
        assert c.entry_count("acme") == 2
        assert c.stats.evictions == 1
        assert c.lookup(tenant_id="acme", key_text="alpha one").hit is False
        assert c.lookup(tenant_id="acme", key_text="charlie three").hit is True

    def test_hit_promotes_entry_and_saves_it_from_eviction(self) -> None:
        c = SemanticCache(max_entries=2, threshold=1.0)
        c.store(tenant_id="acme", key_text="alpha one", response="1")
        c.store(tenant_id="acme", key_text="bravo two", response="2")
        # Touch "alpha one" so it's now most-recently-used.
        assert c.lookup(tenant_id="acme", key_text="alpha one").hit is True
        # Insert a third → LRU evicts "bravo two", NOT the touched "alpha one".
        c.store(tenant_id="acme", key_text="charlie three", response="3")
        assert c.lookup(tenant_id="acme", key_text="alpha one").hit is True
        assert c.lookup(tenant_id="acme", key_text="bravo two").hit is False

    def test_eviction_is_per_tenant(self) -> None:
        # bank_a churns past its cap; bank_b's single entry survives.
        c = SemanticCache(max_entries=1)
        c.store(tenant_id="bank_b", key_text="keep me", response="B")
        c.store(tenant_id="bank_a", key_text="aaa one", response="1")
        c.store(tenant_id="bank_a", key_text="aaa two", response="2")
        assert c.entry_count("bank_b") == 1
        assert c.entry_count("bank_a") == 1
        assert c.lookup(tenant_id="bank_b", key_text="keep me").hit is True


class TestStats:
    def test_hit_rate(self) -> None:
        c = SemanticCache(threshold=1.0)
        c.store(tenant_id="acme", key_text="hello world", response="hi")
        c.lookup(tenant_id="acme", key_text="hello world")  # hit
        c.lookup(tenant_id="acme", key_text="unseen query string")  # miss
        assert c.stats.hit_rate == pytest.approx(0.5)

    def test_hit_rate_zero_lookups(self) -> None:
        assert CacheStats().hit_rate == 0.0

    def test_snapshot_keys(self) -> None:
        snap = SemanticCache().stats.snapshot()
        for key in (
            "lookups",
            "hits",
            "misses",
            "exact_hits",
            "near_hits",
            "stores",
            "evictions",
            "hit_rate",
            "input_tokens_saved",
            "output_tokens_saved",
            "cost_saved_usd",
        ):
            assert key in snap

    def test_clear_resets_store_and_stats(self) -> None:
        c = SemanticCache(threshold=1.0)
        c.store(tenant_id="acme", key_text="some text", response="r")
        c.lookup(tenant_id="acme", key_text="some text")
        c.clear()
        assert c.entry_count() == 0
        assert c.stats.lookups == 0
        assert c.stats.hits == 0
        # After clear, a previously-hot key misses.
        assert c.lookup(tenant_id="acme", key_text="some text").hit is False


class TestCachingLLMClient:
    def test_miss_delegates_and_stores(self) -> None:
        inner = _CountingClient()
        cache = SemanticCache(threshold=1.0)
        client = CachingLLMClient(inner, cache, tenant_id="acme")
        out = client.complete("first prompt text")
        assert out == "answer:first prompt text"
        assert inner.complete_calls == 1
        assert cache.entry_count("acme") == 1

    def test_second_identical_call_hits_cache_no_delegate(self) -> None:
        inner = _CountingClient()
        cache = SemanticCache(threshold=1.0)
        client = CachingLLMClient(inner, cache, tenant_id="acme")
        client.complete("repeat me exactly")
        out2 = client.complete("repeat me exactly")
        assert out2 == "answer:repeat me exactly"
        # Inner client called only ONCE despite two complete() calls.
        assert inner.complete_calls == 1
        assert cache.stats.hits == 1

    def test_two_tenants_share_cache_object_but_not_entries(self) -> None:
        inner = _CountingClient()
        cache = SemanticCache(threshold=1.0)
        a = CachingLLMClient(inner, cache, tenant_id="bank_a")
        b = CachingLLMClient(inner, cache, tenant_id="bank_b")
        a.complete("identical prompt")
        b.complete("identical prompt")
        # Same prompt, different tenants → two delegate calls (no cross-hit).
        assert inner.complete_calls == 2

    def test_name_and_model_surface(self) -> None:
        inner = _CountingClient()
        client = CachingLLMClient(inner, SemanticCache(), tenant_id="acme")
        assert client.name == "cached:stub"
        assert client.model == "stub-model"

    def test_summarize_passthrough_uncached(self) -> None:
        inner = _CountingClient()
        client = CachingLLMClient(inner, SemanticCache(), tenant_id="acme")
        out = client.summarize("a long body of text to summarize")
        assert out.startswith("summary:")
        assert inner.summarize_calls == 1


class TestCacheBenchmark:
    """Smoke + invariant coverage for the Day-25 cost-reduction bench.

    Uses a 20-pair slice of the real dataset to stay fast while still
    exercising the production retrieval + cache path end-to-end.
    """

    def _pairs(self):
        from benchmarks.dataset_loader import load_pairs

        return load_pairs()[:20]

    def test_paraphrase_is_deterministic_and_adds_filler(self) -> None:
        from benchmarks.semantic_cache_bench import paraphrase

        a = paraphrase("what is my rate", 0)
        b = paraphrase("what is my rate", 0)
        assert a == b  # deterministic
        assert "what is my rate" in a
        assert len(a) > len("what is my rate")  # filler added

    def test_build_workload_is_deterministic(self) -> None:
        from benchmarks.semantic_cache_bench import build_briefs, build_workload

        pairs = self._pairs()
        briefs = build_briefs(pairs)
        w1 = build_workload(pairs, briefs, seed=42)
        w2 = build_workload(pairs, briefs, seed=42)
        assert [r.key_text for r in w1] == [r.key_text for r in w2]
        # Every cold contact is present (one per pair).
        assert sum(1 for r in w1 if r.kind == "cold") == len(pairs)

    def test_per_customer_scope_has_zero_false_hits(self) -> None:
        # The headline invariant: per-customer scoping never serves the
        # wrong customer's answer, even at a permissive threshold.
        from benchmarks.semantic_cache_bench import (
            build_briefs,
            build_workload,
            run_cache,
        )

        pairs = self._pairs()
        briefs = build_briefs(pairs)
        workload = build_workload(pairs, briefs, seed=42)
        row = run_cache(workload, threshold=0.7, scope="tenant_customer")
        assert row["false_hits"] == 0

    def test_tenant_scope_exact_threshold_is_safe(self) -> None:
        # At threshold 1.0 even the tenant-wide scope is false-hit-free
        # (no two distinct briefs are byte-identical).
        from benchmarks.semantic_cache_bench import (
            build_briefs,
            build_workload,
            run_cache,
        )

        pairs = self._pairs()
        briefs = build_briefs(pairs)
        workload = build_workload(pairs, briefs, seed=42)
        row = run_cache(workload, threshold=1.0, scope="tenant")
        assert row["false_hits"] == 0
        assert row["near_hits"] == 0  # exact-only

    def test_isolation_probe_holds(self) -> None:
        from benchmarks.semantic_cache_bench import build_briefs, isolation_probe

        pairs = self._pairs()
        briefs = build_briefs(pairs)
        probe = isolation_probe(pairs, briefs)
        assert probe["cross_namespace_hits"] == 0
        assert probe["isolation_holds"] is True
