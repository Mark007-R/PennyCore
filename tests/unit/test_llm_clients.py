"""Day 33 — Phase 7 test-sweep coverage backfill for the LLM provider wrappers.

The dispatch happy-path (where a real provider is actually selected and the
adapter is constructed) wasn't reached by the existing dispatch tests in
`test_llm_dispatch.py` — those only exercise the placeholder-fallback branches
because that's all that's safe to run offline. The 0% coverage on
`anthropic_client.py`, `azure_client.py`, `openai_client.py` and the 69% on
`llm/__init__.py` came from that gap.

This module closes the gap by injecting fake `anthropic` / `openai` SDK modules
into `sys.modules` before construction, then exercising `complete()` +
`summarize()` against the fake client. Same wire shape as the real SDK, zero
network.
"""

from __future__ import annotations

import sys
import types
import warnings
from collections.abc import Iterator
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Env isolation — same shape as test_llm_dispatch.py so each test starts from
# a known-blank baseline regardless of what the shell or .env file injected.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
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
# Fake-SDK helpers — minimal stand-ins for the real `anthropic` and `openai`
# packages. They record every call so tests can assert on the request shape.
# ---------------------------------------------------------------------------


class _FakeAnthropicBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAnthropicNonTextBlock:
    """Anthropic's content list can include image / tool_use blocks without a
    `.text` attribute — the adapter must skip those silently."""


class _FakeAnthropicMessage:
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class _FakeAnthropicMessages:
    def __init__(self, outer: "_FakeAnthropic") -> None:
        self._outer = outer

    def create(self, **kwargs: Any) -> _FakeAnthropicMessage:
        self._outer.calls.append(kwargs)
        return self._outer.next_response


class _FakeAnthropic:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.messages = _FakeAnthropicMessages(self)
        self.calls: list[dict[str, Any]] = []
        self.next_response: _FakeAnthropicMessage = _FakeAnthropicMessage(
            [_FakeAnthropicBlock("hello from fake claude")]
        )


def _install_fake_anthropic(monkeypatch: pytest.MonkeyPatch) -> _FakeAnthropic:
    """Insert a fake `anthropic` module into sys.modules and return the
    captured client instance once the adapter constructs it."""
    captured: dict[str, _FakeAnthropic] = {}

    fake_module = types.ModuleType("anthropic")

    def _ctor(*, api_key: str) -> _FakeAnthropic:
        client = _FakeAnthropic(api_key=api_key)
        captured["client"] = client
        return client

    fake_module.Anthropic = _ctor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    return captured  # type: ignore[return-value]


# Fake openai (covers both the standard and the Foundry / Azure OpenAI paths
# — the adapter for both uses `openai.OpenAI` with different kwargs).


class _FakeOpenAIChatMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeOpenAIChatChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeOpenAIChatMessage(content)


class _FakeOpenAIChatResponse:
    def __init__(self, content: str | None) -> None:
        self.choices = [_FakeOpenAIChatChoice(content)]


class _FakeOpenAIChatCompletions:
    def __init__(self, outer: "_FakeOpenAI") -> None:
        self._outer = outer

    def create(self, **kwargs: Any) -> _FakeOpenAIChatResponse:
        self._outer.calls.append(kwargs)
        return self._outer.next_response


class _FakeOpenAIChat:
    def __init__(self, outer: "_FakeOpenAI") -> None:
        self.completions = _FakeOpenAIChatCompletions(outer)


class _FakeOpenAI:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        default_query: dict[str, str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.default_query = default_query
        self.chat = _FakeOpenAIChat(self)
        self.calls: list[dict[str, Any]] = []
        self.next_response = _FakeOpenAIChatResponse("hello from fake openai")


def _install_fake_openai(monkeypatch: pytest.MonkeyPatch) -> dict[str, _FakeOpenAI]:
    captured: dict[str, _FakeOpenAI] = {}

    fake_module = types.ModuleType("openai")

    def _ctor(**kwargs: Any) -> _FakeOpenAI:
        client = _FakeOpenAI(**kwargs)
        captured["client"] = client
        return client

    fake_module.OpenAI = _ctor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_module)
    return captured


# ---------------------------------------------------------------------------
# AnthropicClient — direct construction + complete/summarize shape.
# ---------------------------------------------------------------------------


class TestAnthropicClientDirect:
    def test_complete_passes_request_shape_and_returns_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_fake_anthropic(monkeypatch)
        from context_engine.llm.anthropic_client import AnthropicClient

        client = AnthropicClient(api_key="sk-ant-fake", model="claude-sonnet-4-6")
        out = client.complete(
            "hello", system="you are a test", max_tokens=128, temperature=0.0
        )

        assert out == "hello from fake claude"
        # SDK was constructed with the key we gave it.
        assert captured["client"].api_key == "sk-ant-fake"
        # The call wraps the prompt into Anthropic's Messages shape — system
        # is a top-level kwarg, not a message.
        call = captured["client"].calls[0]
        assert call["model"] == "claude-sonnet-4-6"
        assert call["max_tokens"] == 128
        assert call["temperature"] == 0.0
        assert call["system"] == "you are a test"
        assert call["messages"] == [{"role": "user", "content": "hello"}]

    def test_complete_omits_system_when_not_provided(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_fake_anthropic(monkeypatch)
        from context_engine.llm.anthropic_client import AnthropicClient

        client = AnthropicClient(api_key="sk-ant-fake")
        client.complete("hi")
        call = captured["client"].calls[0]
        assert "system" not in call

    def test_complete_concatenates_text_blocks_and_skips_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_fake_anthropic(monkeypatch)
        from context_engine.llm.anthropic_client import AnthropicClient

        client = AnthropicClient(api_key="sk-ant-fake")
        # Mix text blocks with a non-text block (e.g. a tool_use block).
        captured["client"].next_response = _FakeAnthropicMessage(
            [
                _FakeAnthropicBlock("part 1 "),
                _FakeAnthropicNonTextBlock(),
                _FakeAnthropicBlock("part 2"),
            ]
        )
        assert client.complete("anything") == "part 1 part 2"

    def test_summarize_uses_summariser_system_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_fake_anthropic(monkeypatch)
        from context_engine.llm.anthropic_client import AnthropicClient

        client = AnthropicClient(api_key="sk-ant-fake")
        client.summarize("customer asked about W-2", max_tokens=64)

        call = captured["client"].calls[0]
        assert "summariser" in call["system"].lower() or "summarizer" in call["system"].lower()
        assert "Never invent facts" in call["system"]
        assert "customer asked about W-2" in call["messages"][0]["content"]
        assert call["max_tokens"] == 64
        assert call["temperature"] == 0.0


# ---------------------------------------------------------------------------
# OpenAIClient — direct construction + complete/summarize shape.
# ---------------------------------------------------------------------------


class TestOpenAIClientDirect:
    def test_complete_builds_chat_messages_with_system(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_fake_openai(monkeypatch)
        from context_engine.llm.openai_client import OpenAIClient

        client = OpenAIClient(api_key="sk-fake", model="gpt-4o-mini")
        out = client.complete(
            "hello", system="you are a test", max_tokens=128, temperature=0.0
        )

        assert out == "hello from fake openai"
        assert captured["client"].api_key == "sk-fake"
        # Standard OpenAI path: no base_url override.
        assert captured["client"].base_url is None
        assert captured["client"].default_query is None

        call = captured["client"].calls[0]
        assert call["model"] == "gpt-4o-mini"
        assert call["max_tokens"] == 128
        assert call["messages"] == [
            {"role": "system", "content": "you are a test"},
            {"role": "user", "content": "hello"},
        ]

    def test_complete_without_system_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_fake_openai(monkeypatch)
        from context_engine.llm.openai_client import OpenAIClient

        client = OpenAIClient(api_key="sk-fake")
        client.complete("hi")
        call = captured["client"].calls[0]
        assert call["messages"] == [{"role": "user", "content": "hi"}]

    def test_complete_handles_none_response_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The OpenAI SDK can return `message.content = None` for tool-only
        # responses; the adapter must coerce that to "" rather than leak the
        # None type to downstream consumers.
        captured = _install_fake_openai(monkeypatch)
        from context_engine.llm.openai_client import OpenAIClient

        client = OpenAIClient(api_key="sk-fake")
        captured["client"].next_response = _FakeOpenAIChatResponse(None)
        assert client.complete("hi") == ""

    def test_summarize_uses_summariser_system_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_fake_openai(monkeypatch)
        from context_engine.llm.openai_client import OpenAIClient

        client = OpenAIClient(api_key="sk-fake")
        client.summarize("customer asked about W-2", max_tokens=64)

        call = captured["client"].calls[0]
        assert call["messages"][0]["role"] == "system"
        assert "Never invent facts" in call["messages"][0]["content"]
        assert "customer asked about W-2" in call["messages"][1]["content"]


# ---------------------------------------------------------------------------
# AzureClient — Foundry endpoint shape (base_url + default_query injection).
# ---------------------------------------------------------------------------


class TestAzureClientDirect:
    def test_constructor_wires_foundry_endpoint_and_api_version(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_fake_openai(monkeypatch)
        from context_engine.llm.azure_client import AzureClient

        AzureClient(
            api_key="azure-fake",
            endpoint="https://acme.services.ai.azure.com/models/",
            api_version="2024-05-01-preview",
            model="Kimi-K2.6",
        )

        client = captured["client"]
        assert client.api_key == "azure-fake"
        # The trailing slash in the endpoint MUST be stripped — the Foundry
        # router doesn't tolerate a double slash before /chat/completions.
        assert client.base_url == "https://acme.services.ai.azure.com/models"
        # api_version is injected on every request via default_query — this is
        # how both Foundry Models and classic Azure OpenAI expose the version.
        assert client.default_query == {"api-version": "2024-05-01-preview"}

    def test_complete_includes_system_and_returns_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_fake_openai(monkeypatch)
        from context_engine.llm.azure_client import AzureClient

        client = AzureClient(
            api_key="azure-fake",
            endpoint="https://acme.services.ai.azure.com/models",
            api_version="2024-05-01-preview",
            model="Kimi-K2.6",
        )
        out = client.complete(
            "hello", system="you are a test", max_tokens=200, temperature=0.0
        )

        assert out == "hello from fake openai"
        call = captured["client"].calls[0]
        assert call["model"] == "Kimi-K2.6"
        assert call["max_tokens"] == 200
        assert call["messages"][0]["role"] == "system"
        assert call["messages"][1]["role"] == "user"

    def test_summarize_uses_summariser_system_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured = _install_fake_openai(monkeypatch)
        from context_engine.llm.azure_client import AzureClient

        client = AzureClient(
            api_key="azure-fake",
            endpoint="https://acme.services.ai.azure.com/models",
            api_version="2024-05-01-preview",
            model="Kimi-K2.6",
        )
        client.summarize("history excerpt", max_tokens=128)
        call = captured["client"].calls[0]
        assert "Never invent facts" in call["messages"][0]["content"]
        assert "history excerpt" in call["messages"][1]["content"]


# ---------------------------------------------------------------------------
# get_client() dispatch happy-paths — exercises the previously-uncovered
# real-provider construction branches in context_engine/llm/__init__.py.
# ---------------------------------------------------------------------------


class TestGetClientHappyPaths:
    def test_anthropic_provider_with_real_key_returns_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_anthropic(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-looking-key")
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

        from context_engine.llm import get_client
        from context_engine.llm.anthropic_client import AnthropicClient

        client = get_client()
        assert isinstance(client, AnthropicClient)
        assert client.name == "anthropic"
        assert client.model == "claude-haiku-4-5-20251001"

    def test_anthropic_sdk_missing_falls_back_to_mock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No fake `anthropic` installed AND the real one isn't there either
        # (PennyCore's requirements pin it optional). Force its absence so
        # the lazy import in the adapter raises.
        monkeypatch.setitem(sys.modules, "anthropic", None)
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-looking-key")

        from context_engine.llm import MockClient, get_client

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            client = get_client()
        assert isinstance(client, MockClient)
        assert any("anthropic" in str(w.message).lower() for w in caught)

    def test_azure_provider_with_foundry_vars_returns_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_openai(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        monkeypatch.setenv("AZURE_API_KEY", "azure-real-key")
        monkeypatch.setenv("AZURE_ENDPOINT", "https://acme.services.ai.azure.com/models")
        monkeypatch.setenv("AZURE_API_VERSION", "2024-05-01-preview")
        monkeypatch.setenv("LLM_MODEL", "Kimi-K2.6")

        from context_engine.llm import get_client
        from context_engine.llm.azure_client import AzureClient

        client = get_client()
        assert isinstance(client, AzureClient)
        assert client.model == "Kimi-K2.6"

    def test_azure_provider_falls_back_to_classic_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the Foundry vars aren't set, the dispatcher must pick up the
        classic Azure OpenAI Service vars instead."""
        _install_fake_openai(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        # No Foundry vars — only the legacy AZURE_OPENAI_* set.
        monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-legacy-key")
        monkeypatch.setenv(
            "AZURE_OPENAI_ENDPOINT", "https://acme.openai.azure.com/"
        )
        monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
        monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini-deploy")

        from context_engine.llm import get_client
        from context_engine.llm.azure_client import AzureClient

        client = get_client()
        assert isinstance(client, AzureClient)
        assert client.model == "gpt-4o-mini-deploy"

    def test_azure_sdk_missing_falls_back_to_mock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "openai", None)
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        monkeypatch.setenv("AZURE_API_KEY", "azure-real-key")
        monkeypatch.setenv("AZURE_ENDPOINT", "https://acme.services.ai.azure.com/models")
        monkeypatch.setenv("LLM_MODEL", "Kimi-K2.6")

        from context_engine.llm import MockClient, get_client

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            client = get_client()
        assert isinstance(client, MockClient)
        assert any("azure" in str(w.message).lower() for w in caught)

    def test_openai_provider_with_real_key_returns_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_fake_openai(monkeypatch)
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-real-looking")
        monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")

        from context_engine.llm import get_client
        from context_engine.llm.openai_client import OpenAIClient

        client = get_client()
        assert isinstance(client, OpenAIClient)
        assert client.model == "gpt-4o"

    def test_openai_provider_without_no_key_falls_back_to_mock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No OPENAI_API_KEY at all — covers the placeholder-detector branch
        # for the standard OpenAI path (separate from the Azure path).
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        from context_engine.llm import MockClient, get_client

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            client = get_client()
        assert isinstance(client, MockClient)
        assert any("openai" in str(w.message).lower() for w in caught)

    def test_openai_sdk_missing_falls_back_to_mock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "openai", None)
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-real-looking")

        from context_engine.llm import MockClient, get_client

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            client = get_client()
        assert isinstance(client, MockClient)
        assert any("openai" in str(w.message).lower() for w in caught)
