#!/usr/bin/env python3
"""
AI Agent Context & Memory System — Evaluator

Usage:
    python evaluate.py

This script defines the MemorySystem protocol and data types, imports your
implementation from memory_system.py, and runs behavioral scenarios.

Do not modify this file.
"""

import importlib
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Data types — your system must accept these
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Message:
    """A single message in a conversation."""

    message_id: str
    borrower_id: str
    channel: str  # "chat", "email", "sms", "voice"
    sender: str  # "borrower" or "agent"
    content: str
    timestamp: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Action:
    """An action the assistant has taken."""

    action_id: str
    borrower_id: str
    action_type: str  # e.g. "send_checklist", "request_document", "schedule_call"
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Protocol — your system must implement this
# ---------------------------------------------------------------------------


@runtime_checkable
class MemorySystem(Protocol):
    def ingest_message(self, message: Message) -> None:
        """Store a message. May be called with messages from any channel."""
        ...

    def assemble_context(self, borrower_id: str, token_budget: int) -> str:
        """
        Build a context string for an LLM call about this borrower.

        The returned string MUST fit within `token_budget` tokens.
        A reasonable approximation: 1 token ≈ 4 characters.
        You may use a more precise method (e.g. tiktoken).
        """
        ...

    def record_action(self, borrower_id: str, action: Action) -> None:
        """Record an action the assistant has taken for this borrower."""
        ...

    def get_actions(self, borrower_id: str) -> list[Action]:
        """Return all actions recorded for this borrower."""
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: 1 token ≈ 4 characters."""
    return len(text) // 4


# ---------------------------------------------------------------------------
# Scenario infrastructure
# ---------------------------------------------------------------------------


@dataclass
class ScenarioResult:
    name: str
    passed: bool
    detail: str
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def scenario_1_basic_ingest_and_retrieve(system: MemorySystem) -> ScenarioResult:
    """Basic ingest and context assembly"""
    now = datetime.now(timezone.utc)

    messages = [
        Message(
            "m1", "borrower-001", "chat", "borrower",
            "Hi, I'm looking to refinance my home. Current rate is 6.5%.",
            now - timedelta(minutes=10),
        ),
        Message(
            "m2", "borrower-001", "chat", "agent",
            "I'd be happy to help with your refinance. What's your current loan balance?",
            now - timedelta(minutes=9),
        ),
        Message(
            "m3", "borrower-001", "chat", "borrower",
            "About $350,000. Home was appraised at $500,000 last year.",
            now - timedelta(minutes=8),
        ),
    ]

    for msg in messages:
        system.ingest_message(msg)

    context = system.assemble_context("borrower-001", token_budget=2000)

    checks = {
        "refinance": "refinance" in context.lower(),
        "loan_amount": "350" in context,
        "rate": "6.5" in context,
        "home_value": "500" in context,
    }

    failed = [k for k, v in checks.items() if not v]
    if failed:
        return ScenarioResult(
            "1. Basic ingest and context assembly", False,
            f"Context missing key information: {', '.join(failed)}",
        )

    return ScenarioResult(
        "1. Basic ingest and context assembly", True,
        "Key borrower facts present in context",
    )


def scenario_2_token_budget(system: MemorySystem) -> ScenarioResult:
    """Token budget is respected"""
    now = datetime.now(timezone.utc)

    # Ingest 50 lengthy messages
    for i in range(50):
        content = (
            f"Message {i}: This is a detailed discussion about mortgage rates, "
            f"loan terms, property valuations, insurance requirements, and "
            f"various documentation needs for the refinance application. "
            f"The borrower has questions about points, closing costs, "
            f"and the timeline for the entire process."
        )
        system.ingest_message(Message(
            f"budget-{i}", "borrower-budget", "chat",
            "borrower" if i % 2 == 0 else "agent",
            content,
            now - timedelta(minutes=50 - i),
        ))

    # Tight budget
    small_budget = 200  # ~800 characters
    context = system.assemble_context("borrower-budget", token_budget=small_budget)
    estimated = _estimate_tokens(context)

    if len(context.strip()) == 0:
        return ScenarioResult(
            "2. Token budget is respected", False,
            "Context is empty — should include something even with a tight budget",
        )

    if estimated > small_budget * 1.1:  # 10% tolerance
        return ScenarioResult(
            "2. Token budget is respected", False,
            f"Context is ~{estimated} tokens, budget was {small_budget}",
        )

    return ScenarioResult(
        "2. Token budget is respected", True,
        f"Context fits budget (~{estimated} tokens, budget {small_budget})",
    )


def scenario_3_cross_channel(system: MemorySystem) -> ScenarioResult:
    """Cross-channel continuity"""
    now = datetime.now(timezone.utc)

    messages = [
        Message(
            "xc1", "borrower-cross", "chat", "borrower",
            "I need to get pre-approved for a mortgage. Budget around $400K.",
            now - timedelta(hours=48),
        ),
        Message(
            "xc2", "borrower-cross", "chat", "agent",
            "I can help with pre-approval. I'll need your income docs and credit authorization.",
            now - timedelta(hours=47),
        ),
        Message(
            "xc3", "borrower-cross", "email", "borrower",
            "Attached are my W-2s and two months of bank statements as discussed in our chat.",
            now - timedelta(hours=24),
            metadata={
                "subject": "Documents for pre-approval",
                "attachments": ["w2_2024.pdf", "bank_jan.pdf", "bank_feb.pdf"],
            },
        ),
        Message(
            "xc4", "borrower-cross", "email", "agent",
            "Thank you for the documents. I see your W-2 shows $95,000 annual income.",
            now - timedelta(hours=23),
        ),
        Message(
            "xc5", "borrower-cross", "sms", "borrower",
            "Hey, any update on my pre-approval?",
            now - timedelta(hours=2),
        ),
    ]

    for msg in messages:
        system.ingest_message(msg)

    context = system.assemble_context("borrower-cross", token_budget=2000)
    ctx = context.lower()

    checks = {
        "pre-approval from chat": "pre-approv" in ctx or "preapprov" in ctx,
        "400K budget": "400" in context,
        "documents from email": (
            "w-2" in ctx or "w2" in ctx or "bank statement" in ctx
        ),
        "income figure": "95" in context,
    }

    failed = [k for k, v in checks.items() if not v]
    if failed:
        return ScenarioResult(
            "3. Cross-channel continuity", False,
            f"Cross-channel context missing: {', '.join(failed)}",
        )

    return ScenarioResult(
        "3. Cross-channel continuity", True,
        "Context unifies information across chat, email, and SMS",
    )


def scenario_4_action_memory(system: MemorySystem) -> ScenarioResult:
    """Action memory prevents repetition"""
    now = datetime.now(timezone.utc)

    system.ingest_message(Message(
        "am1", "borrower-actions", "chat", "borrower",
        "What documents do I need for my application?",
        now - timedelta(hours=1),
    ))
    system.ingest_message(Message(
        "am2", "borrower-actions", "chat", "agent",
        "You'll need pay stubs, W-2s, bank statements, and tax returns.",
        now - timedelta(minutes=59),
    ))

    # Record actions Penny already took
    system.record_action("borrower-actions", Action(
        action_id="act-001",
        borrower_id="borrower-actions",
        action_type="send_checklist",
        details={"items": ["pay_stubs", "w2", "bank_statements", "tax_returns"]},
        timestamp=now - timedelta(minutes=58),
    ))
    system.record_action("borrower-actions", Action(
        action_id="act-002",
        borrower_id="borrower-actions",
        action_type="request_document",
        details={"document_type": "w2", "message": "Please upload your W-2."},
        timestamp=now - timedelta(minutes=30),
    ))

    # Check actions are retrievable
    actions = system.get_actions("borrower-actions")
    action_types = [a.action_type for a in actions]

    if "send_checklist" not in action_types:
        return ScenarioResult(
            "4. Action memory", False,
            "get_actions() missing checklist action",
        )
    if "request_document" not in action_types:
        return ScenarioResult(
            "4. Action memory", False,
            "get_actions() missing document request action",
        )

    # Check actions appear in context
    context = system.assemble_context("borrower-actions", token_budget=2000)
    ctx = context.lower()

    has_action_ref = any(kw in ctx for kw in [
        "checklist", "already", "sent", "requested", "action", "w2", "w-2",
    ])

    if not has_action_ref:
        return ScenarioResult(
            "4. Action memory", False,
            "Context does not reference prior actions — agent may repeat itself",
        )

    return ScenarioResult(
        "4. Action memory", True,
        "Actions are tracked and surfaced in context",
    )


def scenario_5_recency(system: MemorySystem) -> ScenarioResult:
    """Recency prioritization under pressure"""
    now = datetime.now(timezone.utc)

    # 20 old messages
    for i in range(20):
        system.ingest_message(Message(
            f"old-{i}", "borrower-recency", "chat", "borrower",
            f"Old message {i}: discussing initial property search criteria and neighborhoods.",
            now - timedelta(days=5, minutes=20 - i),
        ))

    # Recent messages with a unique keyword
    recent_keyword = "CURRENT_APPRAISAL_DISPUTE"
    system.ingest_message(Message(
        "recent-1", "borrower-recency", "chat", "borrower",
        f"The appraiser valued the home at $380K but comps show $420K. {recent_keyword}.",
        now - timedelta(minutes=5),
    ))
    system.ingest_message(Message(
        "recent-2", "borrower-recency", "chat", "agent",
        "I understand your concern about the appraisal. Let me look into dispute options.",
        now - timedelta(minutes=4),
    ))

    # Tight budget forces prioritization
    context = system.assemble_context("borrower-recency", token_budget=300)
    ctx = context.lower()

    has_recent = (
        recent_keyword.lower() in ctx
        or "apprais" in ctx
        or "380" in context
        or "420" in context
    )

    if not has_recent:
        return ScenarioResult(
            "5. Recency prioritization", False,
            "Tight-budget context lacks recent messages — old messages may be crowding them out",
        )

    return ScenarioResult(
        "5. Recency prioritization", True,
        "Recent conversation is prioritized under tight token budgets",
    )


def scenario_6_llm_integration(system: MemorySystem) -> ScenarioResult:
    """LLM integration — context produces coherent response"""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key or api_key == "your-api-key-here":
        return ScenarioResult(
            "6. LLM integration", False,
            "Skipped — set OPENAI_API_KEY in .env to run this scenario",
        )

    now = datetime.now(timezone.utc)

    messages = [
        Message(
            "llm1", "borrower-llm", "chat", "borrower",
            "I'm applying for a 30-year fixed mortgage. Household income is $120,000.",
            now - timedelta(hours=2),
        ),
        Message(
            "llm2", "borrower-llm", "chat", "agent",
            "Great, let's get started. What's the purchase price of the property?",
            now - timedelta(hours=1, minutes=55),
        ),
        Message(
            "llm3", "borrower-llm", "chat", "borrower",
            "We're looking at a home listed at $450,000. We have $90,000 for down payment.",
            now - timedelta(hours=1, minutes=50),
        ),
        Message(
            "llm4", "borrower-llm", "email", "borrower",
            "Here are my pay stubs showing monthly gross of $10,000.",
            now - timedelta(hours=1),
        ),
    ]

    for msg in messages:
        system.ingest_message(msg)

    context = system.assemble_context("borrower-llm", token_budget=1500)

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are Penny, an AI loan officer assistant. "
                        "Use the following context to answer.\n\n" + context
                    ),
                },
                {
                    "role": "user",
                    "content": "What's my loan-to-value ratio based on what we've discussed?",
                },
            ],
            max_tokens=300,
        )
        answer = (response.choices[0].message.content or "").lower()

        # LTV = (450000 - 90000) / 450000 = 80%
        has_ltv = "80" in answer or "loan-to-value" in answer or "ltv" in answer

        if not has_ltv:
            return ScenarioResult(
                "6. LLM integration", False,
                f"LLM response didn't reference LTV — context may be poorly structured.\n"
                f"         Response preview: {answer[:200]}",
            )

        return ScenarioResult(
            "6. LLM integration", True,
            "LLM produced coherent response using assembled context",
        )

    except Exception as e:
        return ScenarioResult(
            "6. LLM integration", False,
            f"LLM call failed: {e}",
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SCENARIOS = [
    scenario_1_basic_ingest_and_retrieve,
    scenario_2_token_budget,
    scenario_3_cross_channel,
    scenario_4_action_memory,
    scenario_5_recency,
    scenario_6_llm_integration,
]


def _load_system() -> MemorySystem:
    """Import the candidate's implementation from memory_system.py."""
    try:
        mod = importlib.import_module("memory_system")
    except ModuleNotFoundError:
        print("ERROR: Could not import memory_system.py")
        print()
        print("Create a file called memory_system.py that exports a class")
        print("implementing the MemorySystem protocol defined in this file.")
        sys.exit(1)

    # Look for a class implementing the protocol
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and obj is not MemorySystem:
            try:
                instance = obj()
                if isinstance(instance, MemorySystem):
                    return instance
            except TypeError:
                continue

    # Fallback: factory function
    if hasattr(mod, "create_memory_system"):
        instance = mod.create_memory_system()
        if isinstance(instance, MemorySystem):
            return instance

    print("ERROR: No class implementing MemorySystem found in memory_system.py")
    print()
    print("Your module must export a class with these methods:")
    print("  - ingest_message(message: Message) -> None")
    print("  - assemble_context(borrower_id: str, token_budget: int) -> str")
    print("  - record_action(borrower_id: str, action: Action) -> None")
    print("  - get_actions(borrower_id: str) -> list[Action]")
    sys.exit(1)


def main() -> None:
    # Load .env
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    print("=" * 64)
    print("  AI Agent Context & Memory System — Evaluation")
    print("=" * 64)
    print()

    system = _load_system()
    system_cls = type(system)
    print(f"Loaded: {system_cls.__name__}")
    print()

    results: list[ScenarioResult] = []

    for scenario_fn in SCENARIOS:
        # Fresh instance per scenario — no state leakage
        fresh = system_cls()
        t0 = time.perf_counter()
        try:
            result = scenario_fn(fresh)
        except Exception:
            doc = scenario_fn.__doc__ or scenario_fn.__name__
            result = ScenarioResult(doc, False, f"Exception:\n{traceback.format_exc()}")
        result.duration_ms = (time.perf_counter() - t0) * 1000
        results.append(result)

        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {result.name}")
        print(f"         {result.detail}")
        print(f"         ({result.duration_ms:.0f}ms)")
        print()

    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print("-" * 64)
    print(f"  Result: {passed}/{total} scenarios passed")
    print("-" * 64)

    # Allow LLM scenario to fail without hard exit
    sys.exit(0 if passed >= total - 1 else 1)


if __name__ == "__main__":
    main()
