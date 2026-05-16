"""Strategy registry — Phase-3 retrieval comparison study.

Lives in its own module (instead of inline in
:mod:`benchmarks.context_engine_bench`) so the registry has exactly
**one** canonical import path. Day 13 hit a subtle bug where running
the harness as ``python -m benchmarks.context_engine_bench`` loads the
harness twice — once as ``__main__``, once as
``benchmarks.context_engine_bench`` when strategies modules import it
back — and strategies register into the second copy while the runner
reads from the first. Splitting the dict out fixes that: every
import path resolves to the same module instance, so registrations
land where the runner looks for them.

Public surface (importable by strategy modules + the runner):

* :class:`RetrievalFn` — strategy function signature.
* :func:`register_strategy` — register a strategy (optionally with a
  per-strategy ``token_budget`` override).
* :func:`list_strategies` — sorted list of registered names.
* :func:`get_strategy_budget` — per-strategy budget, falling back to
  the runner's default.
* :data:`_STRATEGIES`, :data:`_STRATEGY_BUDGETS` — module-private
  dicts; tests reach into these for cleanup, which is the only
  reason they're exposed at all.
"""
from __future__ import annotations

from typing import Callable

from benchmarks.dataset_loader import BenchmarkPair
from contracts import BriefSegment

# A strategy is anything callable as ``(pair, token_budget) -> list[BriefSegment]``.
# Registration happens at import time of the strategy modules; the runner
# doesn't know what strategies exist beyond what the registry says.
RetrievalFn = Callable[[BenchmarkPair, int], list[BriefSegment]]

_STRATEGIES: dict[str, RetrievalFn] = {}
_STRATEGY_BUDGETS: dict[str, int] = {}


def register_strategy(
    name: str, fn: RetrievalFn, *, token_budget: int | None = None
) -> None:
    """Add a strategy to the registry. Last-write-wins for re-registration.

    Re-registration is allowed (not raised on) so a notebook re-import
    can iterate on a strategy without restarting the kernel.

    The optional ``token_budget`` is a per-strategy budget override that
    the runner uses **instead of** its CLI/default budget for this
    strategy only. Day 13's naive baseline uses this to declare its
    own 50K-token "dump everything" budget — the strategy is part of
    the comparison precisely because it ignores the 8K policy that
    other strategies respect, so it has to own that knob. Strategies
    that don't set this argument inherit the runner's default (the
    common case). Passing ``None`` (or omitting it) on a re-register
    clears any prior override, restoring the runner-default path.
    """
    _STRATEGIES[name] = fn
    if token_budget is not None:
        if token_budget <= 0:
            raise ValueError(
                f"token_budget for strategy {name!r} must be positive, "
                f"got {token_budget}"
            )
        _STRATEGY_BUDGETS[name] = token_budget
    else:
        _STRATEGY_BUDGETS.pop(name, None)


def list_strategies() -> list[str]:
    return sorted(_STRATEGIES)


def get_strategy_budget(name: str, default: int) -> int:
    """Per-strategy budget override, falling back to ``default``.

    Exposed for tests + the runner so the budget resolution is in
    one place. The CLI's ``--token-budget`` always overrides via the
    ``default`` argument; the per-strategy registration is the
    fallback when the CLI doesn't pass one.
    """
    return _STRATEGY_BUDGETS.get(name, default)
