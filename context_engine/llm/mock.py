"""Deterministic stub LLM adapter (Day 5, Phase 2).

Activated when:
  * `MOCK_LLM=true` in the environment, OR
  * the configured provider's API key is missing / placeholder, OR
  * `LLM_PROVIDER` is unset / set to `mock`.

Behaviour rules — keep this in sync with `context_engine/llm/__init__.py`'s
docstring and the SKILL §"Mock mode behavior" section:

  * Summarisation returns the first 200 chars of the input plus
    `"...[mock-summary]"` so downstream code can detect mock-mode output.
  * `complete()` echoes a deterministic string derived from the prompt — no
    randomness, no clock dependence. Tests can assert against the exact
    string. Phase 3 / Phase 5 benchmark numbers generated under mock mode
    are flagged `(MOCK-LLM)` in reports.
"""

from __future__ import annotations

from typing import Final

_MOCK_SUFFIX: Final[str] = "...[mock-summary]"
_MOCK_PREFIX: Final[str] = "[mock-llm] "


class MockClient:
    """Cheap, offline, deterministic LLM stand-in."""

    name: str = "mock"
    model: str = "mock-model"

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        # Echo a truncated form of the prompt with a stable prefix. The
        # `system` argument is intentionally not surfaced — we want the
        # mock response to depend ONLY on the user prompt so test
        # assertions stay simple.
        del system, temperature  # mock has no temperature; declared for protocol parity
        body = prompt[:max_tokens] if max_tokens > 0 else prompt
        return _MOCK_PREFIX + body

    def summarize(self, text: str, *, max_tokens: int = 256) -> str:
        # max_tokens is advisory in mock mode — the summary is always the
        # first 200 chars + suffix so tests get stable output regardless of
        # the budget hint.
        del max_tokens
        head = text[:200]
        return head + _MOCK_SUFFIX
