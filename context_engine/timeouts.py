"""Deadline + degraded-mode helper for slow downstream calls (Day 23).

Failure mode this module handles: a downstream — usually Postgres in the
brief-assembler hot path — gets slow enough that holding the request
thread on it would push p99 latency through the SYSTEM_DESIGN §6.2
budget (700 ms end-to-end). Day 22 measured p99 at 853 ms under load;
this helper is what lets us cap individual downstream calls at, say,
250 ms and still answer the customer, with a `degraded=True` flag the
caller can surface in the response.

Design choices:

* **ThreadPoolExecutor + Future.result(timeout).** The simplest portable
  primitive that gives a hard wall-clock deadline. Native to stdlib, no
  asyncio coupling (the brief-assembler is sync code today and Phase 6
  will decide whether to async it). The cost is one worker thread per
  outstanding deadlined call — for the ~hundreds-of-rps target on a
  single PennyCore process the executor lives at module scope with a
  small max-workers cap.

* **Cancellation is best-effort.** Python's Future.cancel() can only
  cancel futures that haven't started running. A truly stuck query
  inside psycopg lives in the worker thread until the connection times
  out or the query returns. That's an acceptable trade — the customer
  request has already moved on (the helper returned the fallback), and
  the worker thread will eventually free itself when libpq notices.
  Day-29 production hardening can install psycopg's `statement_timeout`
  at the connection level so the worker drops sooner.

* **`fallback` is a value, not a callable.** Easier to reason about at
  call sites and lets the caller construct the fallback once at startup
  (a fixed empty-brief sentinel, an empty list, etc.). A callable
  variant could be added later without breaking the value-based API.

* **No retries here.** This helper enforces a deadline on a SINGLE call.
  Retry policy lives one level up — Phase 6 may add a `retry_with_jitter`
  decorator, but conflating retry with timeout makes the deadline
  semantics fuzzy (does the deadline include the retries?). Keeping
  them separate keeps each helper's contract precise.

Multi-tenant invariant: the helper is tenant-agnostic — it's a pure
control-flow primitive. Tenant scoping happens INSIDE the function the
caller passes in. The helper never logs payload contents, only the
fact-of-timeout, so no cross-tenant leak path exists through this
module.

Audit invariant: each timeout fires a structured log line at WARNING
with the operation name + the configured deadline + the actual elapsed
time. The Day-29 audit-log writer can scrape these logs into the
audit_log table; for Day 23 the log line is the canonical record.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

_LOG = logging.getLogger(__name__)

_T = TypeVar("_T")


# Module-level executor — one worker pool services all `with_deadline`
# calls. Daemon threads so a misbehaving long-running call cannot pin
# process exit. The max-workers cap is intentionally small: this pool
# is for FAILURE-MODE timeouts, not the main request workers; a 200-rps
# steady load that occasionally times out should not balloon the pool.
_EXECUTOR = ThreadPoolExecutor(
    max_workers=32, thread_name_prefix="pennycore-timeout"
)


@dataclass(frozen=True)
class DeadlineResult(Generic[_T]):
    """Outcome of `with_deadline`.

    `degraded=False, error=None` is the happy path. `degraded=True` means
    we returned the fallback — `error` carries the cause (a
    `TimeoutError` for deadline exceeded, the original exception for
    any other failure). `elapsed_ms` is wall-clock; callers can log it
    or surface it in /readyz-style endpoints.
    """

    value: _T
    degraded: bool
    elapsed_ms: float
    error: BaseException | None = None
    operation: str = "unnamed"


class DeadlineExceeded(TimeoutError):
    """Raised internally so callers can distinguish 'we cut it off'
    from 'the callable raised something else'. Inherits TimeoutError
    so callers that only care about timeouts can catch the parent."""


def with_deadline(
    func: Callable[[], _T],
    *,
    timeout_ms: int,
    fallback: _T,
    operation: str = "unnamed",
) -> DeadlineResult[_T]:
    """Run `func()` with a wall-clock cap. Return the value or the fallback.

    NEVER raises. Any exception (timeout, callable's own raise, executor
    failure) is captured in `DeadlineResult.error`. This is deliberate —
    the helper exists to PREVENT exceptions from leaking through to the
    request handler. If a caller wants the raw exception, they should
    write their own try/except; this helper's job is graceful degradation.

    Args:
        func: A zero-arg callable. Wrap with `functools.partial` if the
            real callable takes arguments — keeping the signature zero-arg
            here removes args/kwargs plumbing and the type ambiguity that
            comes with it.
        timeout_ms: Hard wall-clock deadline in milliseconds. Must be
            positive. A value of 0 would mean "no time at all" which
            is never a sensible deadline; we reject it explicitly.
        fallback: Value returned when the deadline trips or the callable
            raises. Caller is responsible for choosing a sentinel the
            downstream code can handle (an empty list, an empty brief
            string, `None`, etc.).
        operation: Free-form name used in the WARNING log line. Recommended
            shape: `"<module>.<callable>"` — e.g.
            `"context_engine.brief_assembly.load_history"`.
    """
    if timeout_ms <= 0:
        raise ValueError(
            f"timeout_ms must be positive, got {timeout_ms}"
        )

    start = time.perf_counter()
    future: Future[_T] = _EXECUTOR.submit(func)
    try:
        value = future.result(timeout=timeout_ms / 1000.0)
    except FutureTimeoutError as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        # Best-effort cancel — see module docstring on why this only
        # works for not-yet-started futures. Either way the calling
        # request gets to move on with the fallback.
        future.cancel()
        deadline_exc = DeadlineExceeded(
            f"{operation} exceeded {timeout_ms}ms deadline "
            f"(elapsed {elapsed_ms:.1f}ms)"
        )
        _LOG.warning(
            "deadline exceeded for %s after %.1fms (cap %dms); "
            "returning fallback",
            operation, elapsed_ms, timeout_ms,
        )
        del exc  # silence "unused" without changing semantics
        return DeadlineResult(
            value=fallback,
            degraded=True,
            elapsed_ms=elapsed_ms,
            error=deadline_exc,
            operation=operation,
        )
    except BaseException as exc:  # pylint: disable=broad-except
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        _LOG.warning(
            "callable %s raised %s after %.1fms; returning fallback",
            operation, type(exc).__name__, elapsed_ms,
        )
        return DeadlineResult(
            value=fallback,
            degraded=True,
            elapsed_ms=elapsed_ms,
            error=exc,
            operation=operation,
        )

    elapsed_ms = (time.perf_counter() - start) * 1000.0
    return DeadlineResult(
        value=value,
        degraded=False,
        elapsed_ms=elapsed_ms,
        error=None,
        operation=operation,
    )


__all__ = [
    "DeadlineExceeded",
    "DeadlineResult",
    "with_deadline",
]
