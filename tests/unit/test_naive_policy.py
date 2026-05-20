"""Tests for the Day-17 NaivePolicyEngine.

Behavioural coverage:
  * Mock-mode follows the event-type heuristic (tenant-agnostic) —
    this is the documented failure mode the benchmark exposes.
  * Real-LLM path parses keywords from free-form output.
  * Metrics counters (llm_call_count + token estimates) increment
    per call and reset cleanly.
  * Tenant isolation: setting tenant A's policy never changes
    tenant B's decision path.
"""

from __future__ import annotations

import pytest

from context_engine.llm.mock import MockClient
from contracts.policies import PolicyDecision
from orchestrator.policy import NaivePolicyEngine


@pytest.fixture
def mock_engine() -> NaivePolicyEngine:
    engine = NaivePolicyEngine(client=MockClient())
    engine.set_policies(
        "tenant-a",
        {
            "send_borrower_message": "auto",
            "request_document": "approval_required",
            "default": "approval_required",
        },
    )
    engine.set_policies(
        "tenant-b",
        {
            "send_borrower_message": "approval_required",
            "default": "approval_required",
        },
    )
    return engine


def test_mock_mode_uses_event_type_heuristic(
    mock_engine: NaivePolicyEngine,
) -> None:
    """Mock-mode ignores the tenant policy table — the documented
    naive-LLM failure mode the benchmark surfaces."""
    decision = mock_engine.decide(
        "tenant-a",
        "send_borrower_message",
        event_context={"event_type": "message_received"},
    )
    # Heuristic says message_received → auto regardless of tenant.
    assert decision is PolicyDecision.AUTO


def test_mock_mode_tenant_agnostic_failure(
    mock_engine: NaivePolicyEngine,
) -> None:
    """tenant-b's policy says approval_required for
    send_borrower_message, but naive mock returns AUTO because the
    heuristic only looks at event_type. This IS the bug the
    benchmark measures — assert it stays."""
    decision = mock_engine.decide(
        "tenant-b",
        "send_borrower_message",
        event_context={"event_type": "message_received"},
    )
    assert decision is PolicyDecision.AUTO


def test_mock_mode_default_safe_for_unknown_event_type(
    mock_engine: NaivePolicyEngine,
) -> None:
    decision = mock_engine.decide(
        "tenant-a",
        "send_borrower_message",
        event_context={"event_type": "unknown_event_type"},
    )
    assert decision is PolicyDecision.APPROVAL_REQUIRED


def test_metrics_increment(mock_engine: NaivePolicyEngine) -> None:
    assert mock_engine.llm_call_count == 0
    mock_engine.decide(
        "tenant-a",
        "send_borrower_message",
        event_context={"event_type": "message_received"},
    )
    assert mock_engine.llm_call_count == 1
    assert mock_engine.llm_input_tokens_estimate > 0
    assert mock_engine.llm_output_tokens_estimate > 0


def test_reset_metrics(mock_engine: NaivePolicyEngine) -> None:
    mock_engine.decide(
        "tenant-a",
        "send_borrower_message",
        event_context={"event_type": "message_received"},
    )
    assert mock_engine.llm_call_count == 1
    mock_engine.reset_metrics()
    assert mock_engine.llm_call_count == 0
    assert mock_engine.llm_input_tokens_estimate == 0
    assert mock_engine.llm_output_tokens_estimate == 0


def test_unknown_tenant_safe_default(mock_engine: NaivePolicyEngine) -> None:
    decision = mock_engine.decide(
        "tenant-ghost",
        "send_borrower_message",
        event_context={"event_type": "anomaly_detected"},
    )
    # Heuristic for anomaly_detected = approval_required regardless.
    assert decision is PolicyDecision.APPROVAL_REQUIRED


# ---------------------------------------------------------------------------
# Real-LLM path parser tests — exercised with a stub client that
# returns canned free-form text so we don't need a live LLM.
# ---------------------------------------------------------------------------


class _StubClient:
    """LLM-shaped stub whose ``name`` is NOT "mock" so the parser
    treats responses as real free-form output."""

    name = "stub-real"
    model = "stub"

    def __init__(self, response: str) -> None:
        self._response = response

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        del prompt, system, max_tokens, temperature
        return self._response

    def summarize(self, text: str, *, max_tokens: int = 256) -> str:
        del text, max_tokens
        return ""


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("auto — message is courtesy", PolicyDecision.AUTO),
        ("approval_required because amount is large", PolicyDecision.APPROVAL_REQUIRED),
        ("approval required for compliance", PolicyDecision.APPROVAL_REQUIRED),
        ("reject — out of scope", PolicyDecision.REJECT),
        ("This is unparseable text without keywords.", PolicyDecision.APPROVAL_REQUIRED),
        ("", PolicyDecision.APPROVAL_REQUIRED),
    ],
)
def test_real_llm_keyword_parser(
    response: str, expected: PolicyDecision
) -> None:
    engine = NaivePolicyEngine(client=_StubClient(response))
    engine.set_policies("tenant-a", {"send_borrower_message": "auto"})
    decision = engine.decide(
        "tenant-a",
        "send_borrower_message",
        event_context={"event_type": "message_received"},
    )
    assert decision is expected


def test_real_llm_failure_falls_back_to_safe_default() -> None:
    class _Boom:
        name = "boom"
        model = "boom"

        def complete(self, *a, **kw):
            raise RuntimeError("network is on fire")

        def summarize(self, *a, **kw):
            return ""

    engine = NaivePolicyEngine(client=_Boom())
    engine.set_policies("tenant-a", {"send_borrower_message": "auto"})
    decision = engine.decide(
        "tenant-a",
        "send_borrower_message",
        event_context={"event_type": "message_received"},
    )
    assert decision is PolicyDecision.APPROVAL_REQUIRED
