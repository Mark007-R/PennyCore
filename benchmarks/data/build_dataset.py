"""Deterministic builder for the Phase-3 context-engine benchmark dataset.

Produces 200 ``(customer_history, query, ground_truth)`` pairs split
across length buckets. Two sources:

1. **Banking templates** (50 pairs) — synthetic mortgage/loan scenarios
   generated from 10 hand-curated base templates, each with 5 variants
   that vary channel mix, prior-action presence, and message count.
   Names use the SKILL's clearly-fake convention (Jane Doe, Acme Bank).
2. **MultiWOZ-derived** (150 pairs) — real public dialogues from
   ``multi_woz_v22`` (HuggingFace, MIT-style license). For each pair
   the LAST user turn becomes the query, the actual SYSTEM response
   right after becomes the ground-truth answer, and earlier turns
   become the customer history. Long histories are constructed by
   concatenating multiple unrelated dialogues with shifted timestamps
   so a single "customer" appears to have multiple prior conversations.

The builder is fully deterministic given ``--seed`` (default 42). The
canonical artifacts live in this directory:

* ``customer_histories.jsonl`` — one customer per line, schema below.
* ``queries.jsonl`` — one pair per line, schema below.
* ``manifest.json`` — counts, seed, source versions, build timestamp.

Schemas are documented in ``benchmarks/data/README.md``. The Day-13/14/
15 benchmark scripts read these JSONL files; they do NOT re-invoke this
builder. Re-running the builder with the same seed reproduces the
artifacts byte-for-byte (modulo the manifest's build_timestamp).
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

HERE = Path(__file__).resolve().parent
HISTORIES_PATH = HERE / "customer_histories.jsonl"
QUERIES_PATH = HERE / "queries.jsonl"
MANIFEST_PATH = HERE / "manifest.json"

TENANT_BANK_A = "tenant_acme_bank"
TENANT_BANK_B = "tenant_jefferson_credit"
TENANT_TRAVEL = "tenant_globetrek_concierge"

LENGTH_BUCKETS = {
    "short": (10, 30),
    "medium": (31, 100),
    "long": (101, 300),
    "very_long": (301, 500),
}
TARGET_DISTRIBUTION = {
    "short": 100,
    "medium": 60,
    "long": 30,
    "very_long": 10,
}
assert sum(TARGET_DISTRIBUTION.values()) == 200

BANKING_TARGET = 50
MULTIWOZ_TARGET = 150


@dataclasses.dataclass
class Message:
    channel: str
    sender: str
    content: str
    timestamp: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class PriorAction:
    action_type: str
    details: dict[str, Any]
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class CustomerHistory:
    customer_id: str
    tenant_id: str
    channel_messages: list[Message]
    prior_actions: list[PriorAction]
    source: str
    source_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "tenant_id": self.tenant_id,
            "channel_messages": [m.to_dict() for m in self.channel_messages],
            "prior_actions": [a.to_dict() for a in self.prior_actions],
            "source": self.source,
            "source_ids": self.source_ids,
        }


@dataclasses.dataclass
class QueryPair:
    pair_id: str
    customer_id: str
    tenant_id: str
    query: str
    ground_truth: str
    history_message_count: int
    history_bucket: str
    source: str
    intent: str
    tags: list[str]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------
# Banking templates (50 pairs)
# ---------------------------------------------------------------------

# Each template produces a (history_template, query, ground_truth, intent, tags).
# Variants modulate channel mix and history length within the "short" / "medium"
# buckets — banking templates never produce long/very_long histories. MultiWOZ
# concatenation handles those buckets.

_LOAN_OFFICER_NAMES = [
    "Loan Officer Sam Patel",
    "Loan Officer Robin Chen",
    "Loan Officer Devon Hayes",
    "Loan Officer Priya Shah",
    "Loan Officer Jordan Reeves",
]
_CUSTOMER_NAMES = [
    ("Jane Doe", "jane.doe@example.com", "+1-555-0142"),
    ("Marcus Bell", "marcus.bell@example.com", "+1-555-0167"),
    ("Sofia Ortiz", "sofia.ortiz@example.com", "+1-555-0193"),
    ("Theo Fairfax", "theo.fairfax@example.com", "+1-555-0206"),
    ("Lin Zhao", "lin.zhao@example.com", "+1-555-0274"),
]


def _ts(base: datetime, minutes: int) -> str:
    return (base + timedelta(minutes=minutes)).isoformat()


def _banking_pair(
    *,
    pair_idx: int,
    template_idx: int,
    variant_idx: int,
    rng: random.Random,
) -> tuple[CustomerHistory, QueryPair]:
    """Produce one banking-template pair given its template/variant indices."""
    template_idx = template_idx % 10
    variant_idx = variant_idx % 5

    name, email, phone = _CUSTOMER_NAMES[variant_idx]
    officer = _LOAN_OFFICER_NAMES[variant_idx]
    cust_id = f"cust_b{pair_idx:03d}"
    tenant_id = TENANT_BANK_A if (pair_idx % 2 == 0) else TENANT_BANK_B

    base_dt = datetime(2026, 4, 1, 9, 0, 0, tzinfo=timezone.utc) + timedelta(
        days=pair_idx
    )

    # Channel mix per variant: 0=chat-only, 1=email-only, 2=mixed chat+email,
    # 3=sms+email, 4=cross-channel chat→email→sms (the linking-stress variant)
    channel_recipe = [
        ["chat"] * 8,
        ["email"] * 6,
        ["chat", "chat", "email", "chat", "email", "email"],
        ["sms", "sms", "email", "email", "sms"],
        ["chat", "chat", "email", "sms", "email", "chat", "sms"],
    ][variant_idx]

    # Build the history for this template. The dispatcher returns the message
    # bodies + query/answer/intent/tags. We attach channels + timestamps here so
    # all 10 templates share a consistent surface.
    builder = _BANKING_TEMPLATES[template_idx]
    history_bodies, prior_action_specs, query, answer, intent, tags = builder(
        name=name, email=email, phone=phone, officer=officer, rng=rng
    )

    # Pad message count up to the variant's target via repeated small-talk
    # turns (so length variance shows up across templates × variants).
    target_msgs = [10, 14, 18, 22, 28][variant_idx]
    while len(history_bodies) < target_msgs:
        filler_sender = rng.choice(["customer", "agent"])
        if filler_sender == "customer":
            history_bodies.append(("customer", "Just checking in — any update?"))
        else:
            history_bodies.append(("agent", "Thanks — I'll follow up shortly."))

    messages: list[Message] = []
    for i, (sender, body) in enumerate(history_bodies):
        ch = channel_recipe[i % len(channel_recipe)]
        meta: dict[str, Any] = {}
        if ch == "email":
            meta["subject"] = f"Re: Loan #{cust_id[-3:]}"
        if ch == "sms":
            meta["from_phone"] = phone if sender == "customer" else "+1-555-BANK"
        messages.append(
            Message(
                channel=ch,
                sender=name if sender == "customer" else officer,
                content=body,
                timestamp=_ts(base_dt, i * 27),
                metadata=meta,
            )
        )

    prior_actions: list[PriorAction] = []
    for offset_min, atype, details in prior_action_specs:
        prior_actions.append(
            PriorAction(
                action_type=atype,
                details=details,
                timestamp=_ts(base_dt, offset_min),
            )
        )

    history = CustomerHistory(
        customer_id=cust_id,
        tenant_id=tenant_id,
        channel_messages=messages,
        prior_actions=prior_actions,
        source="banking_template",
        source_ids=[f"template_{template_idx:02d}_variant_{variant_idx}"],
    )

    bucket = _bucket_for(len(messages))
    pair = QueryPair(
        pair_id=f"pair_{pair_idx:03d}",
        customer_id=cust_id,
        tenant_id=tenant_id,
        query=query,
        ground_truth=answer,
        history_message_count=len(messages),
        history_bucket=bucket,
        source="banking_template",
        intent=intent,
        tags=tags,
    )
    return history, pair


def _t_rate_lookup(*, name, email, phone, officer, rng):
    msgs = [
        ("customer", f"Hi, I spoke with {officer} last week about a 30-year fixed mortgage."),
        ("agent", f"Welcome back, {name}. I see your file. What can I help with?"),
        ("customer", "Can you remind me what rate I was quoted?"),
        ("agent", "You were quoted 6.25% locked for 30 days as of April 12, 2026."),
        ("customer", "Great. Does that include points?"),
        ("agent", "That rate assumes 0.5 points paid at closing."),
    ]
    actions = [
        (5, "send_rate_quote", {"rate_pct": 6.25, "lock_days": 30, "points": 0.5}),
    ]
    return (
        msgs,
        actions,
        "Can you confirm the mortgage rate I was quoted and whether it includes points?",
        "The rate quoted was 6.25% locked for 30 days as of 2026-04-12, with 0.5 points paid at closing.",
        "rate_lookup",
        ["mortgage", "rate", "points"],
    )


def _t_checklist_followup(*, name, email, phone, officer, rng):
    msgs = [
        ("customer", "Hi, I want to make sure I sent everything you needed."),
        ("agent", f"Of course, {name}. Let me check what we received."),
        ("customer", "I'm worried I missed something."),
        ("agent", "We have your W-2s and last two pay stubs. Still need your bank statements."),
    ]
    actions = [
        (3, "send_checklist", {"items": ["w2", "pay_stubs", "bank_statements", "tax_returns"]}),
        (60, "mark_received", {"item": "w2"}),
        (61, "mark_received", {"item": "pay_stubs"}),
    ]
    return (
        msgs,
        actions,
        "What documents am I still missing for my mortgage application?",
        "You still need to send your bank statements and tax returns; W-2s and pay stubs were received.",
        "checklist_followup",
        ["mortgage", "documents", "checklist"],
    )


def _t_status_check(*, name, email, phone, officer, rng):
    msgs = [
        ("customer", "What's the status of my application?"),
        ("agent", "Your application is currently in underwriting review."),
        ("customer", "How long does that usually take?"),
        ("agent", "Typically 5-7 business days from when underwriting begins."),
        ("customer", "When did underwriting begin?"),
        ("agent", "It began on April 18, 2026."),
    ]
    actions = [
        (10, "status_change", {"from": "submitted", "to": "underwriting", "on": "2026-04-18"}),
    ]
    return (
        msgs,
        actions,
        "When did underwriting start on my application and how long should it take?",
        "Underwriting began on 2026-04-18 and typically takes 5-7 business days.",
        "status_check",
        ["mortgage", "status", "underwriting"],
    )


def _t_refi_question(*, name, email, phone, officer, rng):
    msgs = [
        ("customer", "I'm thinking about refinancing my current mortgage. It's a 7.5% from 2023."),
        ("agent", "We can definitely look at that. Current 30-year rates are around 6.25%."),
        ("customer", "What would the savings be on a $400K balance?"),
        ("agent", "Roughly $310/month in lower payments, but you'd pay about $4,500 in closing."),
    ]
    actions: list[tuple[int, str, dict]] = []
    return (
        msgs,
        actions,
        "What's my monthly savings if I refi my $400K loan from 7.5% to 6.25%?",
        "About $310/month in lower payments, against approximately $4,500 in closing costs.",
        "refi_question",
        ["refinance", "savings"],
    )


def _t_closing_date(*, name, email, phone, officer, rng):
    msgs = [
        ("customer", "I just want to confirm our closing date."),
        ("agent", "Closing is scheduled for May 22, 2026 at 10:00 AM."),
        ("customer", "Where do I need to be?"),
        ("agent", "At First American Title, 200 Main St, Suite 300."),
    ]
    actions = [
        (5, "schedule_closing", {"date": "2026-05-22", "time": "10:00", "location": "First American Title, 200 Main St, Suite 300"}),
    ]
    return (
        msgs,
        actions,
        "When and where is my closing scheduled?",
        "Closing is on 2026-05-22 at 10:00 AM at First American Title, 200 Main St, Suite 300.",
        "closing_logistics",
        ["closing", "logistics"],
    )


def _t_handoff(*, name, email, phone, officer, rng):
    msgs = [
        ("customer", "Started chatting earlier about a loan. Switching to email for the docs."),
        ("agent", "Got it. I have your file open."),
        ("customer", "I just emailed over the W-2 and pay stubs."),
        ("agent", "Received both. I'll mark them in the file."),
        ("customer", "Anything else needed today?"),
        ("agent", "Just your bank statements when you have a moment."),
    ]
    actions = [
        (45, "mark_received", {"item": "w2", "channel": "email"}),
        (46, "mark_received", {"item": "pay_stubs", "channel": "email"}),
    ]
    return (
        msgs,
        actions,
        "What's the next document I need to send and through which channel?",
        "Send your bank statements next; email is fine since the W-2 and pay stubs already arrived that way.",
        "handoff_followup",
        ["multi_channel", "documents"],
    )


def _t_preapproval(*, name, email, phone, officer, rng):
    msgs = [
        ("customer", "My pre-approval letter expires soon, doesn't it?"),
        ("agent", "Yes, it's valid through April 30, 2026."),
        ("customer", "We haven't found a house yet. What happens after that?"),
        ("agent", "We'll re-pull credit and re-issue, usually same day."),
    ]
    actions = [
        (8, "send_preapproval", {"valid_through": "2026-04-30", "amount_usd": 425000}),
    ]
    return (
        msgs,
        actions,
        "When does my pre-approval expire and what's the renewal process?",
        "It expires 2026-04-30; renewal involves re-pulling credit and re-issuing the letter, usually same day.",
        "preapproval_expiry",
        ["preapproval", "renewal"],
    )


def _t_appraisal(*, name, email, phone, officer, rng):
    msgs = [
        ("customer", "Has the appraisal come back yet?"),
        ("agent", "It came in yesterday at $445,000."),
        ("customer", "We offered $450K. Is that going to be a problem?"),
        ("agent", "We can either renegotiate, bring extra cash to closing, or contest the appraisal."),
    ]
    actions = [
        (15, "order_appraisal", {"vendor": "AppraisalFirst", "ordered_on": "2026-04-15"}),
        (200, "appraisal_received", {"value_usd": 445000, "received_on": "2026-04-22"}),
    ]
    return (
        msgs,
        actions,
        "What was the appraised value and what are my options if it came in low?",
        "Appraised at $445,000; options are to renegotiate, bring extra cash, or contest the appraisal.",
        "appraisal_outcome",
        ["appraisal", "negotiation"],
    )


def _t_down_payment(*, name, email, phone, officer, rng):
    msgs = [
        ("customer", "Quick question on the down payment amount."),
        ("agent", "Sure. You're putting down $90,000 on the $450K purchase, which is 20%."),
        ("customer", "And the earnest money is separate?"),
        ("agent", "Correct. Earnest money was $5,000, paid at offer acceptance, and credits toward the down payment."),
    ]
    actions: list[tuple[int, str, dict]] = []
    return (
        msgs,
        actions,
        "What's my down payment amount and how does the earnest money apply?",
        "Down payment is $90,000 (20% of $450K); the $5,000 earnest money credits toward it at closing.",
        "down_payment_breakdown",
        ["down_payment", "earnest_money"],
    )


def _t_pmi(*, name, email, phone, officer, rng):
    msgs = [
        ("customer", "Will I have to pay PMI on this loan?"),
        ("agent", "Since you're putting down 15% you would have PMI, yes."),
        ("customer", "When can I drop it?"),
        ("agent", "Once you reach 20% equity in the home, which based on your amortization is around month 38."),
    ]
    actions: list[tuple[int, str, dict]] = []
    return (
        msgs,
        actions,
        "Do I have to pay PMI and when can I get rid of it?",
        "Yes — PMI applies because down payment is under 20%; it can be removed at 20% equity, around month 38 on your amortization.",
        "pmi_question",
        ["pmi", "amortization"],
    )


_BANKING_TEMPLATES = [
    _t_rate_lookup,
    _t_checklist_followup,
    _t_status_check,
    _t_refi_question,
    _t_closing_date,
    _t_handoff,
    _t_preapproval,
    _t_appraisal,
    _t_down_payment,
    _t_pmi,
]


# ---------------------------------------------------------------------
# MultiWOZ-derived (150 pairs)
# ---------------------------------------------------------------------


def _bucket_for(n_msgs: int) -> str:
    for name, (lo, hi) in LENGTH_BUCKETS.items():
        if lo <= n_msgs <= hi:
            return name
    return "very_long" if n_msgs > 500 else "short"


def _multiwoz_dialogues(limit: int = 600) -> Iterator[dict[str, Any]]:
    """Stream MultiWOZ dialogues sorted by dialogue_id for determinism."""
    from datasets import load_dataset  # type: ignore

    ds = load_dataset(
        "multi_woz_v22", split="train", streaming=True, trust_remote_code=True
    )
    collected: list[dict[str, Any]] = []
    for s in ds:
        # Need at least 4 user turns (so we can carve a query + history).
        speakers = s["turns"]["speaker"]
        n_user = sum(1 for sp in speakers if sp == 0)
        if n_user < 3:
            continue
        collected.append(s)
        if len(collected) >= limit:
            break
    collected.sort(key=lambda d: d["dialogue_id"])
    yield from collected


def _dialogue_to_messages(
    dialogue: dict[str, Any],
    *,
    base_dt: datetime,
    minute_offset: int,
) -> list[Message]:
    """Convert one MultiWOZ dialogue into (n_turns - 2) history Messages.

    The last user turn is reserved as the query; the system response right
    after is reserved as the ground truth. Everything before is history.
    """
    speakers = dialogue["turns"]["speaker"]
    utterances = dialogue["turns"]["utterance"]
    last_user_idx = max(i for i, sp in enumerate(speakers) if sp == 0)
    history_turns = list(zip(speakers[:last_user_idx], utterances[:last_user_idx]))

    msgs: list[Message] = []
    for i, (sp, ut) in enumerate(history_turns):
        sender = "customer" if sp == 0 else "agent"
        msgs.append(
            Message(
                channel="chat",
                sender=sender,
                content=ut,
                timestamp=_ts(base_dt, minute_offset + i * 3),
                metadata={"source_dialogue": dialogue["dialogue_id"]},
            )
        )
    return msgs


def _dialogue_query_and_truth(dialogue: dict[str, Any]) -> tuple[str, str]:
    speakers = dialogue["turns"]["speaker"]
    utterances = dialogue["turns"]["utterance"]
    last_user_idx = max(i for i, sp in enumerate(speakers) if sp == 0)
    query = utterances[last_user_idx]
    if last_user_idx + 1 < len(utterances) and speakers[last_user_idx + 1] == 1:
        truth = utterances[last_user_idx + 1]
    else:
        truth = "(no system response in source dialogue — judge against history coherence only)"
    return query, truth


def _multiwoz_pair(
    *,
    pair_idx: int,
    pool: list[dict[str, Any]],
    target_bucket: str,
    rng: random.Random,
) -> tuple[CustomerHistory, QueryPair]:
    """Pull one or more dialogues from the pool to hit the target bucket."""
    primary = pool.pop(0)
    base_dt = datetime(2026, 3, 1, 8, 0, 0, tzinfo=timezone.utc) + timedelta(
        days=pair_idx
    )

    # Convert the primary dialogue. This gives us the query + ground truth.
    query, truth = _dialogue_query_and_truth(primary)

    primary_msgs = _dialogue_to_messages(primary, base_dt=base_dt, minute_offset=0)

    target_lo, target_hi = LENGTH_BUCKETS[target_bucket]
    msgs: list[Message] = list(primary_msgs)
    used_dialogues = [primary["dialogue_id"]]

    # Concatenate older unrelated dialogues until we hit the target bucket.
    # Older dialogues get earlier timestamps so the primary dialogue (which
    # contains the answer's context) stays newest.
    minute_back = -60 * 24  # one day earlier per padding dialogue
    while len(msgs) < target_lo and pool:
        pad = pool.pop(0)
        used_dialogues.append(pad["dialogue_id"])
        pad_msgs = _dialogue_to_messages(
            pad, base_dt=base_dt, minute_offset=minute_back
        )
        # Older first → goes at the front. Newest (primary) stays at the end.
        msgs = pad_msgs + msgs
        minute_back -= 60 * 24

    # If we overshot the upper bound, trim oldest until we fit.
    while len(msgs) > target_hi and len(used_dialogues) > 1:
        # Drop oldest single message at a time to land inside the bucket.
        msgs = msgs[1:]

    cust_id = f"cust_m{pair_idx:03d}"
    tenant_id = TENANT_TRAVEL  # MultiWOZ is travel/concierge domain
    history = CustomerHistory(
        customer_id=cust_id,
        tenant_id=tenant_id,
        channel_messages=msgs,
        prior_actions=[],
        source="multiwoz",
        source_ids=used_dialogues,
    )

    bucket = _bucket_for(len(msgs))
    services = primary.get("services") or ["general"]
    pair = QueryPair(
        pair_id=f"pair_{pair_idx:03d}",
        customer_id=cust_id,
        tenant_id=tenant_id,
        query=query,
        ground_truth=truth,
        history_message_count=len(msgs),
        history_bucket=bucket,
        source="multiwoz",
        intent="multiwoz_followup",
        tags=services,
    )
    return history, pair


# ---------------------------------------------------------------------
# Builder entrypoint
# ---------------------------------------------------------------------


def build(seed: int = 42) -> dict[str, Any]:
    """Materialize the dataset deterministically. Returns the manifest dict."""
    rng = random.Random(seed)

    histories: list[CustomerHistory] = []
    pairs: list[QueryPair] = []

    # 50 banking pairs — fill the SHORT bucket primarily, a few medium.
    # Banking variants 0-3 produce 10-22 msgs (short), variant 4 produces 28
    # msgs (still short under our (10,30) definition). All 50 are "short".
    for i in range(BANKING_TARGET):
        template_idx = i % 10
        variant_idx = (i // 10) % 5
        h, p = _banking_pair(
            pair_idx=i, template_idx=template_idx, variant_idx=variant_idx, rng=rng
        )
        histories.append(h)
        pairs.append(p)

    # 150 MultiWOZ pairs — fill remaining buckets per TARGET_DISTRIBUTION.
    # Already produced: 50 short (banking). Remaining quotas:
    #   short: 100 - 50 = 50
    #   medium: 60
    #   long: 30
    #   very_long: 10
    remaining_quota = {
        "short": TARGET_DISTRIBUTION["short"] - BANKING_TARGET,
        "medium": TARGET_DISTRIBUTION["medium"],
        "long": TARGET_DISTRIBUTION["long"],
        "very_long": TARGET_DISTRIBUTION["very_long"],
    }
    assert sum(remaining_quota.values()) == MULTIWOZ_TARGET, remaining_quota

    # Order the 150 pairs by bucket (short first, very_long last) so the
    # streaming pool consumes dialogues efficiently — very_long uses 20-30
    # source dialogues each, so we save them for last.
    bucket_order: list[str] = []
    for b in ("short", "medium", "long", "very_long"):
        bucket_order.extend([b] * remaining_quota[b])
    assert len(bucket_order) == MULTIWOZ_TARGET

    pool = list(_multiwoz_dialogues(limit=2200))
    if len(pool) < 200:
        raise RuntimeError(
            f"MultiWOZ pool too small ({len(pool)}); rerun with bigger limit"
        )

    for j, target_bucket in enumerate(bucket_order):
        pair_idx = BANKING_TARGET + j
        h, p = _multiwoz_pair(
            pair_idx=pair_idx, pool=pool, target_bucket=target_bucket, rng=rng
        )
        histories.append(h)
        pairs.append(p)

    # Write artifacts (sorted by pair_id for stable diffs).
    histories.sort(key=lambda h: h.customer_id)
    pairs.sort(key=lambda p: p.pair_id)

    HISTORIES_PATH.write_text(
        "\n".join(json.dumps(h.to_dict(), sort_keys=True) for h in histories) + "\n",
        encoding="utf-8",
    )
    QUERIES_PATH.write_text(
        "\n".join(json.dumps(p.to_dict(), sort_keys=True) for p in pairs) + "\n",
        encoding="utf-8",
    )

    bucket_counts: dict[str, int] = {b: 0 for b in LENGTH_BUCKETS}
    source_counts: dict[str, int] = {"banking_template": 0, "multiwoz": 0}
    for p in pairs:
        bucket_counts[p.history_bucket] += 1
        source_counts[p.source] += 1

    payload = HISTORIES_PATH.read_bytes() + QUERIES_PATH.read_bytes()
    artifact_sha = hashlib.sha256(payload).hexdigest()

    manifest = {
        "schema_version": 1,
        "seed": seed,
        "build_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "n_pairs": len(pairs),
        "n_histories": len(histories),
        "bucket_counts": bucket_counts,
        "source_counts": source_counts,
        "tenants": sorted({p.tenant_id for p in pairs}),
        "artifact_sha256": artifact_sha,
        "sources": {
            "banking_template": {
                "count": BANKING_TARGET,
                "license": "PennyCore-internal synthetic — clearly-fake names",
                "templates": 10,
                "variants_per_template": 5,
            },
            "multiwoz": {
                "count": MULTIWOZ_TARGET,
                "dataset": "multi_woz_v22",
                "split": "train",
                "license": "MultiWOZ 2.2 / Apache-style — see HuggingFace dataset card",
                "version_pin": "trust_remote_code=True; deterministic via sorted dialogue_id",
            },
        },
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    manifest = build(seed=args.seed)
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
