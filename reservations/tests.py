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
        location={"label": "Kampus UI Depok", "area": "Depok"},
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
