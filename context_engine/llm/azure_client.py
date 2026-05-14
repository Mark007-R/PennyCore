"""Azure adapter (Day 5, Phase 2).

Default endpoint shape: **Azure AI Foundry "Models"**
(`https://<resource>.services.ai.azure.com/models`) — the multi-provider,
OpenAI-compatible REST surface that hosts Kimi-K2.6, gpt-4o-mini,
DeepSeek-V3, Mistral-Large-2407, etc. behind a single API key. This is the
endpoint the takehome adapter is locked to (rule 17: takehome adapters
ALWAYS use `LLM_PROVIDER=azure`).

Fallback shape: classic **Azure OpenAI Service** (`*.openai.azure.com`) —
read from `AZURE_OPENAI_*` env vars by the dispatch layer when the Foundry
vars aren't set. Both shapes ride on the `openai` SDK's `OpenAI` client
with a custom `base_url`; the wire protocol is identical.

`openai` is imported lazily so callers that never select azure don't need
the SDK installed.
"""

from __future__ import annotations


class AzureClient:
    name: str = "azure"

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        api_version: str,
        model: str,
    ) -> None:
        # Lazy import — avoid forcing `openai` into the unit-test path.
        import openai  # noqa: PLC0415

        # The Foundry Models endpoint is OpenAI-compatible REST: same
        # `/chat/completions` route, same JSON shape, same SDK call sites.
        # `default_query` injects `?api-version=...` on every request, which
        # both the Foundry endpoint and the classic Azure OpenAI endpoint
        # require.
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=endpoint.rstrip("/"),
            default_query={"api-version": api_version},
        )
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
