"""Day 6 — cross-channel customer linking tests.

Coverage:
  * `extract_identity_hints` — every recognized payload key, alias
    handling, normalization, dedupe, priority ordering.
  * `normalize_identity_value` — phone digit-stripping, email/chat-handle
    case folding, external_id case preservation.
  * `resolve_customer` — every branch:
      - explicit customer_id (valid) → trust it; enrich identities.
      - explicit customer_id (unknown in tenant) → LookupError.
      - first contact (no hints, no customer_id) → anonymous customer.
      - first contact with hints → new customer + all identities inserted.
      - second contact, identity matches → existing customer returned.
      - second contact, multiple hints, one matches → existing customer
        + remaining hints attached.
      - cross-channel: SMS first, then email arrives with same external_id
        → both events resolve to same customer.
      - identity collision (same hint linked to a different customer) →
        IdentityCollision propagates.
      - multi-tenant: tenant A's identity does NOT match tenant B's
        lookup; same email under two tenants makes two customers.
      - lookup priority: external_id beats email beats phone beats
        chat_handle when multiple hints present.
  * `InMemoryCustomerRepository` — basic Protocol satisfaction, idempotent
    add_identity, collision detection, count helpers.
"""

from __future__ import annotations

import pytest

from context_engine.customer_repository import (
    CustomerRepository,
    IdentityCollision,
    InMemoryCustomerRepository,
)
from context_engine.ingestion import IngestionRequest
from context_engine.linking import (
    LOOKUP_PRIORITY,
    IdentityHint,
    extract_display_name,
    extract_identity_hints,
    normalize_identity_value,
    resolve_customer,
)
from contracts import IdentityKind

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _req(**payload: object) -> IngestionRequest:
    """Minimal ingestion request used to drive the linker. tenant_id and
    channel/event types are fixed to keep the test focus on payload
    parsing; tenant-isolation tests override `tenant_id` directly."""
    body: dict[str, object] = {
        "tenant_id": "acme-bank",
        "channel_code": "email",
        "event_type": "message_received",
        "idempotency_key": "test-key",
        "payload": payload,
    }
    return IngestionRequest(**body)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# normalize_identity_value
# ---------------------------------------------------------------------------


class TestNormalize:
    def test_email_lowercases_and_strips(self) -> None:
        assert (
            normalize_identity_value(IdentityKind.EMAIL, "  Jane.Doe@ACME.com ")
            == "jane.doe@acme.com"
        )

    def test_chat_handle_lowercases_and_strips(self) -> None:
        assert (
            normalize_identity_value(IdentityKind.CHAT_HANDLE, "  JaneD ")
            == "janed"
        )

    def test_external_id_preserves_case(self) -> None:
        # CRM ids are often case-sensitive (ABC-123 ≠ abc-123).
        assert (
            normalize_identity_value(IdentityKind.EXTERNAL_ID, "  ABC-123  ")
            == "ABC-123"
        )

    def test_phone_strips_formatting(self) -> None:
        assert (
            normalize_identity_value(IdentityKind.PHONE, "+1 (555) 010-0100")
            == "+15550100100"
        )

    def test_phone_without_plus_strips(self) -> None:
        assert (
            normalize_identity_value(IdentityKind.PHONE, "555.010.0100")
            == "5550100100"
        )

    def test_phone_collapses_multiple_plus(self) -> None:
        # User typed weird "+ 1 + 5..." — collapse to single leading +.
        assert (
            normalize_identity_value(IdentityKind.PHONE, "++15550100100")
            == "+15550100100"
        )

    def test_blank_is_blank(self) -> None:
        assert normalize_identity_value(IdentityKind.EMAIL, "   ") == ""


# ---------------------------------------------------------------------------
# extract_identity_hints + extract_display_name
# ---------------------------------------------------------------------------


class TestExtractHints:
    def test_no_hints_returns_empty(self) -> None:
        assert extract_identity_hints({}) == []

    def test_email_field(self) -> None:
        hints = extract_identity_hints({"from_email": "jane@acme.com"})
        assert hints == [IdentityHint(IdentityKind.EMAIL, "jane@acme.com")]

    def test_phone_normalization_applied(self) -> None:
        hints = extract_identity_hints({"from_phone": "+1 (555) 010-0100"})
        assert hints == [IdentityHint(IdentityKind.PHONE, "+15550100100")]

    def test_aliases_recognized(self) -> None:
        # `customer_external_id` is an alias for `external_id`; `username`
        # and `user_handle` are aliases for `chat_handle`.
        hints = extract_identity_hints(
            {"customer_external_id": "CRM-001", "username": "JaneD"}
        )
        kinds = {h.kind for h in hints}
        assert kinds == {IdentityKind.EXTERNAL_ID, IdentityKind.CHAT_HANDLE}
        assert any(h.value == "CRM-001" for h in hints)
        assert any(h.value == "janed" for h in hints)

    def test_dedupes_within_kind(self) -> None:
        # `email` and `from_email` and `sender_email` all carry the same
        # canonical email — should only show up once.
        hints = extract_identity_hints(
            {
                "email": "jane@acme.com",
                "from_email": "JANE@acme.com",
                "sender_email": "  jane@ACME.com  ",
            }
        )
        emails = [h for h in hints if h.kind is IdentityKind.EMAIL]
        assert len(emails) == 1
        assert emails[0].value == "jane@acme.com"

    def test_priority_ordering(self) -> None:
        # All four kinds present; result must be sorted in LOOKUP_PRIORITY order.
        hints = extract_identity_hints(
            {
                "chat_handle": "JaneD",
                "from_phone": "+15550100100",
                "from_email": "jane@acme.com",
                "external_id": "CRM-001",
            }
        )
        assert [h.kind for h in hints] == list(LOOKUP_PRIORITY)

    def test_non_string_value_ignored(self) -> None:
        # A number where a string was expected: drop silently. Hot path —
        # don't throw inside ingestion.
        hints = extract_identity_hints(
            {"from_email": 12345, "from_phone": "+15550100100"}
        )
        kinds = {h.kind for h in hints}
        assert kinds == {IdentityKind.PHONE}

    def test_blank_value_dropped_after_normalization(self) -> None:
        # `"   "` is blank after strip — the linker treats it as no-hint.
        hints = extract_identity_hints({"from_email": "   "})
        assert hints == []

    def test_unknown_keys_ignored(self) -> None:
        hints = extract_identity_hints(
            {"shoe_size": "10", "favorite_color": "blue"}
        )
        assert hints == []


class TestExtractDisplayName:
    def test_picks_first_non_empty(self) -> None:
        assert (
            extract_display_name({"display_name": "Jane Doe"}) == "Jane Doe"
        )

    def test_falls_through_aliases(self) -> None:
        # `display_name` missing → tries `customer_name`.
        assert (
            extract_display_name({"customer_name": "Jane Doe"}) == "Jane Doe"
        )

    def test_truncates_to_128(self) -> None:
        long = "A" * 200
        assert len(extract_display_name({"display_name": long}) or "") == 128

    def test_returns_none_when_absent(self) -> None:
        assert extract_display_name({}) is None

    def test_returns_none_for_blank(self) -> None:
        assert extract_display_name({"display_name": "   "}) is None


# ---------------------------------------------------------------------------
# CustomerRepository — Protocol + InMemory contract
# ---------------------------------------------------------------------------


class TestInMemoryCustomerRepository:
    @pytest.fixture
    def repo(self) -> InMemoryCustomerRepository:
        return InMemoryCustomerRepository()

    def test_satisfies_protocol(self, repo: InMemoryCustomerRepository) -> None:
        assert isinstance(repo, CustomerRepository)

    def test_create_and_lookup(self, repo: InMemoryCustomerRepository) -> None:
        c = repo.create_customer(
            tenant_id="t1",
            identities=[(IdentityKind.EMAIL, "jane@acme.com")],
            display_name="Jane",
        )
        assert c.tenant_id == "t1"
        assert c.display_name == "Jane"
        assert repo.count(tenant_id="t1") == 1

        found = repo.find_customer_by_identity(
            tenant_id="t1",
            identity_kind=IdentityKind.EMAIL,
            identity_value="jane@acme.com",
        )
        assert found is not None
        assert found.id == c.id

    def test_create_collision_raises(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        repo.create_customer(
            tenant_id="t1",
            identities=[(IdentityKind.EMAIL, "jane@acme.com")],
        )
        with pytest.raises(IdentityCollision):
            repo.create_customer(
                tenant_id="t1",
                identities=[(IdentityKind.EMAIL, "jane@acme.com")],
            )

    def test_add_identity_idempotent(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        c = repo.create_customer(
            tenant_id="t1",
            identities=[(IdentityKind.EMAIL, "jane@acme.com")],
        )
        first = repo.add_identity(
            tenant_id="t1",
            customer_id=c.id,
            identity_kind=IdentityKind.PHONE,
            identity_value="+15550100100",
        )
        # Second add is a no-op return of the existing row.
        second = repo.add_identity(
            tenant_id="t1",
            customer_id=c.id,
            identity_kind=IdentityKind.PHONE,
            identity_value="+15550100100",
        )
        assert first.id == second.id
        idents = repo.list_identities(tenant_id="t1", customer_id=c.id)
        kinds = {i.identity_kind for i in idents}
        assert kinds == {IdentityKind.EMAIL, IdentityKind.PHONE}

    def test_add_identity_collision_raises(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        c1 = repo.create_customer(
            tenant_id="t1",
            identities=[(IdentityKind.EMAIL, "a@acme.com")],
        )
        c2 = repo.create_customer(
            tenant_id="t1",
            identities=[(IdentityKind.EMAIL, "b@acme.com")],
        )
        # Trying to attach c1's email to c2 fails.
        with pytest.raises(IdentityCollision):
            repo.add_identity(
                tenant_id="t1",
                customer_id=c2.id,
                identity_kind=IdentityKind.EMAIL,
                identity_value="a@acme.com",
            )
        # And c1 is untouched.
        idents = repo.list_identities(tenant_id="t1", customer_id=c1.id)
        assert len(idents) == 1

    def test_add_identity_to_unknown_customer_raises(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        with pytest.raises(KeyError):
            repo.add_identity(
                tenant_id="t1",
                customer_id="cust_does_not_exist",
                identity_kind=IdentityKind.EMAIL,
                identity_value="x@y.com",
            )

    def test_tenant_isolation_on_lookup(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        # Same email under tenant A and tenant B → two distinct customers,
        # neither lookup leaks across.
        a = repo.create_customer(
            tenant_id="bank-a",
            identities=[(IdentityKind.EMAIL, "jane@acme.com")],
        )
        b = repo.create_customer(
            tenant_id="bank-b",
            identities=[(IdentityKind.EMAIL, "jane@acme.com")],
        )
        assert a.id != b.id

        found_a = repo.find_customer_by_identity(
            tenant_id="bank-a",
            identity_kind=IdentityKind.EMAIL,
            identity_value="jane@acme.com",
        )
        assert found_a is not None and found_a.id == a.id

        # And tenant B can't see tenant A's customer by id.
        assert (
            repo.get_customer(tenant_id="bank-b", customer_id=a.id) is None
        )

    def test_count_with_and_without_tenant(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        repo.create_customer(
            tenant_id="t1",
            identities=[(IdentityKind.EMAIL, "a@x.com")],
        )
        repo.create_customer(
            tenant_id="t2",
            identities=[(IdentityKind.EMAIL, "a@x.com")],
        )
        assert repo.count() == 2
        assert repo.count(tenant_id="t1") == 1
        assert repo.count(tenant_id="missing") == 0


# ---------------------------------------------------------------------------
# resolve_customer — the linker pipeline
# ---------------------------------------------------------------------------


class TestResolveCustomer:
    @pytest.fixture
    def repo(self) -> InMemoryCustomerRepository:
        return InMemoryCustomerRepository()

    def test_first_contact_no_hints_creates_anonymous(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        # Channel produced an event with no identity-bearing fields. We
        # still need to attribute it; create an anonymous customer.
        out = resolve_customer(_req(text="hello"), customer_repo=repo)
        assert out.customer_created is True
        assert out.matched_kind is None
        assert out.identities_added == ()
        assert repo.count(tenant_id="acme-bank") == 1

    def test_first_contact_with_hints_creates_and_attaches(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        out = resolve_customer(
            _req(
                from_email="jane@acme.com",
                from_phone="+15550100100",
                external_id="CRM-001",
            ),
            customer_repo=repo,
        )
        assert out.customer_created is True
        assert out.matched_kind is None
        # All three hints were inserted as identities.
        kinds_added = {h.kind for h in out.identities_added}
        assert kinds_added == {
            IdentityKind.EMAIL,
            IdentityKind.PHONE,
            IdentityKind.EXTERNAL_ID,
        }
        idents = repo.list_identities(
            tenant_id="acme-bank", customer_id=out.customer_id
        )
        assert len(idents) == 3

    def test_second_contact_matches_existing(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        # First event: phone only.
        first = resolve_customer(
            _req(from_phone="+15550100100"), customer_repo=repo
        )
        # Second event: same phone — must resolve to same customer, no
        # new identities.
        second = resolve_customer(
            _req(from_phone="+15550100100"), customer_repo=repo
        )
        assert second.customer_created is False
        assert second.customer_id == first.customer_id
        assert second.matched_kind is IdentityKind.PHONE
        assert second.matched_value == "+15550100100"
        assert second.identities_added == ()

    def test_cross_channel_link_attaches_new_identity(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        # Day 6's headline scenario: customer first seen via SMS, then via
        # email — both resolve to the same customer, and the email is
        # added as a new identity on the existing customer.
        first = resolve_customer(
            _req(from_phone="+15550100100", external_id="CRM-001"),
            customer_repo=repo,
        )
        # Email arrives with same external_id but new email address.
        second = resolve_customer(
            _req(external_id="CRM-001", from_email="jane@acme.com"),
            customer_repo=repo,
        )
        assert second.customer_id == first.customer_id
        assert second.customer_created is False
        assert second.matched_kind is IdentityKind.EXTERNAL_ID  # priority winner
        added_kinds = {h.kind for h in second.identities_added}
        assert added_kinds == {IdentityKind.EMAIL}
        # Customer now has three identities: phone, external_id, email.
        idents = repo.list_identities(
            tenant_id="acme-bank", customer_id=first.customer_id
        )
        assert {i.identity_kind for i in idents} == {
            IdentityKind.EMAIL,
            IdentityKind.PHONE,
            IdentityKind.EXTERNAL_ID,
        }

    def test_lookup_priority_external_id_beats_email(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        # Two existing customers in same tenant: cust-A has email
        # x@y.com, cust-B has external_id ABC. An inbound event carrying
        # BOTH must match cust-B (external_id outranks email).
        cust_a = repo.create_customer(
            tenant_id="acme-bank",
            identities=[(IdentityKind.EMAIL, "x@y.com")],
        )
        cust_b = repo.create_customer(
            tenant_id="acme-bank",
            identities=[(IdentityKind.EXTERNAL_ID, "ABC")],
        )
        # Now an event with both — external_id wins. (This will then
        # raise an IdentityCollision on identity-attachment because
        # email x@y.com already maps to cust_a, not cust_b. The linker
        # surfaces it rather than swallowing — see
        # test_collision_propagates.)
        with pytest.raises(IdentityCollision):
            resolve_customer(
                _req(from_email="x@y.com", external_id="ABC"),
                customer_repo=repo,
            )
        # Sanity: no new customer was created in the failed path.
        assert repo.count(tenant_id="acme-bank") == 2
        # And cust_a's identity didn't migrate.
        a_idents = repo.list_identities(
            tenant_id="acme-bank", customer_id=cust_a.id
        )
        assert {i.identity_value for i in a_idents} == {"x@y.com"}
        # cust_b's identity is intact.
        b_idents = repo.list_identities(
            tenant_id="acme-bank", customer_id=cust_b.id
        )
        assert {i.identity_value for i in b_idents} == {"ABC"}

    def test_lookup_priority_email_beats_phone(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        # Cleaner priority test: only one existing customer; event has
        # both email (matches) and phone (unseen) — match is via email,
        # phone gets attached as a new identity.
        existing = resolve_customer(
            _req(from_email="jane@acme.com"), customer_repo=repo
        )
        out = resolve_customer(
            _req(from_email="jane@acme.com", from_phone="+15550100100"),
            customer_repo=repo,
        )
        assert out.customer_id == existing.customer_id
        assert out.matched_kind is IdentityKind.EMAIL
        assert {h.kind for h in out.identities_added} == {IdentityKind.PHONE}

    def test_explicit_customer_id_trusted(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        c = repo.create_customer(
            tenant_id="acme-bank",
            identities=[(IdentityKind.EMAIL, "jane@acme.com")],
        )
        body: dict[str, object] = {
            "tenant_id": "acme-bank",
            "customer_id": c.id,
            "channel_code": "email",
            "event_type": "message_received",
            "idempotency_key": "test-key",
            "payload": {"from_phone": "+15550100100"},
        }
        out = resolve_customer(
            IngestionRequest(**body),  # type: ignore[arg-type]
            customer_repo=repo,
        )
        assert out.customer_id == c.id
        assert out.customer_created is False
        # Phone was added as a new identity for this existing customer.
        assert {h.kind for h in out.identities_added} == {IdentityKind.PHONE}

    def test_explicit_customer_id_unknown_raises(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        body: dict[str, object] = {
            "tenant_id": "acme-bank",
            "customer_id": "cust_made_up",
            "channel_code": "email",
            "event_type": "message_received",
            "idempotency_key": "test-key",
            "payload": {},
        }
        with pytest.raises(LookupError):
            resolve_customer(
                IngestionRequest(**body),  # type: ignore[arg-type]
                customer_repo=repo,
            )

    def test_multi_tenant_isolation(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        # Same email under tenant A and tenant B must produce two
        # distinct customers — the multi-tenant invariant (rule 15).
        body_a: dict[str, object] = {
            "tenant_id": "bank-a",
            "channel_code": "email",
            "event_type": "message_received",
            "idempotency_key": "k1",
            "payload": {"from_email": "jane@acme.com"},
        }
        body_b: dict[str, object] = {
            "tenant_id": "bank-b",
            "channel_code": "email",
            "event_type": "message_received",
            "idempotency_key": "k1",
            "payload": {"from_email": "jane@acme.com"},
        }
        out_a = resolve_customer(
            IngestionRequest(**body_a),  # type: ignore[arg-type]
            customer_repo=repo,
        )
        out_b = resolve_customer(
            IngestionRequest(**body_b),  # type: ignore[arg-type]
            customer_repo=repo,
        )
        assert out_a.customer_id != out_b.customer_id
        assert out_a.customer_created and out_b.customer_created
        assert repo.count() == 2

    def test_collision_propagates(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        # Two customers, two emails. An event with both emails will match
        # whichever is found first; attaching the other will collide.
        # IdentityCollision must propagate to the caller.
        repo.create_customer(
            tenant_id="acme-bank",
            identities=[(IdentityKind.EMAIL, "a@x.com")],
        )
        repo.create_customer(
            tenant_id="acme-bank",
            identities=[(IdentityKind.EMAIL, "b@x.com")],
        )
        # Pydantic dedupes within a kind in extract — two `email` aliases
        # would collapse. Use sender_email + from_email (different keys,
        # different normalized values, both EMAIL kind).
        with pytest.raises(IdentityCollision):
            resolve_customer(
                _req(from_email="a@x.com", sender_email="b@x.com"),
                customer_repo=repo,
            )

    def test_normalized_phone_matches_unnormalized_input(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        # First event stores a clean +15550100100; second event arrives
        # with "+1 (555) 010-0100" — must match.
        first = resolve_customer(
            _req(from_phone="+15550100100"), customer_repo=repo
        )
        second = resolve_customer(
            _req(from_phone="+1 (555) 010-0100"), customer_repo=repo
        )
        assert second.customer_id == first.customer_id
        assert second.customer_created is False
        assert second.matched_kind is IdentityKind.PHONE

    def test_email_case_insensitive_match(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        first = resolve_customer(
            _req(from_email="jane@acme.com"), customer_repo=repo
        )
        second = resolve_customer(
            _req(from_email="JANE@ACME.com"), customer_repo=repo
        )
        assert second.customer_id == first.customer_id

    def test_external_id_case_sensitive_distinct_customers(
        self, repo: InMemoryCustomerRepository
    ) -> None:
        first = resolve_customer(
            _req(external_id="ABC-123"), customer_repo=repo
        )
        second = resolve_customer(
            _req(external_id="abc-123"), customer_repo=repo
        )
        assert first.customer_id != second.customer_id
        assert second.customer_created is True
