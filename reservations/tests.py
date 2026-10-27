from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from datetime import timedelta
from rest_framework.test import APIClient

from listings.models import ListingStatus, ResourceListing
from shared.enums import Mode, ResourceType, Unit
from .models import Reservation, ReservationStatus, SourceType

User = get_user_model()


def make_user(email, **extra):
    return User.objects.create_user(
        email=email, password="pass12345", display_name=email.split("@")[0], **extra
    )


def make_listing(owner, **overrides):
    defaults = dict(
        owner=owner,
        created_by=owner,
        resource_type=ResourceType.PACKAGED_FOOD_SURPLUS,
        title="Surplus Ricebox",
        category="prepared_meal",
        description="Fresh ricebox surplus.",
        quantity_total=Decimal("20"),
        unit=Unit.BOX,
        mode=Mode.DONATE,
        unit_price=Decimal("0"),
        condition="good",
        suitability=["HUMAN_CONSUMPTION"],
        location={"label": "Kampus UI Depok", "area": "Depok"},
        available_until=timezone.now() + timedelta(days=3),
        status=ListingStatus.AVAILABLE,
    )
    defaults.update(overrides)
    return ResourceListing.objects.create(**defaults)


def error_codes(response):
    return [e["code"] for e in response.data["errors"]]


class ListingClaimTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.provider = make_user("provider@example.com")
        self.receiver = make_user("receiver@example.com")
        self.listing = make_listing(self.provider)

    def url(self, pk):
        return f"/api/v1/listings/{pk}/claims/"

    def auth(self, user):
        self.client.force_authenticate(user)

    # --- Positive ---------------------------------------------------------
    def test_claim_creates_pending_reservation(self):
        self.auth(self.receiver)
        res = self.client.post(self.url(self.listing.pk), {"quantity": "5"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data["success"])
        data = res.data["data"]
        self.assertEqual(data["status"], ReservationStatus.PENDING)
        self.assertEqual(data["source_type"], SourceType.LISTING_CLAIM)
        self.assertEqual(data["listing_id"], self.listing.pk)
        self.assertIsNone(data["request_id"])
        self.assertEqual(data["provider"]["id"], self.provider.id)
        self.assertEqual(data["provider"]["display_name"], self.provider.display_name)
        self.assertEqual(data["receiver"]["id"], self.receiver.id)
        self.assertEqual(data["submitted_by_id"], self.receiver.id)
        self.assertIsNone(data["accepted_at"])
        self.assertIsNone(data["accepted_snapshot"])
        self.assertIsNone(data["pickup"])
        self.assertIsNone(data["counterparty_contact"])
        # terms copied from listing.
        terms = data["terms"]
        self.assertEqual(terms["unit"], self.listing.unit)
        self.assertEqual(terms["mode"], self.listing.mode)
        self.assertEqual(Decimal(terms["unit_price"]), self.listing.unit_price)
        self.assertEqual(Decimal(terms["quantity"]), Decimal("5"))
        self.assertEqual(terms["title"], self.listing.title)
        self.assertEqual(terms["category"], self.listing.category)

    def test_partial_claim_allowed(self):
        self.auth(self.receiver)
        res = self.client.post(self.url(self.listing.pk), {"quantity": "5"}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Decimal(res.data["data"]["terms"]["quantity"]), Decimal("5"))

    def test_pending_claim_does_not_allocate_stock(self):
        # Two receivers may both pend the full quantity while PENDING.
        other = make_user("other@example.com")
        self.auth(self.receiver)
        r1 = self.client.post(self.url(self.listing.pk), {"quantity": "20"}, format="json")
        self.assertEqual(r1.status_code, 201)
        self.auth(other)
        r2 = self.client.post(self.url(self.listing.pk), {"quantity": "20"}, format="json")
        self.assertEqual(r2.status_code, 201, r2.data)

    # --- Client cannot forge protected fields -----------------------------
    def test_client_cannot_override_price_owner_status(self):
        self.auth(self.receiver)
        res = self.client.post(
            self.url(self.listing.pk),
            {
                "quantity": "3",
                "unit_price": "99999",
                "provider": self.receiver.id,
                "status": ReservationStatus.ACCEPTED,
                "mode": Mode.SELL,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        data = res.data["data"]
        self.assertEqual(Decimal(data["terms"]["unit_price"]), self.listing.unit_price)
        self.assertEqual(data["provider"]["id"], self.provider.id)
        self.assertEqual(data["status"], ReservationStatus.PENDING)
        self.assertEqual(data["terms"]["mode"], self.listing.mode)

    # --- Negative: quantity ----------------------------------------------
    def test_quantity_must_be_positive(self):
        self.auth(self.receiver)
        res = self.client.post(self.url(self.listing.pk), {"quantity": "0"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_quantity_exceeding_available_rejected(self):
        self.auth(self.receiver)
        res = self.client.post(self.url(self.listing.pk), {"quantity": "21"}, format="json")
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("insufficient_quantity", error_codes(res))

    def test_quantity_against_latest_available_after_acceptance(self):
        # An ACCEPTED reservation reduces availability to 15; a claim of 16 fails.
        Reservation.objects.create(
            source_type=SourceType.LISTING_CLAIM,
            listing=self.listing,
            provider=self.provider,
            receiver=make_user("early@example.com"),
            submitted_by=self.provider,
            quantity=Decimal("5"),
            unit=self.listing.unit,
            mode=self.listing.mode,
            unit_price=self.listing.unit_price,
            resource_deadline=self.listing.available_until,
            status=ReservationStatus.ACCEPTED,
        )
        self.auth(self.receiver)
        over = self.client.post(self.url(self.listing.pk), {"quantity": "16"}, format="json")
        self.assertEqual(over.status_code, 409, over.data)
        self.assertIn("insufficient_quantity", error_codes(over))
        ok = self.client.post(self.url(self.listing.pk), {"quantity": "15"}, format="json")
        self.assertEqual(ok.status_code, 201, ok.data)

    def test_fractional_quantity_rejected_for_integer_unit(self):
        self.auth(self.receiver)
        res = self.client.post(self.url(self.listing.pk), {"quantity": "2.5"}, format="json")
        self.assertEqual(res.status_code, 400, res.data)

    def test_fractional_quantity_allowed_for_kg(self):
        kg_listing = make_listing(self.provider, unit=Unit.KG, quantity_total=Decimal("10"))
        self.auth(self.receiver)
        res = self.client.post(self.url(kg_listing.pk), {"quantity": "2.5"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(Decimal(res.data["data"]["terms"]["quantity"]), Decimal("2.5"))

    def test_precision_beyond_three_decimals_rejected(self):
        kg_listing = make_listing(self.provider, unit=Unit.KG, quantity_total=Decimal("10"))
        self.auth(self.receiver)
        res = self.client.post(self.url(kg_listing.pk), {"quantity": "2.5001"}, format="json")
        self.assertEqual(res.status_code, 400, res.data)

    # --- Negative: self-claim --------------------------------------------
    def test_self_claim_forbidden(self):
        self.auth(self.provider)
        res = self.client.post(self.url(self.listing.pk), {"quantity": "1"}, format="json")
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("self_transaction", error_codes(res))
        self.assertFalse(Reservation.objects.exists())

    # --- Negative: source status / expiry --------------------------------
    def test_claim_on_non_available_listing_rejected(self):
        self.listing.status = ListingStatus.CLOSED
        self.listing.save(update_fields=["status"])
        self.auth(self.receiver)
        res = self.client.post(self.url(self.listing.pk), {"quantity": "1"}, format="json")
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("source_not_accepting", error_codes(res))

    def test_claim_past_deadline_rejected(self):
        self.listing.available_until = timezone.now() - timedelta(hours=1)
        self.listing.save(update_fields=["available_until"])
        self.auth(self.receiver)
        res = self.client.post(self.url(self.listing.pk), {"quantity": "1"}, format="json")
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("source_not_accepting", error_codes(res))

    def test_claim_missing_listing_returns_404(self):
        self.auth(self.receiver)
        res = self.client.post(self.url(999999), {"quantity": "1"}, format="json")
        self.assertEqual(res.status_code, 404)

    # --- Permission -------------------------------------------------------
    def test_anonymous_claim_unauthorized(self):
        res = self.client.post(self.url(self.listing.pk), {"quantity": "1"}, format="json")
        self.assertEqual(res.status_code, 401)

    # --- Side effects / counters unchanged -------------------------------
    def test_failed_claim_creates_no_reservation(self):
        self.auth(self.receiver)
        self.client.post(self.url(self.listing.pk), {"quantity": "21"}, format="json")
        self.assertEqual(Reservation.objects.count(), 0)

    def test_pending_claim_does_not_change_listing_fields(self):
        self.auth(self.receiver)
        before_qty = self.listing.quantity_total
        before_status = self.listing.status
        self.client.post(self.url(self.listing.pk), {"quantity": "5"}, format="json")
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.quantity_total, before_qty)
        self.assertEqual(self.listing.status, before_status)

    def test_sell_listing_copies_price_and_total(self):
        sell_listing = make_listing(
            self.provider, mode=Mode.SELL, unit_price=Decimal("5000.00")
        )
        self.auth(self.receiver)
        res = self.client.post(self.url(sell_listing.pk), {"quantity": "2"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        terms = res.data["data"]["terms"]
        self.assertEqual(terms["mode"], Mode.SELL)
        self.assertEqual(Decimal(terms["unit_price"]), Decimal("5000.00"))
        self.assertEqual(Decimal(terms["total_price"]), Decimal("10000.00"))


from requests_app.models import RequestStatus, ResourceRequest


def make_request(owner, **overrides):
    defaults = dict(
        owner=owner,
        created_by=owner,
        title="Need ricebox",
        description="For a charity event.",
        resource_type=ResourceType.PACKAGED_FOOD_SURPLUS,
        category="prepared_meal",
        intended_use="HUMAN_CONSUMPTION",
        quantity_needed=Decimal("20"),
        unit=Unit.BOX,
        location={"label": "Kampus UI Depok", "area": "Depok"},
        donate_only=False,
        max_unit_price=None,
        valid_until=timezone.now() + timedelta(days=2),
        status=RequestStatus.OPEN,
    )
    defaults.update(overrides)
    return ResourceRequest.objects.create(**defaults)


def offer_payload(**overrides):
    payload = dict(
        resource_type=ResourceType.PACKAGED_FOOD_SURPLUS,
        title="Rice box tersedia",
        category="prepared_meal",
        description="Nasi ayam dalam kemasan box.",
        unit=Unit.BOX,
        mode=Mode.DONATE,
        unit_price="0.00",
        condition="Baru dikemas",
        suitability=["HUMAN_CONSUMPTION"],
        location={"label": "Kampus UI Depok", "area": "Depok", "latitude": -6.36, "longitude": 106.82},
        quantity="5",
        resource_deadline=(timezone.now() + timedelta(days=1)).isoformat(),
    )
    payload.update(overrides)
    return payload


class RequestOfferTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.receiver = make_user("req_owner@example.com")  # request owner
        self.provider = make_user("provider2@example.com")  # offering user
        self.request = make_request(self.receiver)

    def url(self, pk):
        return f"/api/v1/requests/{pk}/offers/"

    def auth(self, user):
        self.client.force_authenticate(user)

    # --- Positive ---------------------------------------------------------
    def test_offer_creates_pending_reservation(self):
        self.auth(self.provider)
        res = self.client.post(self.url(self.request.pk), offer_payload(), format="json")
        self.assertEqual(res.status_code, 201, res.data)
        data = res.data["data"]
        self.assertEqual(data["status"], ReservationStatus.PENDING)
        self.assertEqual(data["source_type"], SourceType.REQUEST_OFFER)
        self.assertEqual(data["request_id"], self.request.pk)
        self.assertIsNone(data["listing_id"])
        self.assertEqual(data["provider"]["id"], self.provider.id)
        self.assertEqual(data["receiver"]["id"], self.receiver.id)
        self.assertEqual(data["submitted_by_id"], self.provider.id)
        terms = data["terms"]
        self.assertEqual(terms["title"], "Rice box tersedia")
        self.assertEqual(terms["category"], "prepared_meal")
        self.assertEqual(Decimal(terms["quantity"]), Decimal("5"))
        self.assertEqual(terms["mode"], Mode.DONATE)
        self.assertEqual(Decimal(terms["unit_price"]), Decimal("0.00"))

    def test_sell_offer_within_max_price(self):
        req = make_request(self.receiver, max_unit_price=Decimal("5000.00"))
        self.auth(self.provider)
        res = self.client.post(
            self.url(req.pk),
            offer_payload(mode=Mode.SELL, unit_price="5000.00"),
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        terms = res.data["data"]["terms"]
        self.assertEqual(Decimal(terms["total_price"]), Decimal("25000.00"))

    def test_pending_offer_does_not_reduce_remaining(self):
        other = make_user("provider3@example.com")
        self.auth(self.provider)
        r1 = self.client.post(self.url(self.request.pk), offer_payload(quantity="20"), format="json")
        self.assertEqual(r1.status_code, 201, r1.data)
        self.auth(other)
        r2 = self.client.post(self.url(self.request.pk), offer_payload(quantity="20"), format="json")
        self.assertEqual(r2.status_code, 201, r2.data)

    # --- Negative: payload / precision -----------------------------------
    def test_missing_required_field_rejected(self):
        self.auth(self.provider)
        payload = offer_payload()
        del payload["resource_deadline"]
        res = self.client.post(self.url(self.request.pk), payload, format="json")
        self.assertEqual(res.status_code, 400, res.data)

    def test_fractional_quantity_rejected_for_integer_unit(self):
        self.auth(self.provider)
        res = self.client.post(self.url(self.request.pk), offer_payload(quantity="2.5"), format="json")
        self.assertEqual(res.status_code, 400, res.data)

    def test_fractional_quantity_allowed_for_kg(self):
        req = make_request(
            self.receiver,
            resource_type=ResourceType.COMPOSTABLE_FOOD_WASTE,
            category="coffee_grounds",
            intended_use="COMPOSTING",
            unit=Unit.KG,
            quantity_needed=Decimal("10"),
        )
        self.auth(self.provider)
        res = self.client.post(
            self.url(req.pk),
            offer_payload(
                resource_type=ResourceType.COMPOSTABLE_FOOD_WASTE,
                category="coffee_grounds",
                unit=Unit.KG,
                suitability=["COMPOSTING", "BSF"],
                quantity="2.5",
            ),
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)

    def test_resource_deadline_must_be_future(self):
        self.auth(self.provider)
        past = (timezone.now() - timedelta(hours=1)).isoformat()
        res = self.client.post(
            self.url(self.request.pk), offer_payload(resource_deadline=past), format="json"
        )
        self.assertEqual(res.status_code, 400, res.data)

    def test_incomplete_location_rejected(self):
        self.auth(self.provider)
        res = self.client.post(
            self.url(self.request.pk),
            offer_payload(location={"label": "Somewhere", "area": "Depok"}),
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)
        fields = {e["field"] for e in res.data["errors"]}
        self.assertIn("location.latitude", fields)
        self.assertIn("location.longitude", fields)

    def test_out_of_range_latitude_rejected(self):
        self.auth(self.provider)
        res = self.client.post(
            self.url(self.request.pk),
            offer_payload(location={"label": "X", "area": "Depok", "latitude": 999, "longitude": 0}),
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.data)

    # --- Negative: compatibility -----------------------------------------
    def test_resource_type_mismatch_rejected(self):
        self.auth(self.provider)
        res = self.client.post(
            self.url(self.request.pk),
            offer_payload(resource_type=ResourceType.COMPOSTABLE_FOOD_WASTE, suitability=["COMPOSTING"]),
            format="json",
        )
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("terms_mismatch", error_codes(res))

    def test_category_mismatch_rejected(self):
        self.auth(self.provider)
        res = self.client.post(
            self.url(self.request.pk), offer_payload(category="bakery"), format="json"
        )
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("terms_mismatch", error_codes(res))

    def test_unit_mismatch_rejected(self):
        self.auth(self.provider)
        res = self.client.post(
            self.url(self.request.pk), offer_payload(unit=Unit.KG), format="json"
        )
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("terms_mismatch", error_codes(res))

    def test_suitability_must_contain_intended_use(self):
        req = make_request(
            self.receiver,
            resource_type=ResourceType.COMPOSTABLE_FOOD_WASTE,
            category="coffee_grounds",
            intended_use="BSF",
            unit=Unit.KG,
        )
        self.auth(self.provider)
        res = self.client.post(
            self.url(req.pk),
            offer_payload(
                resource_type=ResourceType.COMPOSTABLE_FOOD_WASTE,
                category="coffee_grounds",
                unit=Unit.KG,
                suitability=["COMPOSTING"],  # missing BSF
            ),
            format="json",
        )
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("terms_mismatch", error_codes(res))

    def test_consumption_resource_requires_human_consumption_only(self):
        self.auth(self.provider)
        res = self.client.post(
            self.url(self.request.pk),
            offer_payload(suitability=["HUMAN_CONSUMPTION", "COMPOSTING"]),
            format="json",
        )
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("terms_mismatch", error_codes(res))

    # --- Negative: price / mode ------------------------------------------
    def test_donate_only_request_forbids_sell(self):
        req = make_request(self.receiver, donate_only=True)
        self.auth(self.provider)
        res = self.client.post(
            self.url(req.pk),
            offer_payload(mode=Mode.SELL, unit_price="1000.00"),
            format="json",
        )
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("terms_mismatch", error_codes(res))

    def test_sell_price_exceeding_max_rejected(self):
        req = make_request(self.receiver, max_unit_price=Decimal("5000.00"))
        self.auth(self.provider)
        res = self.client.post(
            self.url(req.pk),
            offer_payload(mode=Mode.SELL, unit_price="6000.00"),
            format="json",
        )
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("terms_mismatch", error_codes(res))

    # --- Negative: quantity / remaining ----------------------------------
    def test_offer_exceeding_remaining_rejected(self):
        self.auth(self.provider)
        res = self.client.post(self.url(self.request.pk), offer_payload(quantity="21"), format="json")
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("insufficient_quantity", error_codes(res))

    def test_remaining_after_acceptance(self):
        Reservation.objects.create(
            source_type=SourceType.REQUEST_OFFER,
            request=self.request,
            provider=make_user("early_provider@example.com"),
            receiver=self.receiver,
            submitted_by=self.provider,
            quantity=Decimal("5"),
            unit=self.request.unit,
            mode=Mode.DONATE,
            unit_price=Decimal("0"),
            resource_deadline=timezone.now() + timedelta(days=1),
            status=ReservationStatus.ACCEPTED,
        )
        self.auth(self.provider)
        over = self.client.post(self.url(self.request.pk), offer_payload(quantity="16"), format="json")
        self.assertEqual(over.status_code, 409, over.data)
        ok = self.client.post(self.url(self.request.pk), offer_payload(quantity="15"), format="json")
        self.assertEqual(ok.status_code, 201, ok.data)

    # --- Negative: self-offer / source status / expiry -------------------
    def test_self_offer_forbidden(self):
        self.auth(self.receiver)
        res = self.client.post(self.url(self.request.pk), offer_payload(), format="json")
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("self_transaction", error_codes(res))
        self.assertFalse(Reservation.objects.exists())

    def test_offer_on_non_open_request_rejected(self):
        self.request.status = RequestStatus.CLOSED
        self.request.save(update_fields=["status"])
        self.auth(self.provider)
        res = self.client.post(self.url(self.request.pk), offer_payload(), format="json")
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("source_not_accepting", error_codes(res))

    def test_offer_past_valid_until_rejected(self):
        self.request.valid_until = timezone.now() - timedelta(hours=1)
        self.request.save(update_fields=["valid_until"])
        self.auth(self.provider)
        res = self.client.post(self.url(self.request.pk), offer_payload(), format="json")
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("source_not_accepting", error_codes(res))

    def test_offer_missing_request_returns_404(self):
        self.auth(self.provider)
        res = self.client.post(self.url(999999), offer_payload(), format="json")
        self.assertEqual(res.status_code, 404)

    # --- Permission -------------------------------------------------------
    def test_anonymous_offer_unauthorized(self):
        res = self.client.post(self.url(self.request.pk), offer_payload(), format="json")
        self.assertEqual(res.status_code, 401)

    # --- Side effects -----------------------------------------------------
    def test_failed_offer_creates_no_reservation(self):
        self.auth(self.provider)
        self.client.post(self.url(self.request.pk), offer_payload(quantity="21"), format="json")
        self.assertEqual(Reservation.objects.count(), 0)

    def test_offer_does_not_create_listing(self):
        from listings.models import ResourceListing
        self.auth(self.provider)
        self.client.post(self.url(self.request.pk), offer_payload(), format="json")
        self.assertEqual(ResourceListing.objects.count(), 0)


def make_admin(email):
    u = User.objects.create_user(email=email, password="pass12345", display_name="Admin")
    u.is_staff = True
    u.save(update_fields=["is_staff"])
    return u


def make_claim_reservation(listing, receiver, **overrides):
    """Create a LISTING_CLAIM reservation directly (submitter = receiver)."""
    defaults = dict(
        source_type=SourceType.LISTING_CLAIM,
        listing=listing,
        provider=listing.owner,
        receiver=receiver,
        submitted_by=receiver,
        quantity=Decimal("5"),
        unit=listing.unit,
        mode=listing.mode,
        unit_price=listing.unit_price,
        resource_deadline=listing.available_until,
        status=ReservationStatus.PENDING,
    )
    defaults.update(overrides)
    return Reservation.objects.create(**defaults)


class ReservationInboxTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.provider = make_user("inbox_provider@example.com")
        self.receiver = make_user("inbox_receiver@example.com")
        self.outsider = make_user("inbox_outsider@example.com")
        self.listing = make_listing(self.provider)
        self.res = make_claim_reservation(self.listing, self.receiver)

    def auth(self, user):
        self.client.force_authenticate(user)

    # --- list scoping -----------------------------------------------------
    def test_list_shows_only_own_reservations(self):
        self.auth(self.receiver)
        res = self.client.get("/api/v1/reservations/")
        self.assertEqual(res.status_code, 200, res.data)
        results = res.data["data"]["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], self.res.pk)

    def test_outsider_sees_empty_list(self):
        self.auth(self.outsider)
        res = self.client.get("/api/v1/reservations/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["count"], 0)

    def test_list_requires_auth(self):
        self.assertEqual(self.client.get("/api/v1/reservations/").status_code, 401)

    def test_role_filter(self):
        self.auth(self.receiver)
        both = self.client.get("/api/v1/reservations/?role=receiver")
        self.assertEqual(both.data["data"]["count"], 1)
        none = self.client.get("/api/v1/reservations/?role=provider")
        self.assertEqual(none.data["data"]["count"], 0)

    def test_status_and_source_filters(self):
        self.auth(self.receiver)
        self.assertEqual(
            self.client.get("/api/v1/reservations/?status=PENDING").data["data"]["count"], 1
        )
        self.assertEqual(
            self.client.get("/api/v1/reservations/?status=ACCEPTED").data["data"]["count"], 0
        )
        self.assertEqual(
            self.client.get("/api/v1/reservations/?source_type=LISTING_CLAIM").data["data"]["count"], 1
        )

    def test_invalid_filter_rejected(self):
        self.auth(self.receiver)
        self.assertEqual(self.client.get("/api/v1/reservations/?role=bogus").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/reservations/?status=BOGUS").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/reservations/?source_type=BOGUS").status_code, 400)

    def test_ordering_newest_first(self):
        second = make_claim_reservation(self.listing, self.receiver)
        self.auth(self.receiver)
        results = self.client.get("/api/v1/reservations/").data["data"]["results"]
        self.assertEqual([r["id"] for r in results], [second.pk, self.res.pk])


class ReservationDetailTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.provider = make_user("det_provider@example.com", whatsapp_number="+628111111111")
        self.receiver = make_user("det_receiver@example.com", whatsapp_number="+628222222222")
        self.outsider = make_user("det_outsider@example.com")
        self.admin = make_admin("det_admin@example.com")
        self.listing = make_listing(self.provider)
        self.res = make_claim_reservation(self.listing, self.receiver)

    def auth(self, user):
        self.client.force_authenticate(user)

    def url(self, pk):
        return f"/api/v1/reservations/{pk}/"

    def test_participant_can_read(self):
        self.auth(self.receiver)
        res = self.client.get(self.url(self.res.pk))
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["data"]["id"], self.res.pk)

    def test_outsider_gets_404(self):
        self.auth(self.outsider)
        self.assertEqual(self.client.get(self.url(self.res.pk)).status_code, 404)

    def test_admin_non_participant_can_read(self):
        self.auth(self.admin)
        self.assertEqual(self.client.get(self.url(self.res.pk)).status_code, 200)

    def test_missing_reservation_404(self):
        self.auth(self.receiver)
        self.assertEqual(self.client.get(self.url(999999)).status_code, 404)

    def test_history_readable_in_terminal_status(self):
        self.res.status = ReservationStatus.REJECTED
        self.res.save(update_fields=["status"])
        self.auth(self.receiver)
        res = self.client.get(self.url(self.res.pk))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["status"], ReservationStatus.REJECTED)

    # --- contact privacy --------------------------------------------------
    def test_no_contact_while_pending(self):
        self.auth(self.receiver)
        res = self.client.get(self.url(self.res.pk))
        self.assertIsNone(res.data["data"]["counterparty_contact"])

    def test_contact_revealed_to_participant_when_accepted(self):
        self.res.status = ReservationStatus.ACCEPTED
        self.res.save(update_fields=["status"])
        self.auth(self.receiver)
        res = self.client.get(self.url(self.res.pk))
        contact = res.data["data"]["counterparty_contact"]
        # Receiver sees the provider's contact.
        self.assertIsNotNone(contact)
        self.assertEqual(contact["user_id"], self.provider.id)
        self.assertEqual(contact["whatsapp_number"], "+628111111111")

    def test_contact_null_when_other_number_missing(self):
        self.provider.whatsapp_number = None
        self.provider.save(update_fields=["whatsapp_number"])
        self.res.status = ReservationStatus.ACCEPTED
        self.res.save(update_fields=["status"])
        self.auth(self.receiver)
        res = self.client.get(self.url(self.res.pk))
        self.assertIsNone(res.data["data"]["counterparty_contact"])

    def test_non_participant_admin_gets_null_contact(self):
        self.res.status = ReservationStatus.ACCEPTED
        self.res.save(update_fields=["status"])
        self.auth(self.admin)
        res = self.client.get(self.url(self.res.pk))
        self.assertIsNone(res.data["data"]["counterparty_contact"])

    def test_pickup_null_without_pickup(self):
        self.auth(self.receiver)
        res = self.client.get(self.url(self.res.pk))
        self.assertIsNone(res.data["data"]["pickup"])


class ReservationReviseTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.provider = make_user("rev_provider@example.com")
        self.receiver = make_user("rev_receiver@example.com")
        self.outsider = make_user("rev_outsider@example.com")
        self.listing = make_listing(self.provider, quantity_total=Decimal("20"))
        self.res = make_claim_reservation(self.listing, self.receiver, quantity=Decimal("5"))

    def auth(self, user):
        self.client.force_authenticate(user)

    def url(self, pk):
        return f"/api/v1/reservations/{pk}/"

    def patch(self, pk, body):
        return self.client.patch(self.url(pk), body, format="json")

    # --- success ----------------------------------------------------------
    def test_submitter_revises_quantity_and_version_bumps(self):
        self.auth(self.receiver)
        res = self.patch(self.res.pk, {"quantity": "8", "expected_version": 1})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(Decimal(res.data["data"]["terms"]["quantity"]), Decimal("8"))
        self.assertEqual(res.data["data"]["version"], 2)
        self.res.refresh_from_db()
        self.assertEqual(self.res.quantity, Decimal("8"))
        self.assertEqual(self.res.version, 2)

    # --- permission -------------------------------------------------------
    def test_non_submitter_participant_forbidden(self):
        # Provider is the approver here, not the submitter.
        self.auth(self.provider)
        res = self.patch(self.res.pk, {"quantity": "8", "expected_version": 1})
        self.assertEqual(res.status_code, 403, res.data)

    def test_outsider_gets_404(self):
        self.auth(self.outsider)
        res = self.patch(self.res.pk, {"quantity": "8", "expected_version": 1})
        self.assertEqual(res.status_code, 404)

    def test_revise_requires_auth(self):
        res = self.patch(self.res.pk, {"quantity": "8", "expected_version": 1})
        self.assertEqual(res.status_code, 401)

    # --- expected_version / stale ----------------------------------------
    def test_expected_version_required(self):
        self.auth(self.receiver)
        res = self.patch(self.res.pk, {"quantity": "8"})
        self.assertEqual(res.status_code, 400, res.data)

    def test_stale_version_rejected(self):
        self.auth(self.receiver)
        res = self.patch(self.res.pk, {"quantity": "8", "expected_version": 99})
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("stale_version", error_codes(res))

    # --- invalid transition ----------------------------------------------
    def test_cannot_revise_non_pending(self):
        self.res.status = ReservationStatus.ACCEPTED
        self.res.save(update_fields=["status"])
        self.auth(self.receiver)
        res = self.patch(self.res.pk, {"quantity": "8", "expected_version": 1})
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("invalid_transition", error_codes(res))

    # --- immutability -----------------------------------------------------
    def test_price_and_parties_not_revisable(self):
        self.auth(self.receiver)
        res = self.patch(
            self.res.pk,
            {
                "quantity": "6",
                "expected_version": 1,
                "unit_price": "9999",
                "mode": "SELL",
                "provider": self.receiver.id,
            },
        )
        self.assertEqual(res.status_code, 200, res.data)
        data = res.data["data"]
        self.assertEqual(Decimal(data["terms"]["unit_price"]), self.listing.unit_price)
        self.assertEqual(data["terms"]["mode"], self.listing.mode)
        self.assertEqual(data["provider"]["id"], self.provider.id)

    # --- precision / availability -----------------------------------------
    def test_fractional_rejected_for_integer_unit(self):
        self.auth(self.receiver)
        res = self.patch(self.res.pk, {"quantity": "2.5", "expected_version": 1})
        self.assertEqual(res.status_code, 400, res.data)

    def test_revised_quantity_exceeding_available_rejected(self):
        self.auth(self.receiver)
        res = self.patch(self.res.pk, {"quantity": "21", "expected_version": 1})
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("insufficient_quantity", error_codes(res))

    def test_revise_pending_does_not_allocate(self):
        self.auth(self.receiver)
        self.patch(self.res.pk, {"quantity": "8", "expected_version": 1})
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.quantity_total, Decimal("20"))
        self.assertEqual(self.listing.status, ListingStatus.AVAILABLE)


class ReservationAcceptTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.provider = make_user("acc_provider@example.com")
        self.receiver = make_user("acc_receiver@example.com")
        self.outsider = make_user("acc_outsider@example.com")
        self.listing = make_listing(self.provider, quantity_total=Decimal("20"))
        self.res = make_claim_reservation(self.listing, self.receiver, quantity=Decimal("5"))

    def auth(self, user):
        self.client.force_authenticate(user)

    def url(self, pk):
        return f"/api/v1/reservations/{pk}/accept/"

    def accept(self, pk, version=1):
        return self.client.post(self.url(pk), {"expected_version": version}, format="json")

    # --- success ----------------------------------------------------------
    def test_provider_accepts_claim(self):
        self.auth(self.provider)
        res = self.accept(self.res.pk, 1)
        self.assertEqual(res.status_code, 200, res.data)
        data = res.data["data"]
        self.assertEqual(data["status"], ReservationStatus.ACCEPTED)
        self.assertIsNotNone(data["accepted_at"])
        self.assertIsNotNone(data["accepted_snapshot"])
        self.assertEqual(data["version"], 2)
        self.res.refresh_from_db()
        self.assertEqual(self.res.status, ReservationStatus.ACCEPTED)

    def test_accept_partial_keeps_listing_available(self):
        self.auth(self.provider)
        self.accept(self.res.pk, 1)
        self.listing.refresh_from_db()
        # 5 of 20 allocated -> still AVAILABLE.
        self.assertEqual(self.listing.status, ListingStatus.AVAILABLE)

    def test_accept_full_quantity_reserves_listing(self):
        full = make_claim_reservation(self.listing, self.receiver, quantity=Decimal("20"))
        self.auth(self.provider)
        res = self.accept(full.pk, 1)
        self.assertEqual(res.status_code, 200, res.data)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, ListingStatus.RESERVED)

    def test_snapshot_immutable_against_source_edit(self):
        self.auth(self.provider)
        self.accept(self.res.pk, 1)
        self.res.refresh_from_db()
        snap_before = self.res.accepted_snapshot
        # Edit the listing after acceptance (switch to SELL with a new price;
        # DONATE requires a zero price, so change mode and price together).
        self.listing.title = "Changed title"
        self.listing.mode = Mode.SELL
        self.listing.unit_price = Decimal("777.00")
        self.listing.save(update_fields=["title", "mode", "unit_price"])
        self.auth(self.provider)
        res = self.client.get(f"/api/v1/reservations/{self.res.pk}/")
        self.assertEqual(res.data["data"]["accepted_snapshot"], snap_before)
        self.assertEqual(res.data["data"]["accepted_snapshot"]["title"], "Surplus Ricebox")

    # --- offer acceptance by receiver ------------------------------------
    def test_receiver_accepts_offer(self):
        req = make_request(self.receiver, quantity_needed=Decimal("10"))
        offer = Reservation.objects.create(
            source_type=SourceType.REQUEST_OFFER,
            request=req,
            provider=self.provider,
            receiver=self.receiver,
            submitted_by=self.provider,
            quantity=Decimal("10"),
            unit=req.unit,
            mode=Mode.DONATE,
            unit_price=Decimal("0"),
            resource_data={
                "resource_type": req.resource_type,
                "title": "Offer box",
                "category": req.category,
                "description": "d",
                "condition": "c",
                "suitability": ["HUMAN_CONSUMPTION"],
                "location": {"label": "x", "area": "Depok"},
                "storage_notes": None,
                "allergen_notes": None,
                "packaging_status": None,
            },
            resource_deadline=timezone.now() + timedelta(days=1),
            status=ReservationStatus.PENDING,
        )
        self.auth(self.receiver)
        res = self.accept(offer.pk, 1)
        self.assertEqual(res.status_code, 200, res.data)
        req.refresh_from_db()
        self.assertEqual(req.status, RequestStatus.ALLOCATED)

    # --- permission / wrong approver -------------------------------------
    def test_claim_receiver_cannot_accept(self):
        # Receiver is the submitter of a claim, not the approver.
        self.auth(self.receiver)
        res = self.accept(self.res.pk, 1)
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("invalid_transition", error_codes(res))

    def test_outsider_gets_404(self):
        self.auth(self.outsider)
        self.assertEqual(self.accept(self.res.pk, 1).status_code, 404)

    def test_accept_requires_auth(self):
        self.assertEqual(self.accept(self.res.pk, 1).status_code, 401)

    # --- expected_version -------------------------------------------------
    def test_expected_version_required(self):
        self.auth(self.provider)
        res = self.client.post(self.url(self.res.pk), {}, format="json")
        self.assertEqual(res.status_code, 400, res.data)

    def test_stale_version_rejected(self):
        self.auth(self.provider)
        res = self.accept(self.res.pk, 99)
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("stale_version", error_codes(res))

    # --- invalid transition ----------------------------------------------
    def test_cannot_accept_non_pending(self):
        self.res.status = ReservationStatus.REJECTED
        self.res.save(update_fields=["status"])
        self.auth(self.provider)
        res = self.accept(self.res.pk, 1)
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("invalid_transition", error_codes(res))

    # --- overselling guard (sequential proxy for concurrency) ------------
    def test_second_acceptance_exceeding_remaining_fails(self):
        a = make_claim_reservation(self.listing, self.receiver, quantity=Decimal("15"))
        b = make_claim_reservation(self.listing, make_user("acc_r2@example.com"), quantity=Decimal("15"))
        self.auth(self.provider)
        self.assertEqual(self.accept(a.pk, 1).status_code, 200)
        # 15 of 20 allocated; a second 15 would oversell.
        over = self.accept(b.pk, 1)
        self.assertEqual(over.status_code, 409, over.data)
        self.assertIn("insufficient_quantity", error_codes(over))

    def test_accept_rolls_back_on_failure(self):
        # Force failure via stale version; ensure nothing changed.
        self.auth(self.provider)
        self.accept(self.res.pk, 99)
        self.res.refresh_from_db()
        self.listing.refresh_from_db()
        self.assertEqual(self.res.status, ReservationStatus.PENDING)
        self.assertIsNone(self.res.accepted_at)
        self.assertEqual(self.listing.status, ListingStatus.AVAILABLE)

    # --- source status / deadline ----------------------------------------
    def test_accept_on_closed_listing_fails(self):
        self.listing.status = ListingStatus.CLOSED
        self.listing.save(update_fields=["status"])
        self.auth(self.provider)
        res = self.accept(self.res.pk, 1)
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("source_not_accepting", error_codes(res))

    def test_accept_past_deadline_fails(self):
        self.listing.available_until = timezone.now() - timedelta(hours=1)
        self.listing.save(update_fields=["available_until"])
        self.auth(self.provider)
        res = self.accept(self.res.pk, 1)
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("deadline_passed", error_codes(res))

    # --- offer terms re-check --------------------------------------------
    def test_offer_terms_rechecked_against_edited_request(self):
        req = make_request(self.receiver, quantity_needed=Decimal("10"), max_unit_price=Decimal("5000.00"))
        offer = Reservation.objects.create(
            source_type=SourceType.REQUEST_OFFER,
            request=req,
            provider=self.provider,
            receiver=self.receiver,
            submitted_by=self.provider,
            quantity=Decimal("5"),
            unit=req.unit,
            mode=Mode.SELL,
            unit_price=Decimal("5000.00"),
            resource_data={
                "resource_type": req.resource_type,
                "title": "Offer box",
                "category": req.category,
                "description": "d",
                "condition": "c",
                "suitability": ["HUMAN_CONSUMPTION"],
                "location": {"label": "x", "area": "Depok"},
                "storage_notes": None,
                "allergen_notes": None,
                "packaging_status": None,
            },
            resource_deadline=timezone.now() + timedelta(days=1),
            status=ReservationStatus.PENDING,
        )
        # Request later tightened to donate_only -> SELL offer no longer valid.
        req.donate_only = True
        req.max_unit_price = None
        req.save(update_fields=["donate_only", "max_unit_price"])
        self.auth(self.receiver)
        res = self.accept(offer.pk, 1)
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("terms_mismatch", error_codes(res))
