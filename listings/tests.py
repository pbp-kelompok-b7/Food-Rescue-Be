"""SL-BE-01 API contract tests: Technical Design Revisi 2, 7.2.1/7.2.3.

Run with PostgreSQL (JSON suitability containment is part of this contract):
    .venv/Scripts/python.exe manage.py test listings

Only GET behavior is covered here. Create/edit/close/delete/admin endpoints and
the periodic expiry worker belong to other tasks. Fixtures use the ORM so these
tests do not depend on the unfinished create/reservation/pickup endpoints.
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient

from pickups.models import Pickup, PickupStatus
from reservations.models import Reservation, ReservationStatus
from .models import ListingStatus, ResourceListing


class SupplyListingReadTests(TestCase):
    url = "/api/v1/listings/"
    now = datetime(2026, 10, 27, 5, 0, tzinfo=dt_timezone.utc)
    response_fields = {
        'image',
        "id", "created_at", "updated_at", "version", "resource_type", "title",
        "category", "description", "unit", "mode", "unit_price", "condition",
        "suitability", "location", "storage_notes", "allergen_notes",
        "packaging_status", "quantity_total", "available_until", "owner", "status",
        "quantity_available", "quantity_allocated", "quantity_completed", "archived_at",
    }

    @classmethod
    def setUpTestData(cls):
        user = get_user_model()
        cls.owner = user.objects.create_user(
            email="supply-owner@example.com", password="test-password",
            display_name="Provider", whatsapp_number="+628123456789",
        )
        cls.other = user.objects.create_user(
            email="supply-other@example.com", password="test-password",
            display_name="Other", whatsapp_number="+628987654321",
        )
        cls.receiver = user.objects.create_user(
            email="supply-receiver@example.com", display_name="Receiver",
        )
        cls.admin = user.objects.create_user(
            email="supply-admin@example.com", display_name="Admin", is_staff=True,
        )

    def setUp(self):
        self.client = APIClient(enforce_csrf_checks=True)
        timer = patch("django.utils.timezone.now", return_value=self.now)
        timer.start()
        self.addCleanup(timer.stop)

    def listing(self, **overrides):
        values = {
            "owner": self.owner, "created_by": self.owner,
            "resource_type": "PACKAGED_FOOD_SURPLUS", "title": "Rice box kampus",
            "category": "prepared_meal", "description": "Nasi ayam baru dikemas",
            "quantity_total": "20.000", "unit": "box", "mode": "DONATE",
            "unit_price": "0.00", "condition": "Baru dimasak",
            "suitability": ["HUMAN_CONSUMPTION"],
            "location": {"label": "Kampus UI", "area": "Depok", "latitude": -6.36, "longitude": 106.82},
            "available_until": self.now + timedelta(days=1),
        }
        values.update(overrides)
        return ResourceListing.objects.create(**values)

    def reservation(self, listing, quantity="3.000", status=ReservationStatus.ACCEPTED,
                    pickup_status=None, **overrides):
        values = {
            "source_type": "LISTING_CLAIM", "listing": listing,
            "provider": listing.owner, "receiver": self.receiver,
            "submitted_by": self.receiver, "quantity": quantity,
            "unit": listing.unit, "mode": listing.mode, "unit_price": listing.unit_price,
            "resource_deadline": listing.available_until, "status": status,
        }
        if status == ReservationStatus.ACCEPTED:
            values.update(accepted_at=self.now - timedelta(hours=1), accepted_snapshot={"title": listing.title})
        values.update(overrides)
        reservation = Reservation.objects.create(**values)
        if pickup_status is not None:
            Pickup.objects.create(
                reservation=reservation, scheduled_at=self.now - timedelta(minutes=30),
                location=listing.location, status=pickup_status,
                completed_at=self.now - timedelta(minutes=10) if pickup_status == PickupStatus.COMPLETED else None,
            )
        return reservation

    def detail_url(self, listing):
        return f"{self.url}{listing.pk}/"

    def authenticate(self, user=None):
        # A real session cookie still passes through Django/DRF authentication.
        self.client.force_login(user or self.owner)

    def data(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response["Content-Type"].split(";")[0], "application/json")
        payload = response.json()
        self.assertEqual(set(payload), {"success", "message", "data"})
        self.assertIs(payload["success"], True)
        self.assertIsInstance(payload["message"], str)
        return payload["data"]

    def results(self, params=None, expected_count=None):
        data = self.data(self.client.get(self.url, params or {}))
        self.assertEqual(set(data), {"count", "next", "previous", "results"})
        if expected_count is not None:
            self.assertEqual(data["count"], expected_count)
        return data["results"]

    def assert_error(self, response, status, field=None, code=None):
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(response["Content-Type"].split(";")[0], "application/json")
        payload = response.json()
        self.assertIs(payload["success"], False)
        self.assertIsInstance(payload["message"], str)
        self.assertNotIn("data", payload)
        self.assertTrue(payload["errors"])
        for error in payload["errors"]:
            self.assertEqual(set(error), {"field", "code", "message"})
            self.assertIsInstance(error["code"], str)
            self.assertIsInstance(error["message"], str)
        self.assertTrue(any(
            (field is None or error["field"] == field)
            and (code is None or error["code"] == code)
            for error in payload["errors"]
        ), payload)

    def assert_quantities(self, data, available, allocated, completed):
        for field, expected in (
            ("quantity_available", available), ("quantity_allocated", allocated),
            ("quantity_completed", completed),
        ):
            self.assertEqual(data[field], expected)
        self.assertEqual(
            Decimal(data["quantity_total"]),
            sum(Decimal(data[key]) for key in ("quantity_available", "quantity_allocated", "quantity_completed")),
        )

    def test_guest_browse_without_filters(self):
        listing = self.listing()
        rows = self.results(expected_count=1)
        self.assertEqual([row["id"] for row in rows], [listing.pk])
        self.assertEqual(rows[0]["status"], "AVAILABLE")
        self.assert_quantities(rows[0], "20.000", "0.000", "0.000")

    def test_explicit_public_scope_matches_default(self):
        self.listing()
        self.assertEqual(self.results(), self.results({"scope": "public"}))

    def test_empty_browse_is_success(self):
        data = self.data(self.client.get(self.url))
        self.assertEqual(data, {"count": 0, "next": None, "previous": None, "results": []})

    def test_public_excludes_nonactive_expired_archived_and_fully_allocated(self):
        visible = self.listing()
        for status in (ListingStatus.CLOSED, ListingStatus.EXPIRED):
            self.listing(status=status)
        completed = self.listing(status=ListingStatus.COMPLETED)
        self.reservation(completed, "20.000", pickup_status=PickupStatus.COMPLETED)
        reserved = self.listing(status=ListingStatus.RESERVED)
        self.reservation(reserved, "20.000")
        self.listing(available_until=self.now - timedelta(seconds=1))
        self.listing(available_until=self.now)
        self.listing(archived_at=self.now)
        stale_available = self.listing()
        self.reservation(stale_available, "20.000")
        self.assertEqual([row["id"] for row in self.results(expected_count=1)], [visible.pk])

    def test_deadline_one_second_in_future_is_public(self):
        listing = self.listing(available_until=self.now + timedelta(seconds=1))
        self.assertEqual(self.results(expected_count=1)[0]["id"], listing.pk)

    def test_logged_in_owner_and_admin_still_get_public_scope_by_default(self):
        visible = self.listing()
        self.listing(status=ListingStatus.CLOSED)
        for actor in (self.owner, self.admin):
            with self.subTest(actor=actor.pk):
                self.authenticate(actor)
                self.assertEqual([row["id"] for row in self.results()], [visible.pk])

    def test_mine_requires_authentication(self):
        self.assert_error(self.client.get(self.url, {"scope": "mine"}), 401, code="authentication_required")

    def test_inactive_session_cannot_access_mine(self):
        self.authenticate(self.other)
        get_user_model().objects.filter(pk=self.other.pk).update(is_active=False)
        self.assert_error(self.client.get(self.url, {"scope": "mine"}), 401, code="authentication_required")

    def test_mine_contains_all_own_statuses_and_archive_only(self):
        own = [self.listing(status=status) for status in ListingStatus.values]
        self.reservation(own[1], "20.000")
        self.reservation(own[2], "20.000", pickup_status=PickupStatus.COMPLETED)
        own.append(self.listing(status=ListingStatus.CLOSED, archived_at=self.now))
        self.listing(owner=self.other, created_by=self.other)
        self.listing(owner=self.other, created_by=self.other, status=ListingStatus.CLOSED)
        self.authenticate()
        rows = self.results({"scope": "mine"}, expected_count=len(own))
        self.assertEqual({row["id"] for row in rows}, {listing.pk for listing in own})
        self.assertEqual({row["status"] for row in rows}, set(ListingStatus.values))

    def test_mine_does_not_include_listing_where_user_is_only_creator(self):
        self.listing(owner=self.other, created_by=self.owner)
        self.authenticate()
        self.assertEqual(self.results({"scope": "mine"}, expected_count=0), [])

    def test_admin_mine_is_also_limited_to_own_listings(self):
        self.listing()
        own = self.listing(owner=self.admin, created_by=self.admin)
        self.authenticate(self.admin)
        self.assertEqual([row["id"] for row in self.results({"scope": "mine"})], [own.pk])

    def test_status_filter_only_allowed_in_mine(self):
        self.assert_error(self.client.get(self.url, {"status": "AVAILABLE"}), 400, field="status")
        self.authenticate()
        self.assert_error(self.client.get(self.url, {"scope": "public", "status": "CLOSED"}), 400, field="status")

    def test_mine_filters_each_status(self):
        fixtures = {status: self.listing(status=status) for status in ListingStatus.values}
        self.reservation(fixtures["RESERVED"], "20.000")
        self.reservation(fixtures["COMPLETED"], "20.000", pickup_status=PickupStatus.COMPLETED)
        self.authenticate()
        for status, listing in fixtures.items():
            with self.subTest(status=status):
                rows = self.results({"scope": "mine", "status": status}, expected_count=1)
                self.assertEqual(rows[0]["id"], listing.pk)

    def test_search_matches_title_case_insensitively(self):
        match = self.listing(title="Bakery Kampus")
        self.listing(title="Sayuran", description="Masih segar", category="snack")
        self.assertEqual([row["id"] for row in self.results({"q": "bAkErY"})], [match.pk])

    def test_search_with_no_match_is_success(self):
        self.listing()
        self.assertEqual(self.results({"q": "tidak-ada-hasil"}, expected_count=0), [])

    def test_search_length_boundaries_are_valid(self):
        for term in ("ab", "a" * 100):
            with self.subTest(length=len(term)):
                self.results({"q": term}, expected_count=0)

    def test_invalid_query_parameters(self):
        cases = (
            ("scope", "all"), ("scope", ""),
            ("q", ""), ("q", "a"), ("q", "a" * 101),
            ("resource_type", "FOOD"), ("resource_type", ""),
            ("category", "unknown_category"), ("category", ""),
            ("mode", "FREE"), ("mode", ""),
            ("suitability", "UNKNOWN"), ("suitability", ""),
            ("suitability", "COMPOSTING,BSF"),
            ("status", "UNKNOWN"), ("ordering", "title"),
            ("ordering", "-id"), ("ordering", "created_at,-id"),
            ("ordering", "valid_until"), ("ordering", ""),
            ("page", "abc"), ("page", "1.5"),
            ("page_size", "0"), ("page_size", "101"),
            ("page_size", "abc"), ("page_size", "1.5"),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.assert_error(self.client.get(self.url, {field: value}), 400, field=field)

    def test_unknown_and_readonly_query_fields_rejected(self):
        for field in ("unknown", "owner_id", "created_by", "quantity_available", "latitude", "radius", "archived_at"):
            with self.subTest(field=field):
                self.assert_error(self.client.get(self.url, {field: "1"}), 400, field=field)

    def test_filter_resource_type(self):
        food = self.listing()
        waste = self.listing(resource_type="COMPOSTABLE_FOOD_WASTE", category="coffee_grounds", suitability=["COMPOSTING", "BSF"], unit="kg")
        for listing in (food, waste):
            with self.subTest(resource_type=listing.resource_type):
                self.assertEqual([row["id"] for row in self.results({"resource_type": listing.resource_type})], [listing.pk])

    def test_all_contract_category_codes_are_accepted_and_filter_exactly(self):
        for category in ("prepared_meal", "bakery", "snack", "coffee_grounds", "fruit_vegetable_scraps", "eggshells"):
            waste = category in ("coffee_grounds", "fruit_vegetable_scraps", "eggshells")
            listing = self.listing(category=category, resource_type="COMPOSTABLE_FOOD_WASTE" if waste else "PACKAGED_FOOD_SURPLUS", suitability=["COMPOSTING"] if waste else ["HUMAN_CONSUMPTION"])
            with self.subTest(category=category):
                self.assertEqual([row["id"] for row in self.results({"category": category})], [listing.pk])

    def test_filter_mode(self):
        donation = self.listing()
        sale = self.listing(mode="SELL", unit_price="20000.00")
        for listing in (donation, sale):
            with self.subTest(mode=listing.mode):
                self.assertEqual([row["id"] for row in self.results({"mode": listing.mode})], [listing.pk])

    def test_filter_each_suitability_including_multiple_values_on_listing(self):
        food = self.listing()
        waste = self.listing(resource_type="COMPOSTABLE_FOOD_WASTE", category="coffee_grounds", suitability=["COMPOSTING", "BSF", "ANIMAL_FEED", "UPCYCLING"])
        for suitability in ("HUMAN_CONSUMPTION", "COMPOSTING", "BSF", "ANIMAL_FEED", "UPCYCLING"):
            with self.subTest(suitability=suitability):
                expected = food if suitability == "HUMAN_CONSUMPTION" else waste
                self.assertEqual([row["id"] for row in self.results({"suitability": suitability})], [expected.pk])

    def test_area_is_exact_case_insensitive(self):
        depok = self.listing()
        self.listing(location={"label": "Depok", "area": "Depok Timur", "latitude": -6.3, "longitude": 106.8})
        self.assertEqual([row["id"] for row in self.results({"area": "dEpOk"})], [depok.pk])
        self.assertEqual(self.results({"area": "Dep"}, expected_count=0), [])

    def test_filters_combine_with_and(self):
        match = self.listing(title="Rice box kampus", mode="SELL", unit_price="20000.00")
        self.listing(title="Rice box kampus")
        self.listing(title="Rice box kampus", mode="SELL", unit_price="20000.00", category="bakery")
        filters = {"q": "Rice", "resource_type": "PACKAGED_FOOD_SURPLUS", "category": "prepared_meal", "mode": "SELL", "suitability": "HUMAN_CONSUMPTION", "area": "depok"}
        self.assertEqual([row["id"] for row in self.results(filters)], [match.pk])

    def test_mine_can_combine_filters_without_leaking_other_owners(self):
        own = self.listing(status=ListingStatus.CLOSED, mode="SELL", unit_price="1500.00")
        self.listing(owner=self.other, status=ListingStatus.CLOSED, mode="SELL", unit_price="1500.00")
        self.authenticate()
        self.assertEqual([row["id"] for row in self.results({"scope": "mine", "status": "CLOSED", "mode": "SELL"})], [own.pk])

    def test_default_order_is_newest_then_descending_id(self):
        older = self.listing()
        first = self.listing()
        second = self.listing()
        ResourceListing.objects.filter(pk=older.pk).update(created_at=self.now - timedelta(days=2))
        self.assertEqual([row["id"] for row in self.results()], [second.pk, first.pk, older.pk])

    def test_all_allowed_orderings(self):
        first = self.listing(available_until=self.now + timedelta(days=3))
        second = self.listing(available_until=self.now + timedelta(days=1))
        ResourceListing.objects.filter(pk=first.pk).update(created_at=self.now - timedelta(days=2))
        for ordering, expected in (
            ("created_at", [first.pk, second.pk]), ("-created_at", [second.pk, first.pk]),
            ("available_until", [second.pk, first.pk]), ("-available_until", [first.pk, second.pk]),
        ):
            with self.subTest(ordering=ordering):
                self.assertEqual([row["id"] for row in self.results({"ordering": ordering})], expected)

    def test_default_pagination_is_twenty(self):
        for _ in range(21):
            self.listing()
        first = self.data(self.client.get(self.url))
        self.assertEqual(first["count"], 21)
        self.assertEqual(len(first["results"]), 20)
        self.assertIsNone(first["previous"])
        self.assertIsNotNone(first["next"])
        second = self.data(self.client.get(first["next"]))
        self.assertEqual(len(second["results"]), 1)
        self.assertIsNone(second["next"])
        self.assertIsNotNone(second["previous"])
        self.assertTrue({row["id"] for row in first["results"]}.isdisjoint(row["id"] for row in second["results"]))

    def test_pagination_links_preserve_filters_and_order(self):
        for index in range(3):
            listing = self.listing()
            ResourceListing.objects.filter(pk=listing.pk).update(
                created_at=self.now - timedelta(minutes=3 - index),
            )
        self.listing(mode="SELL", unit_price="20000.00")
        data = self.data(self.client.get(self.url, {"mode": "DONATE", "ordering": "created_at", "page_size": 1}))
        ids = [data["results"][0]["id"]]
        for _ in range(2):
            self.assertIsNotNone(data["next"])
            data = self.data(self.client.get(data["next"]))
            self.assertEqual(data["count"], 3)
            self.assertEqual(len(data["results"]), 1)
            self.assertEqual(data["results"][0]["mode"], "DONATE")
            ids.append(data["results"][0]["id"])
        self.assertIsNone(data["next"])
        self.assertEqual(ids, sorted(set(ids)))
        self.assertEqual(len(ids), 3)

    def test_maximum_page_size_is_accepted(self):
        self.listing()
        self.results({"page_size": 100}, expected_count=1)

    def test_browse_response_schema_excludes_contacts_and_creator(self):
        self.listing(mode="SELL", unit_price="1500.25")
        row = self.results(expected_count=1)[0]
        self.assertEqual(set(row), self.response_fields)
        self.assertEqual(row["owner"], {"id": self.owner.pk, "display_name": self.owner.display_name})
        self.assertEqual(row["unit_price"], "1500.25")
        self.assertNotIn(self.owner.email, str(row))
        self.assertNotIn(self.owner.whatsapp_number, str(row))

    def test_browse_quantities_include_partial_allocation_and_completed_once(self):
        listing = self.listing()
        self.reservation(listing, "3.000")
        self.reservation(listing, "4.000", pickup_status=PickupStatus.SCHEDULED)
        self.reservation(listing, "5.000", pickup_status=PickupStatus.COMPLETED)
        self.reservation(listing, "2.000", status=ReservationStatus.PENDING)
        row = self.results(expected_count=1)[0]
        self.assert_quantities(row, "8.000", "7.000", "5.000")
        self.assertEqual(row["status"], "AVAILABLE")

    def test_mine_effective_expiry_releases_allocation_but_keeps_completed_quantity(self):
        listing = self.listing(available_until=self.now)
        self.reservation(listing, "3.000", pickup_status=PickupStatus.SCHEDULED)
        self.reservation(listing, "5.000", pickup_status=PickupStatus.COMPLETED)
        self.authenticate()
        for _ in range(2):
            row = self.results({"scope": "mine", "status": "EXPIRED"}, expected_count=1)[0]
            self.assertEqual(row["status"], "EXPIRED")
            self.assert_quantities(row, "15.000", "0.000", "5.000")
            self.assertEqual(self.results(expected_count=0), [])

    def test_page_out_of_range_returns_contract_error(self):
        self.listing()
        for page in (0, -1, 2, 999):
            with self.subTest(page=page):
                self.assert_error(self.client.get(self.url, {"page": page}), 404, code="page_not_found")

    def test_empty_browse_second_page_is_not_found(self):
        self.assert_error(self.client.get(self.url, {"page": 2}), 404, code="page_not_found")

    def test_public_detail_schema_decimal_strings_and_no_contacts(self):
        listing = self.listing(mode="SELL", unit_price="20000.00")
        detail = self.data(self.client.get(self.detail_url(listing)))
        self.assertEqual(set(detail), self.response_fields)
        self.assertEqual(detail["owner"], {"id": self.owner.pk, "display_name": self.owner.display_name})
        self.assertEqual(detail["quantity_total"], "20.000")
        self.assertEqual(detail["unit_price"], "20000.00")
        self.assertEqual(detail["location"], listing.location)
        self.assertEqual(detail["suitability"], listing.suitability)
        self.assertEqual(detail["version"], 1)
        self.assertIsNone(detail["archived_at"])
        for field in ("created_at", "updated_at", "available_until"):
            self.assertIsNotNone(parse_datetime(detail[field]).tzinfo)
        for field in ("storage_notes", "allergen_notes", "packaging_status"):
            self.assertIsNone(detail[field])
        rows = self.results()
        self.assertEqual(rows[0], detail)
        for response in (detail, rows):
            text = str(response)
            for private in (self.owner.email, self.owner.whatsapp_number, "whatsapp_number", "google_sub", "password", "created_by"):
                self.assertNotIn(private, text)

    def test_optional_notes_are_serialized(self):
        listing = self.listing(storage_notes="Simpan dingin", allergen_notes="Kacang", packaging_status="Tertutup")
        data = self.data(self.client.get(self.detail_url(listing)))
        self.assertEqual(data["storage_notes"], "Simpan dingin")
        self.assertEqual(data["allergen_notes"], "Kacang")
        self.assertEqual(data["packaging_status"], "Tertutup")

    def test_missing_detail_returns_not_found(self):
        self.assert_error(self.client.get(f"{self.url}999999/"), 404, code="not_found")

    def test_malformed_or_nonpositive_detail_id_is_bad_request(self):
        for pk in ("abc", "1.5", "0", "-1", "9223372036854775808", "9" * 100):
            with self.subTest(pk=pk):
                self.assert_error(self.client.get(f"{self.url}{pk}/"), 400)

    def test_detail_rejects_unknown_query_parameters(self):
        listing = self.listing()
        self.assert_error(
            self.client.get(self.detail_url(listing), {"owner_id": self.owner.pk}),
            400, field="owner_id", code="unknown_field",
        )

    def test_inactive_owner_session_cannot_read_private_detail(self):
        listing = self.listing(status=ListingStatus.CLOSED)
        self.authenticate()
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assert_error(self.client.get(self.detail_url(listing)), 404, code="not_found")

    def test_inactive_detail_hidden_from_guest_and_unrelated_user(self):
        listings = [self.listing(status=status) for status in (ListingStatus.CLOSED, ListingStatus.EXPIRED)]
        listings.append(self.listing(archived_at=self.now))
        listings.append(self.listing(available_until=self.now))
        reserved = self.listing(status=ListingStatus.RESERVED)
        self.reservation(reserved, "20.000")
        listings.append(reserved)
        completed = self.listing(status=ListingStatus.COMPLETED)
        self.reservation(completed, "20.000", pickup_status=PickupStatus.COMPLETED)
        listings.append(completed)
        for actor in (None, self.other):
            if actor:
                self.authenticate(actor)
            for listing in listings:
                with self.subTest(actor=getattr(actor, "pk", None), listing=listing.pk):
                    self.assert_error(self.client.get(self.detail_url(listing)), 404, code="not_found")

    def test_owner_and_admin_can_read_all_inactive_and_archived_details(self):
        listings = [self.listing(status=status) for status in ListingStatus.values]
        self.reservation(listings[1], "20.000")
        self.reservation(listings[2], "20.000", pickup_status=PickupStatus.COMPLETED)
        listings.append(self.listing(status=ListingStatus.CLOSED, archived_at=self.now))
        for actor in (self.owner, self.admin):
            self.authenticate(actor)
            for listing in listings:
                with self.subTest(actor=actor.pk, listing=listing.pk):
                    data = self.data(self.client.get(self.detail_url(listing)))
                    self.assertEqual(data["id"], listing.pk)
                    self.assertEqual(set(data["owner"]), {"id", "display_name"})

    def test_related_receiver_can_read_history_for_every_reservation_status(self):
        for status in ReservationStatus.values:
            listing = self.listing(status=ListingStatus.CLOSED, archived_at=self.now)
            self.reservation(listing, status=status)
            self.authenticate(self.receiver)
            with self.subTest(status=status):
                data = self.data(self.client.get(self.detail_url(listing)))
                self.assertEqual(data["id"], listing.pk)
                self.assertEqual(set(data), self.response_fields)
                self.assertEqual(set(data["owner"]), {"id", "display_name"})

    def test_creator_without_ownership_or_participation_cannot_read_history(self):
        listing = self.listing(owner=self.other, created_by=self.owner, status=ListingStatus.CLOSED)
        self.authenticate(self.owner)
        self.assert_error(self.client.get(self.detail_url(listing)), 404, code="not_found")

    def test_participation_in_another_listing_does_not_grant_history_access(self):
        related = self.listing(status=ListingStatus.CLOSED)
        unrelated = self.listing(status=ListingStatus.CLOSED)
        self.reservation(related)
        self.authenticate(self.receiver)
        self.assert_error(self.client.get(self.detail_url(unrelated)), 404, code="not_found")

    def test_pending_rejected_and_cancelled_do_not_allocate_stock(self):
        listing = self.listing()
        self.reservation(listing, "2.000", status=ReservationStatus.PENDING)
        self.reservation(listing, "3.000", status=ReservationStatus.REJECTED)
        self.reservation(listing, "4.000", status=ReservationStatus.CANCELLED, pickup_status=PickupStatus.CANCELLED)
        data = self.data(self.client.get(self.detail_url(listing)))
        self.assert_quantities(data, "20.000", "0.000", "0.000")

    def test_partial_allocations_and_completed_pickups_are_counted_once(self):
        listing = self.listing()
        self.reservation(listing, "3.000")
        self.reservation(listing, "4.000", pickup_status=PickupStatus.SCHEDULED)
        self.reservation(listing, "5.000", pickup_status=PickupStatus.COMPLETED)
        self.reservation(listing, "2.000", status=ReservationStatus.PENDING)
        data = self.data(self.client.get(self.detail_url(listing)))
        self.assert_quantities(data, "8.000", "7.000", "5.000")
        self.assertEqual(data["status"], "AVAILABLE")
        self.assertEqual(self.results(expected_count=1)[0], data)

    def test_decimal_kg_quantity_remains_precise(self):
        listing = self.listing(resource_type="COMPOSTABLE_FOOD_WASTE", category="coffee_grounds", suitability=["COMPOSTING"], unit="kg", quantity_total="1.001", mode="SELL", unit_price="1500.25")
        self.reservation(listing, "0.333")
        self.reservation(listing, "0.111", pickup_status=PickupStatus.COMPLETED)
        data = self.data(self.client.get(self.detail_url(listing)))
        self.assert_quantities(data, "0.557", "0.333", "0.111")
        self.assertEqual(data["unit_price"], "1500.25")

    def test_quantity_from_other_listing_is_not_counted(self):
        listing = self.listing()
        other = self.listing()
        self.reservation(other, "20.000")
        self.assert_quantities(self.data(self.client.get(self.detail_url(listing))), "20.000", "0.000", "0.000")

    def test_cancelled_and_no_show_pickups_release_quantity(self):
        listing = self.listing()
        self.reservation(listing, "4.000", status=ReservationStatus.CANCELLED, pickup_status=PickupStatus.CANCELLED)
        self.reservation(listing, "5.000", status=ReservationStatus.CANCELLED, pickup_status=PickupStatus.NO_SHOW)
        self.reservation(listing, "3.000")
        data = self.data(self.client.get(self.detail_url(listing)))
        self.assert_quantities(data, "17.000", "3.000", "0.000")
        self.assertEqual(data["status"], "AVAILABLE")

    def test_related_receiver_cannot_browse_inactive_history_or_claim_ownership(self):
        listing = self.listing(status=ListingStatus.CLOSED, archived_at=self.now)
        self.reservation(listing)
        self.authenticate(self.receiver)
        self.assertEqual(self.results(expected_count=0), [])
        self.assertEqual(self.results({"scope": "mine"}, expected_count=0), [])

    def test_fully_allocated_listing_is_reserved_in_history(self):
        listing = self.listing(status=ListingStatus.RESERVED)
        self.reservation(listing, "15.000")
        self.reservation(listing, "5.000", pickup_status=PickupStatus.COMPLETED)
        self.authenticate()
        data = self.data(self.client.get(self.detail_url(listing)))
        self.assertEqual(data["status"], "RESERVED")
        self.assert_quantities(data, "0.000", "15.000", "5.000")
        self.assertEqual(self.results(expected_count=0), [])

    def test_fully_completed_listing_remains_completed_after_deadline(self):
        listing = self.listing(status=ListingStatus.COMPLETED, available_until=self.now - timedelta(hours=1))
        self.reservation(listing, "20.000", pickup_status=PickupStatus.COMPLETED)
        self.authenticate()
        data = self.data(self.client.get(self.detail_url(listing)))
        self.assertEqual(data["status"], "COMPLETED")
        self.assert_quantities(data, "0.000", "0.000", "20.000")

    def test_closed_listing_keeps_accepted_allocation_until_cancelled(self):
        listing = self.listing(status=ListingStatus.CLOSED)
        self.reservation(listing, "5.000")
        self.authenticate()
        data = self.data(self.client.get(self.detail_url(listing)))
        self.assertEqual(data["status"], "CLOSED")
        self.assert_quantities(data, "15.000", "5.000", "0.000")

    def test_closed_and_expired_listings_do_not_reopen_after_release(self):
        for status in (ListingStatus.CLOSED, ListingStatus.EXPIRED):
            listing = self.listing(status=status)
            self.reservation(listing, "5.000", status=ReservationStatus.CANCELLED, pickup_status=PickupStatus.NO_SHOW)
            self.authenticate()
            with self.subTest(status=status):
                data = self.data(self.client.get(self.detail_url(listing)))
                self.assertEqual(data["status"], status)
                self.assert_quantities(data, "20.000", "0.000", "0.000")

    def test_deadline_effective_before_worker_and_repeated_reads_are_stable(self):
        listing = self.listing(available_until=self.now)
        self.reservation(listing, "3.000")
        self.reservation(listing, "4.000", pickup_status=PickupStatus.SCHEDULED)
        self.reservation(listing, "5.000", pickup_status=PickupStatus.COMPLETED)
        self.reservation(listing, "2.000", status=ReservationStatus.PENDING)
        self.authenticate()
        for _ in range(2):
            data = self.data(self.client.get(self.detail_url(listing)))
            self.assertEqual(data["status"], "EXPIRED")
            self.assert_quantities(data, "15.000", "0.000", "5.000")
            rows = self.results({"scope": "mine", "status": "EXPIRED"}, expected_count=1)
            self.assertEqual(rows[0]["id"], listing.pk)
            self.assert_quantities(rows[0], "15.000", "0.000", "5.000")
            self.assertEqual(self.results(expected_count=0), [])

    def test_repeated_reads_do_not_duplicate_or_change_transactions(self):
        listing = self.listing()
        reservation = self.reservation(listing, "5.000", pickup_status=PickupStatus.COMPLETED)
        expected_snapshot = reservation.accepted_snapshot
        first = self.data(self.client.get(self.detail_url(listing)))
        second = self.data(self.client.get(self.detail_url(listing)))
        self.assertEqual(first, second)
        self.assertEqual(Reservation.objects.count(), 1)
        self.assertEqual(Pickup.objects.count(), 1)
        reservation.refresh_from_db()
        self.assertEqual(reservation.accepted_snapshot, expected_snapshot)
        self.assertEqual(reservation.status, ReservationStatus.ACCEPTED)
        self.assertEqual(reservation.pickup.status, PickupStatus.COMPLETED)
