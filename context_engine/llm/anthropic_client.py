"""Anthropic Claude adapter (Day 5, Phase 2).

Default provider for the PennyCore main system. Used by the brief
assembler's optional summarisation path (Phase 2 Day 7), the orchestrator
planner (Day 9), and the LLM-as-judge eval (Day 15 / Day 27).

Models in use:
  * `claude-sonnet-4-6` — system + LLM-as-judge (default).
  * `claude-haiku-4-5-20251001` — cost-sensitive paths (override via
    `ANTHROPIC_MODEL` env or by passing `model=` to `complete()`).

The `anthropic` SDK is imported LAZILY in `__init__` so callers that never
hit this provider don't pay the import cost (and tests in mock mode don't
need the SDK installed).
"""

from __future__ import annotations

from typing import Any


class AnthropicClient:
    name: str = "anthropic"

    def __init__(self, *, api_key: str, model: str = "claude-sonnet-4-6") -> None:
        # Lazy import — the dispatch layer guarantees we only land here
        # when an Anthropic API key is configured, so it's safe to fail
        # loudly if the SDK is missing.
        import anthropic  # noqa: PLC0415  (lazy by design)

        self._client = anthropic.Anthropic(api_key=api_key)
        self.model: str = model

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        # Anthropic's Messages API: `system` is a top-level string param,
        # NOT a message in the `messages` list (different from OpenAI).
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system is not None:
            kwargs["system"] = system

        msg = self._client.messages.create(**kwargs)
        # `msg.content` is a list of content blocks; we only emit text blocks.
        parts: list[str] = []
        for block in msg.content:
            text = getattr(block, "text", None)
            if text is not None:
                parts.append(text)
        return "".join(parts)

    def summarize(self, text: str, *, max_tokens: int = 256) -> str:
        # Cheaper, narrower call than full `complete()` — pinned to a
        # summarisation-shaped system prompt so the brief assembler gets
        # consistent compression behaviour regardless of caller.
        return self.complete(
            prompt=f"Summarize the following customer-history excerpt in <= {max_tokens} tokens. "
                   f"Preserve names, account references, dates, and any pending requests:\n\n{text}",
            system="You are a precise summarizer for a customer-service AI memory layer. "
                   "Never invent facts. If the excerpt is empty, return an empty string.",
            max_tokens=max_tokens,
            temperature=0.0,
        )
