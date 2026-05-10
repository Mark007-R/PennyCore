"""Day 7 — takehome `MemorySystem` adapter unit tests.

Mirrors the five non-LLM scenarios in `takehome/context-engine/evaluate.py`
so we have pytest coverage of the adapter independently of the
external evaluator. The evaluator itself runs in a subprocess (script
mode); this suite exercises the same behavioral expectations from
inside pytest.

Why duplicate? Two reasons:
  1. The pytest path runs in CI (whenever we wire up local CI) and
     reports per-scenario pass/fail with rich diffs. The evaluator
     just prints PASS/FAIL.
  2. Refactors to brief assembly that quietly break the takehome get
     caught by `pytest -x` rather than only by `python evaluate.py`.

The adapter module lives under a directory with a hyphen
(`takehome/context-engine/memory_system.py`) — Python's normal import
machinery can't reach it. We `importlib.util.spec_from_file_location`
to load it by path.
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ADAPTER_PATH = os.path.join(
    REPO_ROOT, "takehome", "context-engine", "memory_system.py"
)


def _load_adapter_module():
    spec = importlib.util.spec_from_file_location(
        "takehome_memory_system", ADAPTER_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def adapter_module():
    return _load_adapter_module()


@pytest.fixture
def system(adapter_module):
    return adapter_module.PennyCoreMemorySystem()


# ---------------------------------------------------------------------------
# Local message + action shapes — mirror the evaluator's frozen dataclasses
# without coupling the test file to evaluate.py's import path.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Msg:
    message_id: str
    borrower_id: str
    channel: str
    sender: str
    content: str
    timestamp: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Act:
    action_id: str
    borrower_id: str
    action_type: str
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _estimate_tokens(text: str) -> int:
    """Mirrors the evaluator's estimator — see evaluate.py."""
    return len(text) // 4


# ---------------------------------------------------------------------------
# Scenario 1 — basic ingest + retrieve
# ---------------------------------------------------------------------------


class TestScenario1BasicIngestRetrieve:
    def test_key_facts_present_in_context(self, system) -> None:
        now = datetime.now(timezone.utc)
        for msg in [
            _Msg(
                "m1", "b1", "chat", "borrower",
                "Hi, I'm looking to refinance my home. Current rate is 6.5%.",
                now - timedelta(minutes=10),
            ),
            _Msg(
                "m2", "b1", "chat", "agent",
                "I'd be happy to help. What's your current loan balance?",
                now - timedelta(minutes=9),
            ),
            _Msg(
                "m3", "b1", "chat", "borrower",
                "About $350,000. Home was appraised at $500,000 last year.",
                now - timedelta(minutes=8),
            ),
        ]:
            system.ingest_message(msg)

        ctx = system.assemble_context("b1", token_budget=2000)

        assert "refinance" in ctx.lower()
        assert "350" in ctx
        assert "6.5" in ctx
        assert "500" in ctx


# ---------------------------------------------------------------------------
# Scenario 2 — token budget honored
# ---------------------------------------------------------------------------


class TestScenario2TokenBudget:
    def test_tight_budget_yields_nonempty_brief_under_estimate(self, system) -> None:
        now = datetime.now(timezone.utc)
        for i in range(50):
            content = (
                f"Message {i}: detailed discussion about mortgage rates, "
                f"loan terms, property valuations, insurance requirements, "
                f"and various documentation needs for the refinance "
                f"application. Closing costs and timeline questions."
            )
            system.ingest_message(_Msg(
                f"msg-{i}", "b2", "chat",
                "borrower" if i % 2 == 0 else "agent",
                content,
                now - timedelta(minutes=50 - i),
            ))

        budget = 200
        ctx = system.assemble_context("b2", token_budget=budget)

        assert ctx.strip(), "tight-budget context should not be empty"
        # Evaluator allows 10% tolerance.
        assert _estimate_tokens(ctx) <= budget * 1.1


# ---------------------------------------------------------------------------
# Scenario 3 — cross-channel continuity
# ---------------------------------------------------------------------------


class TestScenario3CrossChannel:
    def test_unified_view_across_chat_email_sms(self, system) -> None:
        now = datetime.now(timezone.utc)
        msgs = [
            _Msg(
                "x1", "b3", "chat", "borrower",
                "I need pre-approval for a mortgage. Budget around $400K.",
                now - timedelta(hours=48),
            ),
            _Msg(
                "x2", "b3", "chat", "agent",
                "I can help. I'll need your income docs.",
                now - timedelta(hours=47),
            ),
            _Msg(
                "x3", "b3", "email", "borrower",
                "Attached are my W-2s and two months of bank statements as discussed.",
                now - timedelta(hours=24),
                metadata={
                    "subject": "Documents for pre-approval",
                    "attachments": ["w2_2024.pdf", "bank_jan.pdf"],
                },
            ),
            _Msg(
                "x4", "b3", "email", "agent",
                "Thank you. I see your W-2 shows $95,000 annual income.",
                now - timedelta(hours=23),
            ),
            _Msg(
                "x5", "b3", "sms", "borrower",
                "Hey, any update on my pre-approval?",
                now - timedelta(hours=2),
            ),
        ]
        for m in msgs:
            system.ingest_message(m)

        ctx = system.assemble_context("b3", token_budget=2000)
        low = ctx.lower()

        assert "pre-approv" in low or "preapprov" in low
        assert "400" in ctx
        assert "w-2" in low or "w2" in low or "bank statement" in low
        assert "95" in ctx
        # Channel provenance markers must be present (rubric C2).
        assert "[chat]" in low
        assert "[email]" in low
        assert "[sms]" in low


# ---------------------------------------------------------------------------
# Scenario 4 — action memory
# ---------------------------------------------------------------------------


class TestScenario4ActionMemory:
    def test_actions_retrievable_and_in_context(self, system) -> None:
        now = datetime.now(timezone.utc)
        system.ingest_message(_Msg(
            "a1", "b4", "chat", "borrower",
            "What documents do I need?",
            now - timedelta(hours=1),
        ))
        system.record_action("b4", _Act(
            action_id="act-1",
            borrower_id="b4",
            action_type="send_checklist",
            details={"items": ["pay_stubs", "w2"]},
            timestamp=now - timedelta(minutes=58),
        ))
        system.record_action("b4", _Act(
            action_id="act-2",
            borrower_id="b4",
            action_type="request_document",
            details={"document_type": "w2"},
            timestamp=now - timedelta(minutes=30),
        ))

        actions = system.get_actions("b4")
        types = [a.action_type for a in actions]
        assert "send_checklist" in types
        assert "request_document" in types

        ctx = system.assemble_context("b4", token_budget=2000).lower()
        # The evaluator looks for any of these keywords.
        assert any(
            kw in ctx
            for kw in ["checklist", "already", "sent", "requested", "action", "w2"]
        )


# ---------------------------------------------------------------------------
# Scenario 5 — recency under tight budget
# ---------------------------------------------------------------------------


class TestScenario5Recency:
    def test_recent_messages_survive_tight_budget(self, system) -> None:
        now = datetime.now(timezone.utc)
        # 20 old messages
        for i in range(20):
            system.ingest_message(_Msg(
                f"old-{i}", "b5", "chat", "borrower",
                f"Old message {i}: discussing initial property search.",
                now - timedelta(days=5, minutes=20 - i),
            ))
        # Two recent messages with a unique keyword
        system.ingest_message(_Msg(
            "r1", "b5", "chat", "borrower",
            "The appraiser valued the home at $380K but comps show $420K. "
            "CURRENT_APPRAISAL_DISPUTE.",
            now - timedelta(minutes=5),
        ))
        system.ingest_message(_Msg(
            "r2", "b5", "chat", "agent",
            "I understand your concern about the appraisal.",
            now - timedelta(minutes=4),
        ))

        ctx = system.assemble_context("b5", token_budget=300)
        low = ctx.lower()

        assert (
            "current_appraisal_dispute" in low
            or "apprais" in low
            or "380" in ctx
            or "420" in ctx
        )


# ---------------------------------------------------------------------------
# Edge cases & invariants
# ---------------------------------------------------------------------------


class TestAdapterInvariants:
    def test_no_argument_constructor(self, adapter_module) -> None:
        """Evaluator does `system_cls()` per scenario — must not raise."""
        adapter_module.PennyCoreMemorySystem()

    def test_fresh_instances_are_isolated(self, adapter_module) -> None:
        """Two instances share no state — required by evaluator's
        per-scenario fresh-instance pattern."""
        a = adapter_module.PennyCoreMemorySystem()
        b = adapter_module.PennyCoreMemorySystem()
        a.ingest_message(_Msg("x", "b", "chat", "borrower", "hi", datetime.now(timezone.utc)))
        assert a.assemble_context("b", token_budget=500).strip()
        assert b.assemble_context("b", token_budget=500).strip() in ("", "Borrower: b")

    def test_unknown_borrower_returns_header_only_or_empty(self, system) -> None:
        ctx = system.assemble_context("ghost", token_budget=500)
        # Header is allowed; no message bodies.
        assert "[chat]" not in ctx
        assert "[action]" not in ctx

    def test_get_actions_unknown_borrower_returns_empty(self, system) -> None:
        assert system.get_actions("ghost") == []

    def test_factory_function_returns_instance(self, adapter_module) -> None:
        instance = adapter_module.create_memory_system()
        assert isinstance(instance, adapter_module.PennyCoreMemorySystem)

    def test_token_budget_invariant_under_evaluator_estimator(self, system) -> None:
        now = datetime.now(timezone.utc)
        for i in range(100):
            system.ingest_message(_Msg(
                f"m{i}", "b9", "chat", "borrower",
                "Lorem ipsum " * (5 + i % 7),
                now - timedelta(minutes=100 - i),
            ))
        for budget in [50, 100, 250, 500, 1500, 5000]:
            ctx = system.assemble_context("b9", token_budget=budget)
            assert _estimate_tokens(ctx) <= budget, (
                f"budget {budget}: produced {_estimate_tokens(ctx)} tokens"
            )
