"""Semantic-cache cost-reduction benchmark — Day 25, Phase 5.

Measures what the Day-25 :class:`context_engine.llm.semantic_cache.SemanticCache`
buys on a realistic customer-service workload: how much LLM spend a
semantic cache eliminates, how the savings trade off against the risk
of serving the *wrong* cached answer, and that the cache never leaks
across tenants.

## The workload model

Real support traffic isn't 200 unique questions — customers come back,
and they rarely re-type the exact same words. We model that:

* **Cold contacts** (one per benchmark pair): the first time a
  (customer, query) is seen. Always a cache MISS — populates the cache.
* **Exact returns**: the same customer asks the identical question
  again later. An exact-string cache catches these.
* **Paraphrase returns**: the same customer asks the *same thing* in
  different words (deterministic lexical paraphrase — filler words,
  reordering, casing). An exact cache MISSES these; only a semantic
  cache can catch them. This is the entire reason to run a semantic
  cache instead of a dict.

``--return-rate`` controls how many cold contacts come back;
``--paraphrase-share`` splits returns between exact and paraphrase.

## The cache key

``key_text = query + "\\n" + brief`` where the brief is assembled by
the Day-14 semantic strategy at the production-default 8K budget. The
brief dominates the token count (and therefore the cost), so caching
on (query, brief) is caching on the actual LLM input.

## What we measure, per threshold

We sweep the near-hit threshold and report, for each:

* **hit_rate / exact_hits / near_hits** — how much traffic was served
  from cache, split by match kind.
* **cost_saved_usd / cost_reduction_pct** — dollars eliminated vs. a
  no-cache baseline that calls the LLM on every request. Priced at
  Claude Sonnet 4.x list (input $3 / output $15 per MTok).
* **false_hit_rate** — the counterintuitive cost. We store each pair's
  ground-truth answer as the cached payload; a hit whose returned
  payload ≠ the requesting pair's ground truth is a *wrong answer
  served from cache*. Lowering the threshold captures more paraphrases
  (good) but eventually starts matching genuinely-different questions
  that happen to share words (bad). The sweep makes that curve visible
  so production can pick a threshold on the right side of it.

## Multi-tenant isolation

A dedicated probe stores answers under tenant A and replays the
identical prompts under tenant B; ``cross_tenant_hits`` must be 0
(SKILL HARD RULE 15).

Mock-mode note: everything here is deterministic and LLM-free. The
"responses" are the dataset's ground-truth strings (so false hits are
detectable); no provider is called. Real-LLM cost numbers would use
the same token counts × the same prices — the savings *ratio* is
LLM-independent because it's a function of hit rate, not of what the
LLM returns.
"""
from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass
from pathlib import Path

from benchmarks.dataset_loader import BenchmarkPair, load_pairs
from context_engine.brief_assembly import pack_segments
from context_engine.llm.semantic_cache import (
    INPUT_USD_PER_MTOK,
    OUTPUT_USD_PER_MTOK,
    SemanticCache,
)
from context_engine.retrieval.recency import estimate_tokens
from context_engine.retrieval.semantic import semantic_segments

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
DEFAULT_BUDGET = 8000
DEFAULT_THRESHOLDS = [1.0, 0.95, 0.9, 0.8, 0.7, 0.6]
DEFAULT_RETURN_RATE = 0.5
DEFAULT_PARAPHRASE_SHARE = 0.5
DEFAULT_SEED = 42

# Deterministic lexical paraphrase templates: (prefix, suffix). Each
# adds filler tokens around the original query, preserving the content
# words so a hashed-BoW cosine stays high but below 1.0. Models the
# "same question, different words" return traffic an exact cache can't
# catch. NOT deep semantic paraphrase (different words, same meaning) —
# that needs a neural embedder; see semantic_cache.py docstring.
_PARAPHRASE_TEMPLATES = [
    ("can you tell me ", ""),
    ("i would like to know ", " please"),
    ("quick question ", " thanks"),
    ("hey just wondering ", ""),
    ("could you please share ", " with me"),
]


def paraphrase(query: str, variant: int) -> str:
    """Deterministic lexical paraphrase of ``query`` for ``variant``."""
    prefix, suffix = _PARAPHRASE_TEMPLATES[variant % len(_PARAPHRASE_TEMPLATES)]
    return f"{prefix}{query.lower()}{suffix}"


def _key_text(query: str, brief: str) -> str:
    """The cache key text = query + brief (the LLM input)."""
    return f"{query}\n{brief}"


def _call_cost_usd(key_text: str, response: str) -> float:
    """LLM call cost: input (prompt) + output (response) tokens priced."""
    return (
        estimate_tokens(key_text) / 1_000_000 * INPUT_USD_PER_MTOK
        + estimate_tokens(response) / 1_000_000 * OUTPUT_USD_PER_MTOK
    )


def build_briefs(
    pairs: list[BenchmarkPair], *, token_budget: int = DEFAULT_BUDGET
) -> dict[str, str]:
    """Assemble one semantic-strategy brief per pair, keyed by pair_id."""
    briefs: dict[str, str] = {}
    for p in pairs:
        segments = semantic_segments(
            query=p.query,
            messages=p.history.messages,
            actions=p.history.actions,
        )
        briefs[p.pair_id] = pack_segments(
            segments,
            token_budget=token_budget,
            header=f"Borrower: {p.customer_id} (tenant={p.tenant_id})",
        )
    return briefs


@dataclass(frozen=True)
class Request:
    """One workload request. ``ground_truth`` doubles as the cached
    payload so false hits (wrong cached answer) are detectable."""

    tenant_id: str
    customer_id: str
    pair_id: str
    key_text: str
    ground_truth: str
    kind: str  # "cold" | "exact_return" | "paraphrase_return"
    call_cost_usd: float


# Cache-namespace scoping strategies. The Day-25 headline is the
# contrast between them: "tenant" keys the cache by tenant only —
# every customer of a bank shares one namespace; "tenant_customer"
# gives each customer their own namespace. Briefs are mostly
# boilerplate (header, channel markers, shared mortgage vocabulary),
# so under "tenant" scope two *different* customers' (query, brief)
# pairs look similar to a hashed-BoW cosine — a near-hit then serves
# the wrong customer's answer. Per-customer scoping makes that
# impossible by construction.
SCOPES = ("tenant", "tenant_customer")


def _namespace(req: Request, scope: str) -> str:
    if scope == "tenant_customer":
        return f"{req.tenant_id}:{req.customer_id}"
    return req.tenant_id


def build_workload(
    pairs: list[BenchmarkPair],
    briefs: dict[str, str],
    *,
    return_rate: float = DEFAULT_RETURN_RATE,
    paraphrase_share: float = DEFAULT_PARAPHRASE_SHARE,
    seed: int = DEFAULT_SEED,
) -> list[Request]:
    """Build a cold-contacts-then-returns request stream.

    All cold contacts are emitted first (so the cache is populated
    before any return arrives), then the sampled return traffic.
    Deterministic given ``seed``.
    """
    rng = random.Random(seed)
    cold: list[Request] = []
    returns: list[Request] = []
    for p in pairs:
        brief = briefs[p.pair_id]
        cold_key = _key_text(p.query, brief)
        cold.append(
            Request(
                tenant_id=p.tenant_id,
                customer_id=p.customer_id,
                pair_id=p.pair_id,
                key_text=cold_key,
                ground_truth=p.ground_truth,
                kind="cold",
                call_cost_usd=_call_cost_usd(cold_key, p.ground_truth),
            )
        )
        if rng.random() < return_rate:
            if rng.random() < paraphrase_share:
                pq = paraphrase(p.query, rng.randint(0, len(_PARAPHRASE_TEMPLATES) - 1))
                pk = _key_text(pq, brief)
                returns.append(
                    Request(
                        tenant_id=p.tenant_id,
                        customer_id=p.customer_id,
                        pair_id=p.pair_id,
                        key_text=pk,
                        ground_truth=p.ground_truth,
                        kind="paraphrase_return",
                        call_cost_usd=_call_cost_usd(pk, p.ground_truth),
                    )
                )
            else:
                returns.append(
                    Request(
                        tenant_id=p.tenant_id,
                        customer_id=p.customer_id,
                        pair_id=p.pair_id,
                        key_text=cold_key,
                        ground_truth=p.ground_truth,
                        kind="exact_return",
                        call_cost_usd=_call_cost_usd(cold_key, p.ground_truth),
                    )
                )
    return cold + returns


def run_cache(
    workload: list[Request],
    *,
    threshold: float,
    scope: str = "tenant",
    max_entries: int = 100_000,
) -> dict[str, object]:
    """Replay ``workload`` through a fresh cache at ``threshold`` / ``scope``.

    A miss calls the (mock) LLM and stores the answer; a hit returns
    the cached answer for free. Tracks cost saved vs. a no-cache
    baseline and false hits (cached answer ≠ requesting pair's truth).
    The cache namespace is chosen by ``scope`` — see :data:`SCOPES`.
    """
    cache = SemanticCache(threshold=threshold, max_entries=max_entries)
    no_cache_cost = 0.0
    saved_cost = 0.0
    false_hits = 0
    kind_hits: dict[str, int] = {"exact_return": 0, "paraphrase_return": 0, "cold": 0}

    for req in workload:
        ns = _namespace(req, scope)
        no_cache_cost += req.call_cost_usd
        res = cache.lookup(tenant_id=ns, key_text=req.key_text)
        if res.hit and res.response is not None:
            saved_cost += req.call_cost_usd
            kind_hits[req.kind] += 1
            if res.response != req.ground_truth:
                false_hits += 1
        else:
            cache.store(
                tenant_id=ns,
                key_text=req.key_text,
                response=req.ground_truth,
            )

    stats = cache.stats
    return {
        "scope": scope,
        "threshold": threshold,
        "lookups": stats.lookups,
        "hits": stats.hits,
        "misses": stats.misses,
        "exact_hits": stats.exact_hits,
        "near_hits": stats.near_hits,
        "hit_rate": round(stats.hit_rate, 6),
        "hits_by_request_kind": kind_hits,
        "no_cache_cost_usd": round(no_cache_cost, 6),
        "cost_saved_usd": round(saved_cost, 6),
        "cost_reduction_pct": round(
            (saved_cost / no_cache_cost * 100) if no_cache_cost else 0.0, 4
        ),
        "false_hits": false_hits,
        "false_hit_rate": round(false_hits / stats.hits, 6) if stats.hits else 0.0,
        "cache_internal_cost_saved_usd": round(stats.cost_saved_usd, 6),
    }


def isolation_probe(pairs: list[BenchmarkPair], briefs: dict[str, str]) -> dict[str, object]:
    """Prove namespace isolation: entries stored under one namespace are
    invisible to a *different* namespace.

    Store every pair's answer under a single namespace ``"namespace_A"``,
    then replay the identical prompts under an empty ``"namespace_B"``.
    A correct cache returns ZERO hits — namespace B has no entries, so
    no lookup can read namespace A's data, regardless of how lexically
    similar the prompts are. Threshold is set deliberately low (0.5) so
    this proves the isolation is *structural* (separate namespaces), not
    an accident of the similarity gate.
    """
    cache = SemanticCache(threshold=0.5, max_entries=100_000)
    for p in pairs:
        cache.store(
            tenant_id="namespace_A",
            key_text=_key_text(p.query, briefs[p.pair_id]),
            response=p.ground_truth,
        )
    cross_hits = sum(
        1
        for p in pairs
        if cache.lookup(
            tenant_id="namespace_B", key_text=_key_text(p.query, briefs[p.pair_id])
        ).hit
    )
    return {
        "method": "store under namespace_A; replay identical prompts under empty namespace_B",
        "threshold": 0.5,
        "prompts_replayed_cross_namespace": len(pairs),
        "cross_namespace_hits": cross_hits,
        "isolation_holds": cross_hits == 0,
    }


def run(
    *,
    thresholds: list[float] | None = None,
    return_rate: float = DEFAULT_RETURN_RATE,
    paraphrase_share: float = DEFAULT_PARAPHRASE_SHARE,
    seed: int = DEFAULT_SEED,
    token_budget: int = DEFAULT_BUDGET,
) -> dict[str, object]:
    """Full benchmark: build workload, sweep thresholds, probe isolation."""
    thresholds = thresholds or DEFAULT_THRESHOLDS
    pairs = load_pairs()
    briefs = build_briefs(pairs, token_budget=token_budget)
    workload = build_workload(
        pairs,
        briefs,
        return_rate=return_rate,
        paraphrase_share=paraphrase_share,
        seed=seed,
    )
    kind_counts: dict[str, int] = {}
    for r in workload:
        kind_counts[r.kind] = kind_counts.get(r.kind, 0) + 1
    sweeps = {
        scope: [run_cache(workload, threshold=t, scope=scope) for t in thresholds]
        for scope in SCOPES
    }
    return {
        "config": {
            "n_pairs": len(pairs),
            "token_budget": token_budget,
            "return_rate": return_rate,
            "paraphrase_share": paraphrase_share,
            "seed": seed,
            "thresholds": thresholds,
            "scopes": list(SCOPES),
            "pricing_usd_per_mtok": {
                "input": INPUT_USD_PER_MTOK,
                "output": OUTPUT_USD_PER_MTOK,
            },
            "llm_mode": "mock (deterministic; responses are dataset ground-truth)",
        },
        "workload": {
            "total_requests": len(workload),
            "request_kind_counts": kind_counts,
        },
        "threshold_sweep_by_scope": sweeps,
        "isolation_probe": isolation_probe(pairs, briefs),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--return-rate", type=float, default=DEFAULT_RETURN_RATE)
    parser.add_argument(
        "--paraphrase-share", type=float, default=DEFAULT_PARAPHRASE_SHARE
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--token-budget", type=int, default=DEFAULT_BUDGET)
    parser.add_argument(
        "--thresholds",
        type=str,
        default="",
        help="Comma-separated floats. Empty = default sweep.",
    )
    parser.add_argument(
        "--out", type=Path, default=RESULTS_DIR / "phase5_semantic_cache.json"
    )
    args = parser.parse_args()

    thresholds = (
        [float(t) for t in args.thresholds.split(",") if t]
        if args.thresholds
        else None
    )
    payload = run(
        thresholds=thresholds,
        return_rate=args.return_rate,
        paraphrase_share=args.paraphrase_share,
        seed=args.seed,
        token_budget=args.token_budget,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload["threshold_sweep_by_scope"], indent=2, sort_keys=True))
    print(json.dumps(payload["isolation_probe"], indent=2, sort_keys=True))
    print(f"\nWrote benchmark to {args.out}")


if __name__ == "__main__":
    main()
