"""Standard OpenAI adapter (Day 5, Phase 2).

Selected when `LLM_PROVIDER=openai` and `OPENAI_API_KEY` is set. Used in
the comparison study (Phase 3 / Phase 5) when a side-by-side run against
public-OpenAI is needed; the production default remains Anthropic Claude
(SKILL §SYSTEM DISCIPLINE RULE: "Pick ONE LLM and stick with it across the
comparison study"). The takehome adapter does NOT use this path — it
strictly uses the Azure adapter to satisfy the external "OpenAI API"
requirement on Foundry / Azure OpenAI Service.

Models:
  * `gpt-4o-mini` — cost-sensitive default.
  * `gpt-4o` — quality bar (override via `OPENAI_MODEL`).

`openai` is imported lazily.
"""

from __future__ import annotations


class OpenAIClient:
    name: str = "openai"

    def __init__(self, *, api_key: str, model: str = "gpt-4o-mini") -> None:
        import openai  # noqa: PLC0415

        self._client = openai.OpenAI(api_key=api_key)
        self.model: str = model

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        choice = resp.choices[0].message.content
        return choice or ""

    def summarize(self, text: str, *, max_tokens: int = 256) -> str:
        return self.complete(
            prompt=f"Summarize the following customer-history excerpt in <= {max_tokens} tokens. "
                   f"Preserve names, account references, dates, and any pending requests:\n\n{text}",
            system="You are a precise summarizer for a customer-service AI memory layer. "
                   "Never invent facts. If the excerpt is empty, return an empty string.",
            max_tokens=max_tokens,
            temperature=0.0,
        )
