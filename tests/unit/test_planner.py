"""Tests for the Day-9 orchestrator LLM planner.

Coverage:
  1. Mock-client short-circuit → rule-table fallback per event_type.
  2. LLM happy path with a fake client returning valid JSON.
  3. LLM markdown-fenced JSON gets stripped before parsing.
  4. LLM malformed JSON → fallback with `proposed_by=fallback`.
  5. LLM unknown action_type → fallback (schema violation).
  6. LLM call raises → fallback (network/SDK error).
  7. Missing envelope fields → ValueError (developer error, not user input).
  8. ProposalsBuffer isolation per tenant.
  9. chain_handlers continues on per-handler errors.
 10. make_planning_handler appends to buffer in mock mode.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from context_engine.llm.mock import MockClient
from contracts import ActionType, ProposedBy
from orchestrator.planner import (
    PlanRequest,
    ProposalsBuffer,
    chain_handlers,
    make_planning_handler,
    propose_action,
)

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _envelope(
    *,
    event_type: str = "message_received",
    tenant_id: str = "tenant-a",
    event_id: str = "evt_d9_001",
    customer_id: str | None = "cust_d9_001",
    channel_code: str = "email",
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "tenant_id": tenant_id,
        "customer_id": customer_id,
        "channel_code": channel_code,
        "event_type": event_type,
        "received_at": "2026-05-12T10:00:00+00:00",
    }


class _FakeLLM:
    """Minimal LLMClient stub. `name` is anything other than 'mock' so the
    planner takes the LLM path. `complete` either returns the canned
    response or raises the canned exception."""

    def __init__(
        self,
        *,
        name: str = "fake-llm",
        model: str = "fake-model",
        response: str | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self._response = response
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "system": system,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response

    def summarize(self, text: str, *, max_tokens: int = 256) -> str:  # noqa: ARG002
        return text[:max_tokens]


def _ids() -> Any:
    counter = {"n": 0}

    def _next() -> str:
        counter["n"] += 1
        return f"prop_test_{counter['n']:04d}"

    return _next


# ----------------------------------------------------------------------------
# Mock-mode rule table
# ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event_type,expected_action",
    [
        ("message_received", ActionType.SEND_BORROWER_MESSAGE),
        ("document_uploaded", ActionType.UPDATE_STATUS),
        ("status_changed", ActionType.NOTIFY_LOAN_OFFICER),
        ("anomaly_detected", ActionType.NOTIFY_LOAN_OFFICER),
        ("system_event", ActionType.NO_OP),
    ],
)
def test_mock_mode_uses_rule_table_per_event_type(
    event_type: str, expected_action: ActionType
) -> None:
    req = PlanRequest(envelope=_envelope(event_type=event_type))
    proposal = propose_action(
        req, client=MockClient(), proposal_id_factory=_ids()
    )
    assert proposal.action_type == expected_action
    assert proposal.proposed_by == ProposedBy.FALLBACK
    assert proposal.tenant_id == "tenant-a"
    assert proposal.event_id == "evt_d9_001"
    assert proposal.customer_id == "cust_d9_001"
    assert "_planner_reasoning" in proposal.payload


def test_mock_mode_unknown_event_type_escalates_to_loan_officer() -> None:
    req = PlanRequest(envelope=_envelope(event_type="something_unknown"))
    proposal = propose_action(
        req, client=MockClient(), proposal_id_factory=_ids()
    )
    assert proposal.action_type == ActionType.NOTIFY_LOAN_OFFICER
    assert proposal.proposed_by == ProposedBy.FALLBACK
    assert "unknown event_type" in proposal.payload["_planner_reasoning"]


def test_mock_mode_handles_unlinked_customer() -> None:
    """Linker may not have resolved a customer yet — proposal still emits."""
    req = PlanRequest(envelope=_envelope(customer_id=None))
    proposal = propose_action(
        req, client=MockClient(), proposal_id_factory=_ids()
    )
    assert proposal.customer_id is None
    assert proposal.proposed_by == ProposedBy.FALLBACK


# ----------------------------------------------------------------------------
# LLM happy path
# ----------------------------------------------------------------------------


def test_llm_valid_json_yields_llm_proposal() -> None:
    llm_response = json.dumps(
        {
            "action_type": "send_borrower_message",
            "reasoning": "customer asked about rate lock — acknowledge in 1h SLA",
            "payload": {"message": "Thanks Jane — we'll confirm by 5pm."},
        }
    )
    client = _FakeLLM(response=llm_response)
    req = PlanRequest(
        envelope=_envelope(event_type="message_received"),
        brief_text="Jane Doe asked about rate lock yesterday.",
    )
    proposal = propose_action(req, client=client, proposal_id_factory=_ids())
    assert proposal.action_type == ActionType.SEND_BORROWER_MESSAGE
    assert proposal.proposed_by == ProposedBy.LLM
    assert proposal.payload["message"] == "Thanks Jane — we'll confirm by 5pm."
    # reasoning gets folded into payload for the audit log
    assert "rate lock" in proposal.payload["_planner_reasoning"]
    # one LLM call, system prompt supplied
    assert len(client.calls) == 1
    assert client.calls[0]["system"] is not None
    assert "JSON object" in client.calls[0]["system"]


def test_llm_strips_markdown_code_fence() -> None:
    llm_response = (
        "```json\n"
        + json.dumps(
            {
                "action_type": "notify_loan_officer",
                "reasoning": "ambiguous status change",
                "payload": {"reason": "borrower changed employer"},
            }
        )
        + "\n```"
    )
    client = _FakeLLM(response=llm_response)
    req = PlanRequest(envelope=_envelope(event_type="status_changed"))
    proposal = propose_action(req, client=client, proposal_id_factory=_ids())
    assert proposal.action_type == ActionType.NOTIFY_LOAN_OFFICER
    assert proposal.proposed_by == ProposedBy.LLM


def test_llm_includes_brief_in_user_prompt() -> None:
    """The brief from context-engine must reach the LLM prompt — Day 11
    end-to-end test depends on this. Asserted here via prompt inspection
    on the fake client."""
    llm_response = json.dumps(
        {"action_type": "no_op", "reasoning": "test", "payload": {}}
    )
    client = _FakeLLM(response=llm_response)
    req = PlanRequest(
        envelope=_envelope(),
        brief_text="MARKER_BRIEF_TEXT customer history bullets here",
    )
    propose_action(req, client=client, proposal_id_factory=_ids())
    assert "MARKER_BRIEF_TEXT" in client.calls[0]["prompt"]


def test_llm_handles_empty_brief_explicitly() -> None:
    """When brief_text is empty, the prompt must say so explicitly so
    the LLM doesn't hallucinate context — assert the marker phrase."""
    llm_response = json.dumps(
        {"action_type": "no_op", "reasoning": "test", "payload": {}}
    )
    client = _FakeLLM(response=llm_response)
    req = PlanRequest(envelope=_envelope(), brief_text="")
    propose_action(req, client=client, proposal_id_factory=_ids())
    assert "no brief available" in client.calls[0]["prompt"]


# ----------------------------------------------------------------------------
# LLM error paths → fallback
# ----------------------------------------------------------------------------


def test_llm_malformed_json_falls_back() -> None:
    client = _FakeLLM(response="this is not JSON at all, just prose")
    req = PlanRequest(envelope=_envelope(event_type="message_received"))
    proposal = propose_action(req, client=client, proposal_id_factory=_ids())
    assert proposal.proposed_by == ProposedBy.FALLBACK
    assert proposal.action_type == ActionType.SEND_BORROWER_MESSAGE  # rule for message_received


def test_llm_unknown_action_type_falls_back() -> None:
    """An LLM that invents a new action_type must not pollute the action
    table — the planner must fall back to the rule table."""
    llm_response = json.dumps(
        {"action_type": "send_carrier_pigeon", "reasoning": "novel", "payload": {}}
    )
    client = _FakeLLM(response=llm_response)
    req = PlanRequest(envelope=_envelope(event_type="anomaly_detected"))
    proposal = propose_action(req, client=client, proposal_id_factory=_ids())
    assert proposal.proposed_by == ProposedBy.FALLBACK
    assert proposal.action_type == ActionType.NOTIFY_LOAN_OFFICER


def test_llm_non_dict_payload_falls_back() -> None:
    """payload must be a JSON object (dict). A list or string is a
    schema violation that triggers fallback."""
    llm_response = json.dumps(
        {
            "action_type": "send_borrower_message",
            "reasoning": "test",
            "payload": ["message", "as", "a", "list"],
        }
    )
    client = _FakeLLM(response=llm_response)
    req = PlanRequest(envelope=_envelope(event_type="message_received"))
    proposal = propose_action(req, client=client, proposal_id_factory=_ids())
    assert proposal.proposed_by == ProposedBy.FALLBACK


def test_llm_raises_network_error_falls_back() -> None:
    client = _FakeLLM(raises=RuntimeError("simulated network blip"))
    req = PlanRequest(envelope=_envelope(event_type="document_uploaded"))
    proposal = propose_action(req, client=client, proposal_id_factory=_ids())
    assert proposal.proposed_by == ProposedBy.FALLBACK
    assert proposal.action_type == ActionType.UPDATE_STATUS


def test_llm_empty_response_falls_back() -> None:
    client = _FakeLLM(response="")
    req = PlanRequest(envelope=_envelope(event_type="message_received"))
    proposal = propose_action(req, client=client, proposal_id_factory=_ids())
    assert proposal.proposed_by == ProposedBy.FALLBACK


def test_llm_response_is_list_not_dict_falls_back() -> None:
    client = _FakeLLM(response=json.dumps([{"action_type": "no_op"}]))
    req = PlanRequest(envelope=_envelope())
    proposal = propose_action(req, client=client, proposal_id_factory=_ids())
    assert proposal.proposed_by == ProposedBy.FALLBACK


# ----------------------------------------------------------------------------
# Input validation
# ----------------------------------------------------------------------------


def test_missing_tenant_id_raises_value_error() -> None:
    envelope = _envelope()
    del envelope["tenant_id"]
    with pytest.raises(ValueError, match="tenant_id"):
        propose_action(PlanRequest(envelope=envelope), client=MockClient())


def test_missing_event_id_raises_value_error() -> None:
    envelope = _envelope()
    del envelope["event_id"]
    with pytest.raises(ValueError, match="event_id"):
        propose_action(PlanRequest(envelope=envelope), client=MockClient())


# ----------------------------------------------------------------------------
# ProposalsBuffer
# ----------------------------------------------------------------------------


def test_proposals_buffer_isolates_tenants() -> None:
    buf = ProposalsBuffer()
    req_a = PlanRequest(envelope=_envelope(tenant_id="tenant-a", event_id="evt_a"))
    req_b = PlanRequest(envelope=_envelope(tenant_id="tenant-b", event_id="evt_b"))
    buf.append(propose_action(req_a, client=MockClient(), proposal_id_factory=_ids()))
    buf.append(propose_action(req_b, client=MockClient(), proposal_id_factory=_ids()))

    a = buf.recent("tenant-a")
    b = buf.recent("tenant-b")
    assert [p.event_id for p in a] == ["evt_a"]
    assert [p.event_id for p in b] == ["evt_b"]


def test_proposals_buffer_returns_most_recent_first() -> None:
    buf = ProposalsBuffer()
    ids = _ids()
    for i in range(5):
        req = PlanRequest(
            envelope=_envelope(tenant_id="acme", event_id=f"evt_{i}")
        )
        buf.append(propose_action(req, client=MockClient(), proposal_id_factory=ids))
    out = buf.recent("acme", limit=3)
    assert [p.event_id for p in out] == ["evt_4", "evt_3", "evt_2"]


def test_proposals_buffer_caps_per_tenant() -> None:
    buf = ProposalsBuffer(max_per_tenant=2)
    ids = _ids()
    for i in range(5):
        req = PlanRequest(
            envelope=_envelope(tenant_id="acme", event_id=f"evt_{i}")
        )
        buf.append(propose_action(req, client=MockClient(), proposal_id_factory=ids))
    out = buf.recent("acme", limit=10)
    assert [p.event_id for p in out] == ["evt_4", "evt_3"]


def test_proposals_buffer_empty_for_unknown_tenant() -> None:
    assert ProposalsBuffer().recent("never-published") == []


# ----------------------------------------------------------------------------
# Handler composition
# ----------------------------------------------------------------------------


def test_make_planning_handler_appends_proposal() -> None:
    buf = ProposalsBuffer()
    handler = make_planning_handler(
        buf,
        client_factory=MockClient,
        proposal_id_factory=_ids(),
    )
    handler(_envelope(event_type="message_received"))
    out = buf.recent("tenant-a")
    assert len(out) == 1
    assert out[0].action_type == ActionType.SEND_BORROWER_MESSAGE


def test_make_planning_handler_swallows_planner_errors() -> None:
    """A malformed envelope must not poison the listener — the planner
    handler logs and drops, never re-raises."""
    buf = ProposalsBuffer()
    handler = make_planning_handler(
        buf,
        client_factory=MockClient,
        proposal_id_factory=_ids(),
    )
    handler({"event_id": "evt", "tenant_id": ""})  # bad tenant — raises in propose_action

    # No exception bubbled out. Buffer untouched.
    assert buf.recent("tenant-a") == []


def test_chain_handlers_runs_all_in_order() -> None:
    calls: list[str] = []

    def h1(_env: dict[str, Any]) -> None:
        calls.append("h1")

    def h2(_env: dict[str, Any]) -> None:
        calls.append("h2")

    chained = chain_handlers(h1, h2)
    chained(_envelope())
    assert calls == ["h1", "h2"]


def test_chain_handlers_continues_after_handler_failure() -> None:
    """A bug in one handler must not stop subsequent handlers — same
    invariant as the listener publisher-vs-subscriber isolation."""
    calls: list[str] = []

    def h1(_env: dict[str, Any]) -> None:
        calls.append("h1")
        raise RuntimeError("h1 is broken")

    def h2(_env: dict[str, Any]) -> None:
        calls.append("h2")

    chained = chain_handlers(h1, h2)
    chained(_envelope())  # does NOT raise
    assert calls == ["h1", "h2"]
