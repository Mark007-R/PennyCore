"""Multi-provider LLM dispatch layer (Day 5, Phase 2).

The rest of the codebase MUST go through `get_client()` — never import a
provider SDK directly, never read `ANTHROPIC_API_KEY` / `AZURE_API_KEY` /
`OPENAI_API_KEY` from outside this package.

Resolution order (mirrors TASK1 SKILL §LLM PROVIDER MODE):

1. `MOCK_LLM=true` → `MockClient` (overrides everything; useful for offline
   benchmarks and unit tests).
2. `LLM_PROVIDER=anthropic` + valid `ANTHROPIC_API_KEY` → `AnthropicClient`
   (default for the main system; model `claude-sonnet-4-6`).
3. `LLM_PROVIDER=azure`  + valid Azure vars → `AzureClient` (Foundry Models
   endpoint by default — Kimi-K2.6 / gpt-4o-mini / etc.; falls back to the
   classic Azure OpenAI Service vars when Foundry vars are blank).
4. `LLM_PROVIDER=openai` + valid `OPENAI_API_KEY` → `OpenAIClient`.
5. Anything else (no provider, placeholder keys, missing vars) → `MockClient`
   with a warning so degraded mode is visible in logs.

Provider SDKs are imported LAZILY inside each client constructor — installing
`anthropic` / `openai` is only required if that provider is actually selected.
This keeps the unit-test path zero-dependency and the mock-mode container
small.
"""

from __future__ import annotations

import os
import warnings
from typing import Protocol, runtime_checkable

from context_engine.llm.mock import MockClient


@runtime_checkable
class LLMClient(Protocol):
    """The minimal contract every provider adapter satisfies.

    Day 5 surface is intentionally small (`complete` + `summarize`). Day 9
    extends with `propose_actions` (tool-calling JSON output for the
    orchestrator planner), and Day 15 adds `judge` (LLM-as-judge for the
    Phase 3 quality scoring). All future methods follow the same pattern:
    declared on the Protocol, implemented in every adapter.
    """

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

    def summarize(self, text: str, *, max_tokens: int = 256) -> str: ...


def _is_placeholder(value: str | None) -> bool:
    """Detect template values from `.env.example` so the dispatcher falls
    back to mock mode rather than 401-ing on a fake key."""
    if not value:
        return True
    needles = ("REPLACE-WITH", "YOUR-KEY", "YOUR-RESOURCE", "YOUR-DEPLOYMENT")
    return any(n in value for n in needles)


def get_client() -> LLMClient:
    """Return the active LLM adapter, resolved fresh from the environment.

    Cheap to call — no network, no SDK construction unless a real provider
    is selected. Callers should hold the returned client for the duration of
    a request rather than re-resolving per LLM call (the lazy SDK imports
    have non-trivial cost on first hit).
    """
    if os.getenv("MOCK_LLM", "").lower() in ("true", "1", "yes"):
        return MockClient()

    provider = os.getenv("LLM_PROVIDER", "mock").lower()

    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if _is_placeholder(api_key):
            warnings.warn(
                "LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY missing/placeholder; "
                "falling back to MockClient.",
                stacklevel=2,
            )
            return MockClient()
        from context_engine.llm.anthropic_client import AnthropicClient

        return AnthropicClient(
            api_key=api_key or "",
            model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        )

    if provider == "azure":
        # Foundry Models endpoint preferred; classic Azure OpenAI Service as fallback.
        api_key = os.getenv("AZURE_API_KEY") or os.getenv("AZURE_OPENAI_API_KEY")
        endpoint = os.getenv("AZURE_ENDPOINT") or os.getenv("AZURE_OPENAI_ENDPOINT")
        api_version = (
            os.getenv("AZURE_API_VERSION") or os.getenv("AZURE_OPENAI_API_VERSION") or "2024-05-01-preview"
        )
        model = os.getenv("LLM_MODEL") or os.getenv("AZURE_OPENAI_DEPLOYMENT")
        if _is_placeholder(api_key) or _is_placeholder(endpoint) or not model:
            warnings.warn(
                "LLM_PROVIDER=azure but AZURE_API_KEY / AZURE_ENDPOINT / LLM_MODEL "
                "missing or placeholder; falling back to MockClient.",
                stacklevel=2,
            )
            return MockClient()
        from context_engine.llm.azure_client import AzureClient

        return AzureClient(
            api_key=api_key or "",
            endpoint=endpoint or "",
            api_version=api_version,
            model=model,
        )

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if _is_placeholder(api_key):
            warnings.warn(
                "LLM_PROVIDER=openai but OPENAI_API_KEY missing/placeholder; "
                "falling back to MockClient.",
                stacklevel=2,
            )
            return MockClient()
        from context_engine.llm.openai_client import OpenAIClient

        return OpenAIClient(
            api_key=api_key or "",
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        )

    # Explicit `mock` or unset → silent (the user knows). Unknown provider
    # value → warn so the misconfiguration is visible in logs / CI rather
    # than silently degrading to mock mode.
    if provider not in ("mock", ""):
        warnings.warn(
            f"LLM_PROVIDER={provider!r} is not recognized; falling back to "
            "MockClient. Supported values: anthropic, azure, openai, mock.",
            stacklevel=2,
        )
    return MockClient()


__all__ = ["LLMClient", "MockClient", "get_client"]
