"""Day 7 — recency-strategy retrieval tests.

Covers:
  * `recency_segments` returns BriefSegments sorted newest-first by
    priority.
  * Action segments outrank message segments of equal recency
    (the +1.0 boost).
  * Email metadata (subject, attachments) appears in the formatted
    body — rubric criterion C3.
  * Channel provenance markers (`[chat]`, `[email]`) appear at the
    head of every message segment — rubric criterion C2.
  * Action formatter surfaces `action_type` literally so the LLM can
    see "I already sent send_checklist" — rubric criterion D2.
  * Empty inputs return `[]`.
  * Duck-typed inputs (using `SimpleNamespace`) are accepted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from context_engine.retrieval.recency import (
    estimate_tokens,
    format_action_segment,
    format_message_segment,
    recency_segments,
)
from contracts import SegmentSource

NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def _msg(
    *,
    channel: str = "chat",
    sender: str = "borrower",
    content: str = "hello",
    timestamp: datetime | None = None,
    metadata: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        channel=channel,
        sender=sender,
        content=content,
        timestamp=timestamp or NOW,
        metadata=metadata or {},
    )


def _action(
    *,
    action_type: str = "send_checklist",
    details: dict | None = None,
    timestamp: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        action_type=action_type,
        details=details or {},
        timestamp=timestamp or NOW,
    )


class TestEstimateTokens:
    def test_empty_string_zero_tokens(self) -> None:
        assert estimate_tokens("") == 0

    def test_short_string_floor_div_four(self) -> None:
        assert estimate_tokens("abc") == 0  # 3 // 4 == 0
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("abcdefgh") == 2

    def test_matches_evaluator_estimator(self) -> None:
        # The takehome evaluator's `_estimate_tokens` is `len(text) // 4`.
        # Mirror three sample lengths.
        for text in ["short", "medium length text", "X" * 1000]:
            assert estimate_tokens(text) == len(text) // 4


class TestFormatMessageSegment:
    def test_includes_channel_marker(self) -> None:
        out = format_message_segment(_msg(channel="email"))
        assert out.startswith("[email]")

    def test_includes_sender_and_content(self) -> None:
        out = format_message_segment(
            _msg(sender="agent", content="hello there")
        )
        assert "agent: hello there" in out

    def test_email_subject_surfaced(self) -> None:
        out = format_message_segment(
            _msg(channel="email", metadata={"subject": "Documents"})
        )
        assert "Documents" in out

    def test_email_attachments_surfaced(self) -> None:
        out = format_message_segment(
            _msg(
                channel="email",
                metadata={"attachments": ["w2.pdf", "bank.pdf"]},
            )
        )
        assert "w2.pdf" in out
        assert "bank.pdf" in out

    def test_no_metadata_no_extras_appended(self) -> None:
        out = format_message_segment(_msg(content="plain"))
        assert out.endswith("plain")


class TestFormatActionSegment:
    def test_includes_action_marker_and_type(self) -> None:
        out = format_action_segment(_action(action_type="send_checklist"))
        assert "[action]" in out
        assert "send_checklist" in out

    def test_includes_details_compactly(self) -> None:
        out = format_action_segment(
            _action(
                action_type="request_document",
                details={"document_type": "w2"},
            )
        )
        assert "document_type=w2" in out

    def test_list_details_rendered_as_csv(self) -> None:
        out = format_action_segment(
            _action(
                action_type="send_checklist",
                details={"items": ["pay_stubs", "w2", "bank_statements"]},
            )
        )
        assert "items=pay_stubs, w2, bank_statements" in out


class TestRecencySegments:
    def test_empty_inputs_return_empty(self) -> None:
        assert recency_segments(messages=[], actions=[]) == []

    def test_messages_sorted_newest_first(self) -> None:
        old = _msg(content="old", timestamp=NOW - timedelta(hours=2))
        mid = _msg(content="mid", timestamp=NOW - timedelta(hours=1))
        new = _msg(content="new", timestamp=NOW)
        segs = recency_segments(messages=[old, new, mid], actions=[])
        bodies = [s.body for s in segs]
        assert bodies[0].endswith("new")
        assert bodies[1].endswith("mid")
        assert bodies[2].endswith("old")

    def test_action_outranks_message_at_same_timestamp(self) -> None:
        msg = _msg(content="msg-text", timestamp=NOW)
        act = _action(action_type="send_checklist", timestamp=NOW)
        segs = recency_segments(messages=[msg], actions=[act])
        # Action should be first because of the +1.0 priority boost.
        assert segs[0].source is SegmentSource.PRIOR_ACTION
        assert segs[1].source is SegmentSource.RECENT_MESSAGE

    def test_message_can_outrank_older_action(self) -> None:
        # Even with the +1.0 boost, an action 10 minutes older should
        # be beaten by a fresher message.
        old_act = _action(timestamp=NOW - timedelta(minutes=10))
        new_msg = _msg(content="fresh", timestamp=NOW)
        segs = recency_segments(messages=[new_msg], actions=[old_act])
        assert segs[0].source is SegmentSource.RECENT_MESSAGE

    def test_segment_metadata_preserved(self) -> None:
        msg = _msg(channel="email", sender="agent")
        segs = recency_segments(messages=[msg], actions=[])
        assert segs[0].metadata["channel"] == "email"
        assert segs[0].metadata["sender"] == "agent"

    def test_action_segment_records_action_type_in_metadata(self) -> None:
        act = _action(action_type="send_checklist")
        segs = recency_segments(messages=[], actions=[act])
        assert segs[0].metadata["action_type"] == "send_checklist"

    def test_token_estimate_populated(self) -> None:
        msg = _msg(content="x" * 100)
        segs = recency_segments(messages=[msg], actions=[])
        assert segs[0].token_estimate == estimate_tokens(segs[0].body)
        assert segs[0].token_estimate > 0
