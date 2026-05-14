"""Day 5 — LLM dispatch + MockClient tests.

Goal: prove the dispatch resolution rules in `context_engine.llm.get_client`
match the SKILL contract, AND that MockClient behaviour is deterministic
enough for downstream tests to assert against.

Real-provider tests (Anthropic / Azure / OpenAI) are NOT exercised here —
those require either the SDK package OR live network. The dispatch tests
verify that with placeholder env values the code falls back to MockClient,
which is the only branch we can fully test offline.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator

import pytest

from context_engine.llm import LLMClient, MockClient, get_client
from context_engine.llm.mock import _MOCK_PREFIX, _MOCK_SUFFIX


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Wipe every LLM-related env var before each test so the dispatch
    resolution starts from a known baseline. Required because `.env` is
    loaded on import of `context_engine.api`, which earlier tests may
    have triggered."""
    for var in (
        "MOCK_LLM",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "AZURE_API_KEY",
        "AZURE_ENDPOINT",
        "AZURE_API_VERSION",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_DEPLOYMENT",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ---------------------------------------------------------------------------
# MockClient
# ---------------------------------------------------------------------------


class TestMockClient:
    def test_complete_is_deterministic(self) -> None:
        c = MockClient()
        out_a = c.complete("hello world")
        out_b = c.complete("hello world")
        assert out_a == out_b
        assert out_a.startswith(_MOCK_PREFIX)
        assert "hello world" in out_a

    def test_complete_truncates_at_max_tokens(self) -> None:
        c = MockClient()
        prompt = "x" * 1000
        out = c.complete(prompt, max_tokens=50)
        # Mock truncates the *prompt* to max_tokens chars, then prepends prefix.
        assert out == _MOCK_PREFIX + ("x" * 50)

    def test_summarize_short_input(self) -> None:
        c = MockClient()
        out = c.summarize("Customer asked about their W-2.")
        assert out.endswith(_MOCK_SUFFIX)
        assert "W-2" in out

    def test_summarize_long_input_caps_at_200_chars(self) -> None:
        c = MockClient()
        long_text = "abcdefghij" * 100  # 1000 chars
        out = c.summarize(long_text, max_tokens=999)
        # First 200 chars + suffix; max_tokens is advisory in mock mode.
        assert out == ("abcdefghij" * 20) + _MOCK_SUFFIX

    def test_satisfies_protocol(self) -> None:
        # Sanity check that MockClient is recognized as an LLMClient at
        # runtime (we use `runtime_checkable` Protocols).
        c: LLMClient = MockClient()
        assert isinstance(c, LLMClient)


# ---------------------------------------------------------------------------
# get_client() resolution
# ---------------------------------------------------------------------------


class TestGetClientResolution:
    def test_unset_env_returns_mock(self) -> None:
        c = get_client()
        assert isinstance(c, MockClient)
        assert c.name == "mock"

    def test_mock_llm_overrides_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Even with a real-looking Anthropic key, MOCK_LLM=true wins.
        monkeypatch.setenv("MOCK_LLM", "true")
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-prod-real-key-not-actually-used")
        c = get_client()
        assert isinstance(c, MockClient)

    @pytest.mark.parametrize("flag_value", ["TRUE", "True", "1", "yes"])
    def test_mock_llm_truthy_values(
        self, monkeypatch: pytest.MonkeyPatch, flag_value: str
    ) -> None:
        monkeypatch.setenv("MOCK_LLM", flag_value)
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        # No key — but MOCK_LLM=truthy short-circuits the lookup before that matters.
        assert isinstance(get_client(), MockClient)

    def test_anthropic_with_placeholder_key_falls_back_to_mock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-REPLACE-WITH-YOUR-KEY")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            c = get_client()
        assert isinstance(c, MockClient)
        assert any("anthropic" in str(w.message).lower() for w in caught)

    def test_anthropic_with_no_key_falls_back_to_mock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        # No ANTHROPIC_API_KEY at all.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            c = get_client()
        assert isinstance(c, MockClient)
        assert any("anthropic" in str(w.message).lower() for w in caught)

    def test_azure_with_placeholder_endpoint_falls_back_to_mock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        monkeypatch.setenv("AZURE_API_KEY", "real-looking-key-12345")
        monkeypatch.setenv("AZURE_ENDPOINT", "https://YOUR-RESOURCE.services.ai.azure.com/models")
        monkeypatch.setenv("LLM_MODEL", "Kimi-K2.6")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            c = get_client()
        assert isinstance(c, MockClient)
        assert any("azure" in str(w.message).lower() for w in caught)

    def test_azure_with_no_model_falls_back_to_mock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        monkeypatch.setenv("AZURE_API_KEY", "real-looking-key")
        monkeypatch.setenv("AZURE_ENDPOINT", "https://prod.services.ai.azure.com/models")
        # No LLM_MODEL — Foundry needs the deployment name.
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            c = get_client()
        assert isinstance(c, MockClient)
        assert any("azure" in str(w.message).lower() for w in caught)

    def test_openai_with_placeholder_key_falls_back_to_mock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-REPLACE-WITH-YOUR-KEY")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            c = get_client()
        assert isinstance(c, MockClient)
        assert any("openai" in str(w.message).lower() for w in caught)

    def test_unknown_provider_falls_back_to_mock_with_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_PROVIDER", "cohere")  # unsupported
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            c = get_client()
        assert isinstance(c, MockClient)
        # Misconfiguration must be visible in logs, not silently degraded.
        assert any("cohere" in str(w.message).lower() for w in caught)
        assert any("not recognized" in str(w.message).lower() for w in caught)

    def test_explicit_mock_provider_is_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `LLM_PROVIDER=mock` is intentional — the user knows. No warning.
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            c = get_client()
        assert isinstance(c, MockClient)
        assert not any("not recognized" in str(w.message).lower() for w in caught)
