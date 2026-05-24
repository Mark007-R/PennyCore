"""Cross-channel customer linking (Day 6, Phase 2).

When an event arrives, this module answers: *which customer is this?*

The pipeline (SYSTEM_DESIGN §6.1):

  1. If the inbound request supplies an explicit `customer_id` AND it
     resolves in the same tenant → trust it. Caller knows best; nothing
     to link. (An invalid `customer_id` raises rather than silently
     falling through — silent fallthrough would mask client bugs.)
  2. Otherwise, extract identity *hints* from the event payload:
     `external_id`, `from_email` / `email`, `from_phone` / `phone`,
     `chat_handle`. Normalize per-kind.
  3. Walk the hints in priority order — `external_id` → `email` →
     `phone` → `chat_handle` — and look each up in
     `customer_identities`. First hit wins; the matched customer is
     enriched with any *new* hints (cross-channel linking happens here:
     a customer first seen by phone now also has their email recorded).
  4. If no hint matches, create a new customer + insert every hint as
     an identity row. If there were no hints AT ALL, create an
     anonymous customer (channel still produced an event we need to
     attribute somehow).

The (`tenant_id`, `identity_kind`, `identity_value`) UNIQUE oracle on
`customer_identities` is the join the in-memory repo and Postgres alike
enforce — meaning two events with the same email under the same tenant
*always* resolve to the same customer, by construction.

Multi-tenant invariant (rule 15): every read and write here is scoped by
`tenant_id`. There is no API path that forgets the tenant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from context_engine.customer_repository import CustomerRepository, IdentityCollision
from context_engine.ingestion import IngestionRequest
from contracts import IdentityKind

# ----------------------------------------------------------------------------
# Identity-hint extraction
# ----------------------------------------------------------------------------

# Payload-key → identity-kind map. INBOUND fields only — `to_email` /
# `to_phone` belong to PennyCore-the-recipient, not the customer, so they
# are deliberately omitted. Aliases reflect the heterogeneity of upstream
# channel adapters: a Twilio webhook surfaces `from_phone`, an internal CRM
# adapter might surface `phone` — both should land here.
_PAYLOAD_KEY_TO_KIND: dict[str, IdentityKind] = {
    "external_id": IdentityKind.EXTERNAL_ID,
    "customer_external_id": IdentityKind.EXTERNAL_ID,
    "from_email": IdentityKind.EMAIL,
    "email": IdentityKind.EMAIL,
    "sender_email": IdentityKind.EMAIL,
    "from_phone": IdentityKind.PHONE,
    "phone": IdentityKind.PHONE,
    "sender_phone": IdentityKind.PHONE,
    "chat_handle": IdentityKind.CHAT_HANDLE,
    "user_handle": IdentityKind.CHAT_HANDLE,
    "username": IdentityKind.CHAT_HANDLE,
}

# Lookup priority. The order is intentional:
#   * external_id is tenant-supplied and authoritative — same external_id
#     means same person, full stop.
#   * email is next-most stable (people change phones more than emails).
#   * phone is volatile (re-issued, transposed) but a strong signal.
#   * chat_handle is the most ambiguous (handles get reused on many
#     platforms) so it loses to everything else.
LOOKUP_PRIORITY: tuple[IdentityKind, ...] = (
    IdentityKind.EXTERNAL_ID,
    IdentityKind.EMAIL,
    IdentityKind.PHONE,
    IdentityKind.CHAT_HANDLE,
)


@dataclass(frozen=True)
class IdentityHint:
    """One canonical identity extracted from a payload, post-normalization."""

    kind: IdentityKind
    value: str  # already normalized via `normalize_identity_value`


# Phone normalization: collapse to "+digits" or "digits", dropping any
# spacing or punctuation a human might have typed. Naïve compared to
# libphonenumber but sufficient for MVP — rule 13 (no scope creep) keeps
# us off heavy phone-parsing dependencies until a customer query forces
# the issue. The takehome scenarios all use simple `+15550100123` shapes.
_PHONE_NON_DIGIT = re.compile(r"[^\d+]")


def normalize_identity_value(kind: IdentityKind, raw: str) -> str:
    """Canonicalize an identity value for storage AND lookup.

    Must match the `_ident_index_value` helper in
    `customer_repository.py` (lowercase+strip for email/chat_handle, strip
    for the rest) PLUS the extra phone normalization. The `CustomerIdentity`
    Pydantic validator independently re-applies the lowercase+strip step,
    so even a caller that bypasses this function and constructs a model
    directly still gets the canonical form.
    """
    v = raw.strip()
    if not v:
        return v
    if kind is IdentityKind.PHONE:
        # Strip everything that isn't a digit, but preserve a single leading "+".
        leading_plus = v.startswith("+")
        digits = _PHONE_NON_DIGIT.sub("", v)
        # `digits` may still contain "+" if the user typed e.g. "+1+5..." —
        # collapse to AT MOST one leading +.
        digits = digits.lstrip("+")
        return ("+" if leading_plus else "") + digits
    if kind in (IdentityKind.EMAIL, IdentityKind.CHAT_HANDLE):
        return v.lower()
    return v  # external_id: case-sensitive (CRM ids often are)


def extract_identity_hints(payload: dict[str, Any]) -> list[IdentityHint]:
    """Walk the payload, pull every recognized identity-bearing field,
    normalize, dedupe, return in lookup priority order.

    Unknown keys are ignored — the linker is conservative. A payload
    field with the right name but a non-string value is also ignored
    (silently): the call site already validated event shape, but we'd
    rather drop a weird hint than throw inside the ingestion hot path.
    Whitespace-only values are dropped after normalization.
    """
    seen: set[tuple[IdentityKind, str]] = set()
    hints: list[IdentityHint] = []
    for key, kind in _PAYLOAD_KEY_TO_KIND.items():
        raw = payload.get(key)
        if not isinstance(raw, str):
            continue
        normalized = normalize_identity_value(kind, raw)
        if not normalized:
            continue
        sig = (kind, normalized)
        if sig in seen:
            continue
        seen.add(sig)
        hints.append(IdentityHint(kind=kind, value=normalized))

    # Sort by priority so callers can rely on `hints[0]` being the most
    # authoritative kind present (used by `resolve_customer` for the
    # primary-match decision).
    priority_index = {k: i for i, k in enumerate(LOOKUP_PRIORITY)}
    hints.sort(key=lambda h: priority_index.get(h.kind, len(LOOKUP_PRIORITY)))
    return hints


def extract_display_name(payload: dict[str, Any]) -> str | None:
    """Pull a customer-friendly display name from the payload, if present.

    Tries `display_name`, `customer_name`, `from_name`, `sender_name` in
    that order. Returns the first non-empty string after strip; otherwise
    `None`. Truncates to 128 chars to fit the `Customer.display_name`
    contract.
    """
    for key in ("display_name", "customer_name", "from_name", "sender_name"):
        raw = payload.get(key)
        if isinstance(raw, str):
            v = raw.strip()
            if v:
                return v[:128]
    return None


# ----------------------------------------------------------------------------
# Linking outcome + the resolver
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkingOutcome:
    """Result of resolving a request to a customer.

    `matched_kind` and `matched_value` describe which identity hint
    actually triggered the lookup match — useful for the audit log
    ("we linked this event to cust_X via the email handle"). They are
    `None` for newly-created customers, since no match preceded
    creation.

    `identities_added` lists every hint freshly inserted into
    `customer_identities` on this resolve — this is the cross-channel
    linking signal the audit trail records ("first time we've seen this
    phone for cust_X").
    """

    customer_id: str
    customer_created: bool
    matched_kind: IdentityKind | None
    matched_value: str | None
    identities_added: tuple[IdentityHint, ...]


def resolve_customer(
    request: IngestionRequest,
    *,
    customer_repo: CustomerRepository,
) -> LinkingOutcome:
    """Resolve `request` to a `customer_id`, creating + linking as needed.

    See module docstring for the pipeline. Idempotent under replay: a
    second call with identical hints hits the same customer and adds no
    new identities (the `_identity_index` lookup is the dedup oracle).

    Raises:
      `LookupError` if the request supplies an explicit `customer_id`
      that isn't in this tenant — silent fallthrough to identity
      matching would mask the client bug.
    """
    tenant_id = request.tenant_id

    # 1. Explicit customer_id wins (after a tenant-scoped existence check).
    if request.customer_id is not None:
        existing = customer_repo.get_customer(
            tenant_id=tenant_id, customer_id=request.customer_id
        )
        if existing is None:
            raise LookupError(
                f"request.customer_id={request.customer_id!r} not found in "
                f"tenant {tenant_id!r}"
            )
        # Even with a known customer_id, we still enrich identities from the
        # payload (a new channel for an existing customer is THE cross-channel
        # linking case). Skip silently on collision — collision means the hint
        # already maps somewhere, which is fine if it maps to *this* customer
        # and a hard error if it maps to a different one (we surface that).
        added = _attach_new_identities(
            customer_id=existing.id,
            tenant_id=tenant_id,
            hints=extract_identity_hints(request.payload),
            customer_repo=customer_repo,
        )
        return LinkingOutcome(
            customer_id=existing.id,
            customer_created=False,
            matched_kind=None,
            matched_value=None,
            identities_added=tuple(added),
        )

    # 2. Identity-based match.
    hints = extract_identity_hints(request.payload)
    for kind in LOOKUP_PRIORITY:
        for hint in hints:
            if hint.kind is not kind:
                continue
            match = customer_repo.find_customer_by_identity(
                tenant_id=tenant_id,
                identity_kind=hint.kind,
                identity_value=hint.value,
            )
            if match is not None:
                # Found — enrich with any other hints we hadn't seen for
                # this customer yet.
                added = _attach_new_identities(
                    customer_id=match.id,
                    tenant_id=tenant_id,
                    hints=[h for h in hints if h is not hint],
                    customer_repo=customer_repo,
                )
                return LinkingOutcome(
                    customer_id=match.id,
                    customer_created=False,
                    matched_kind=hint.kind,
                    matched_value=hint.value,
                    identities_added=tuple(added),
                )

    # 3. No match — create a new customer with all available hints.
    #
    # Race-safe path (Day 21 hardening): the check-then-create pattern
    # above has a TOCTOU window — two concurrent ingests for the SAME
    # never-before-seen customer can BOTH miss step 2 and BOTH reach
    # step 3. The first thread's `create_customer` wins; the second
    # thread's `create_customer` collides on the
    # `(tenant_id, identity_kind, identity_value)` UNIQUE oracle and
    # raises `IdentityCollision`. With an in-memory repo + the GIL the
    # window is tiny; with the Postgres adapter the find→create round
    # trip opens the window to milliseconds, which Twilio-style retries
    # WILL race through. We tolerate the lost race by re-running the
    # identity match (which now succeeds thanks to the winner's write)
    # and returning that as a regular match. The race becomes
    # indistinguishable from a slightly-later replay.
    try:
        customer = customer_repo.create_customer(
            tenant_id=tenant_id,
            identities=[(h.kind, h.value) for h in hints],
            display_name=extract_display_name(request.payload),
        )
    except IdentityCollision:
        # The winner inserted at least one of OUR hints between our
        # step-2 miss and the create attempt. Re-run the priority walk
        # — exactly the same logic, now guaranteed to hit. The
        # second-step lookup is the source of truth; if it STILL
        # misses (very unlikely — only possible if the winner deleted
        # the identity in between, which the repository's append-only
        # nature forbids) we re-raise so the caller sees the real
        # failure rather than a silent infinite loop.
        for kind in LOOKUP_PRIORITY:
            for hint in hints:
                if hint.kind is not kind:
                    continue
                match = customer_repo.find_customer_by_identity(
                    tenant_id=tenant_id,
                    identity_kind=hint.kind,
                    identity_value=hint.value,
                )
                if match is not None:
                    added = _attach_new_identities(
                        customer_id=match.id,
                        tenant_id=tenant_id,
                        hints=[h for h in hints if h is not hint],
                        customer_repo=customer_repo,
                    )
                    return LinkingOutcome(
                        customer_id=match.id,
                        customer_created=False,
                        matched_kind=hint.kind,
                        matched_value=hint.value,
                        identities_added=tuple(added),
                    )
        raise  # truly inconsistent — surface the failure
    return LinkingOutcome(
        customer_id=customer.id,
        customer_created=True,
        matched_kind=None,
        matched_value=None,
        identities_added=tuple(hints),
    )


def _attach_new_identities(
    *,
    customer_id: str,
    tenant_id: str,
    hints: list[IdentityHint],
    customer_repo: CustomerRepository,
) -> list[IdentityHint]:
    """Add any of `hints` that don't already point at SOMEWHERE.

    Returns the subset that was actually inserted — the audit trail uses
    this list to record cross-channel linking events. Hints that already
    map to *this* customer are no-ops (idempotent). Hints that map to a
    *different* customer raise `IdentityCollision`, which we re-raise:
    that's a real data integrity problem (a phone number has been
    registered to two customers in the same tenant) and silent
    swallowing would hide it.
    """
    added: list[IdentityHint] = []
    for hint in hints:
        existing = customer_repo.find_customer_by_identity(
            tenant_id=tenant_id,
            identity_kind=hint.kind,
            identity_value=hint.value,
        )
        if existing is None:
            customer_repo.add_identity(
                tenant_id=tenant_id,
                customer_id=customer_id,
                identity_kind=hint.kind,
                identity_value=hint.value,
            )
            added.append(hint)
        elif existing.id != customer_id:
            raise IdentityCollision(
                f"identity ({hint.kind.value}={hint.value!r}) is linked to "
                f"customer {existing.id!r}, cannot also link to {customer_id!r} "
                f"in tenant {tenant_id!r}"
            )
        # else: already linked to this customer — no-op.
    return added
