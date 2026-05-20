"""Tests for the Day-17 LLMJudgePolicyEngine.

Behavioural coverage:
  * Mock-mode mirrors the declarative engine exactly (the documented
    "perfectly-prompt-following LLM" emulation).
  * Real-LLM path parses strict JSON; malformed responses fall back
    to the safe default.
  * Tenant isolation: same policy table semantics as declarative.
  * Metrics counters increment per call and reset.
"""

from __future__ import annotations

import pytest

from context_engine.llm.mock import MockClient
from contracts.policies import PolicyDecision
from orchestrator.policy import DeclarativePolicyEngine, LLMJudgePolicyEngine


@pytest.fixture
def mock_judge() -> LLMJudgePolicyEngine:
    engine = LLMJudgePolicyEngine(client=MockClient())
    engine.set_policies(
        "tenant-a",
        {
            "send_borrower_message": "auto",
            "request_document": "approval_required",
            "schedule_call": "reject",
            "default": "approval_required",
        },
    )
    return engine


def test_mock_judge_mirrors_declarative(
    mock_judge: LLMJudgePolicyEngine,
) -> None:
    """Mock-mode judge produces the same decisions as the declarative
    engine on the same policy table. Same correctness → cost differs."""
    declarative = DeclarativePolicyEngine()
    declarative.set_policies(
        "tenant-a",
        {
            "send_borrower_message": "auto",
            "request_document": "approval_required",
            "schedule_call": "reject",
            "default": "approval_required",
        },
    )
    for action in (
        "send_borrower_message",
        "request_document",
        "schedule_call",
        "notify_loan_officer",
    ):
        assert mock_judge.decide("tenant-a", action) is declarative.decide(
            "tenant-a", action
        ), f"divergence on {action}"


def test_mock_judge_unknown_tenant(
    mock_judge: LLMJudgePolicyEngine,
) -> None:
    assert (
        mock_judge.decide("tenant-ghost", "send_borrower_message")
        is PolicyDecision.APPROVAL_REQUIRED
    )


def test_metrics_increment(mock_judge: LLMJudgePolicyEngine) -> None:
    assert mock_judge.llm_call_count == 0
    mock_judge.decide("tenant-a", "send_borrower_message")
    assert mock_judge.llm_call_count == 1
    assert mock_judge.llm_input_tokens_estimate > 0
    assert mock_judge.llm_output_tokens_estimate > 0


def test_reset_metrics(mock_judge: LLMJudgePolicyEngine) -> None:
    mock_judge.decide("tenant-a", "send_borrower_message")
    mock_judge.reset_metrics()
    assert mock_judge.llm_call_count == 0
    assert mock_judge.llm_input_tokens_estimate == 0
    assert mock_judge.llm_output_tokens_estimate == 0


def test_tenant_isolation() -> None:
    engine = LLMJudgePolicyEngine(client=MockClient())
    engine.set_policies("tenant-a", {"send_borrower_message": "auto"})
    engine.set_policies(
        "tenant-b", {"send_borrower_message": "approval_required"}
    )
    assert (
        engine.decide("tenant-a", "send_borrower_message")
        is PolicyDecision.AUTO
    )
    assert (
        engine.decide("tenant-b", "send_borrower_message")
        is PolicyDecision.APPROVAL_REQUIRED
    )


# ---------------------------------------------------------------------------
# Real-LLM path: strict-JSON parser + safe-default fallback.
# ---------------------------------------------------------------------------


class _StubClient:
    name = "stub-real"
    model = "stub"

    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, prompt: str, **kw) -> str:
        del prompt, kw
        return self._response

    def summarize(self, text: str, **kw) -> str:
        del text, kw
        return ""


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ('{"decision": "auto", "reasoning": "ok"}', PolicyDecision.AUTO),
        (
            '{"decision": "approval_required", "reasoning": "..."}',
            PolicyDecision.APPROVAL_REQUIRED,
        ),
        ('{"decision": "reject"}', PolicyDecision.REJECT),
        # Fenced output — parser tolerates a single ```json fence.
        (
            '```json\n{"decision": "auto"}\n```',
            PolicyDecision.AUTO,
        ),
        # Garbage → safe default.
        ("not even json", PolicyDecision.APPROVAL_REQUIRED),
        ('{"decision": "blast_off"}', PolicyDecision.APPROVAL_REQUIRED),
        ("", PolicyDecision.APPROVAL_REQUIRED),
        # JSON with missing decision key → safe default.
        ('{"reasoning": "no decision key"}', PolicyDecision.APPROVAL_REQUIRED),
    ],
)
def test_real_llm_strict_json_parser(
    response: str, expected: PolicyDecision
) -> None:
    engine = LLMJudgePolicyEngine(client=_StubClient(response))
    engine.set_policies("tenant-a", {"send_borrower_message": "auto"})
    assert engine.decide("tenant-a", "send_borrower_message") is expected


def test_real_llm_failure_falls_back() -> None:
    class _Boom:
        name = "boom"
        model = "boom"

        def complete(self, *a, **kw):
            raise RuntimeError("LLM offline")

        def summarize(self, *a, **kw):
            return ""

    engine = LLMJudgePolicyEngine(client=_Boom())
    engine.set_policies("tenant-a", {"send_borrower_message": "auto"})
    assert (
        engine.decide("tenant-a", "send_borrower_message")
        is PolicyDecision.APPROVAL_REQUIRED
    )
