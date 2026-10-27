from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from pickups.models import Pickup, PickupStatus
from reservations.models import Reservation, ReservationStatus, SourceType
from shared.enums import Mode, ResourceType, Suitability
from .models import ResourceRequest, RequestStatus


class RequestReadTests(TestCase):
    url = "/api/v1/requests/"

    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_user(email="owner@example.com", password="secret",
                                     display_name="Receiver", whatsapp_number="+6281234567890")
        cls.provider = users.create_user(email="provider@example.com", password="secret", display_name="Provider")
        cls.stranger = users.create_user(email="stranger@example.com", password="secret", display_name="Stranger")
        cls.admin = users.create_user(email="admin@example.com", password="secret", display_name="Admin", is_staff=True)

    def setUp(self):
        self.client = APIClient()
        self.now = timezone.now()

    def make_request(self, **changes):
        values = dict(owner=self.owner, created_by=self.owner, title="Makanan kampus",
                      description="Untuk kegiatan mahasiswa", resource_type=ResourceType.PACKAGED_FOOD_SURPLUS,
                      category="prepared_meal", intended_use=Suitability.HUMAN_CONSUMPTION,
                      quantity_needed=Decimal("20.000"), unit="box",
                      location={"label": "Kampus UI", "area": "Depok", "latitude": -6.36, "longitude": 106.82},
                      donate_only=True, max_unit_price=None, valid_until=self.now + timedelta(days=1))
        values.update(changes)
        return ResourceRequest.objects.create(**values)

    def offer(self, source, quantity, status=ReservationStatus.ACCEPTED, pickup_status=None, **changes):
        values = dict(source_type=SourceType.REQUEST_OFFER, request=source, provider=self.provider,
                      receiver=source.owner, submitted_by=self.provider, quantity=quantity,
                      unit=source.unit, mode=Mode.DONATE, unit_price="0.00", status=status,
                      resource_deadline=self.now + timedelta(days=2))
        values.update(changes)
        reservation = Reservation.objects.create(**values)
        if pickup_status:
            Pickup.objects.create(reservation=reservation, scheduled_at=self.now + timedelta(hours=2),
                                  location=source.location, status=pickup_status)
        return reservation

    def detail(self, source):
        return self.client.get(f"{self.url}{source.pk}/")

    def ids(self, response):
        self.assertEqual(response.status_code, 200, response.data)
        return [row["id"] for row in response.data["data"]["results"]]

    def test_public_only_open_unexpired_unarchived_positive_remaining(self):
        visible = self.make_request()
        for status in (RequestStatus.ALLOCATED, RequestStatus.FULFILLED, RequestStatus.CLOSED, RequestStatus.EXPIRED):
            self.make_request(status=status)
        self.make_request(valid_until=self.now - timedelta(seconds=1))
        self.make_request(archived_at=self.now)
        full = self.make_request()
        self.offer(full, "20.000")
        fulfilled = self.make_request()
        self.offer(fulfilled, "20.000", pickup_status=PickupStatus.COMPLETED)
        self.assertEqual(self.ids(self.client.get(self.url)), [visible.pk])

    def test_empty_list_and_default_pagination(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"success": True, "message": "OK", "data": {
            "count": 0, "next": None, "previous": None, "results": []}})
        self.make_request()
        self.assertEqual(self.ids(self.client.get(self.url, {"q": "not present"})), [])

    def test_quantities_ignore_pending_rejected_cancelled_and_do_not_double_count_completed(self):
        source = self.make_request(quantity_needed="30.000")
        self.offer(source, "4.000")  # Accepted, no pickup yet.
        self.offer(source, "3.000", pickup_status=PickupStatus.SCHEDULED)
        self.offer(source, "5.000", pickup_status=PickupStatus.COMPLETED)
        self.offer(source, "2.000", pickup_status=PickupStatus.CANCELLED)
        self.offer(source, "2.000", pickup_status=PickupStatus.NO_SHOW)
        for status in (ReservationStatus.PENDING, ReservationStatus.REJECTED, ReservationStatus.CANCELLED):
            self.offer(source, "9.000", status=status)
        data = self.detail(source).data["data"]
        self.assertEqual(data["quantity_needed"], "30.000")
        self.assertEqual(data["quantity_allocated"], "7.000")
        self.assertEqual(data["quantity_fulfilled"], "5.000")
        self.assertEqual(data["quantity_remaining"], "18.000")
        self.assertEqual(self.client.get(self.url).data["data"]["results"][0], data)

    def test_decimal_quantities_and_price_are_strings(self):
        source = self.make_request(quantity_needed="2.125", unit="kg", donate_only=False, max_unit_price="15000.50")
        self.offer(source, "0.125")
        data = self.detail(source).data["data"]
        self.assertEqual(data["quantity_remaining"], "2.000")
        self.assertEqual(data["max_unit_price"], "15000.50")
        self.assertEqual(data["unit"], "kg")
        self.assertFalse(data["donate_only"])

    def test_filters_individually_and_combined(self):
        target = self.make_request()
        other = self.make_request(title="Ampas kopi", description="Bahan kompos", resource_type=ResourceType.COMPOSTABLE_FOOD_WASTE,
                                  category="coffee_grounds", intended_use=Suitability.COMPOSTING, donate_only=False,
                                  location={"label": "Kafe", "area": "Bogor", "latitude": -6.6, "longitude": 106.8})
        for key, value in (("q", "KAMPUS"), ("resource_type", ResourceType.PACKAGED_FOOD_SURPLUS),
                           ("category", "prepared_meal"), ("area", "dEpOk"),
                           ("intended_use", Suitability.HUMAN_CONSUMPTION), ("donate_only", "true")):
            with self.subTest(key=key):
                self.assertEqual(self.ids(self.client.get(self.url, {key: value})), [target.pk])
        self.assertEqual(self.ids(self.client.get(self.url, {"donate_only": "false"})), [other.pk])
        self.assertEqual(self.ids(self.client.get(self.url, {"area": "Dep"})), [])
        self.assertEqual(self.ids(self.client.get(self.url, {"q": "kopi", "resource_type": ResourceType.COMPOSTABLE_FOOD_WASTE,
                                                          "category": "coffee_grounds", "intended_use": "COMPOSTING",
                                                          "area": "bogor", "donate_only": "false"})), [other.pk])

    def test_search_covers_description_category_and_location(self):
        source = self.make_request()
        for term in ("mahasiswa", "prepared_meal", "Kampus UI", "Depok"):
            with self.subTest(term=term):
                self.assertEqual(self.ids(self.client.get(self.url, {"q": term})), [source.pk])

    def test_invalid_query_parameters(self):
        cases = {"scope": ["all", ""], "q": ["x", "x" * 101, ""], "resource_type": ["food"],
                 "category": ["unknown"], "area": ["", "x" * 101], "intended_use": ["EATING"],
                 "donate_only": ["1", "yes", "True"], "ordering": ["id", "available_until", "created_at,-id"],
                 "page": ["abc", "1.0", "last"],
                 "page_size": ["0", "101", "abc", "1.0"], "unknown": ["x"]}
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    response = self.client.get(self.url, {key: value})
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertFalse(response.data["success"])
                    self.assertEqual(response.data["errors"][0]["field"], key)
        self.assertEqual(self.client.get(self.url + "?q=rice&q=meal").status_code, 400)

    def test_pagination_and_stable_order(self):
        sources = [self.make_request() for _ in range(3)]
        ResourceRequest.objects.update(created_at=self.now)
        first = self.client.get(self.url, {"page_size": 2})
        self.assertEqual(self.ids(first), [sources[2].pk, sources[1].pk])
        self.assertEqual(first.data["data"]["count"], 3)
        self.assertIsNotNone(first.data["data"]["next"])
        second = self.client.get(self.url, {"page_size": 2, "page": 2})
        self.assertEqual(self.ids(second), [sources[0].pk])
        self.assertIsNotNone(second.data["data"]["previous"])
        response = self.client.get(self.url, {"page_size": 2, "page": 3})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["errors"][0]["code"], "page_not_found")
        for page in (0, -1):
            response = self.client.get(self.url, {"page": page})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.data["errors"][0]["code"], "page_not_found")

    def test_default_page_size_is_twenty(self):
        for _ in range(21):
            self.make_request()
        response = self.client.get(self.url)
        self.assertEqual(len(self.ids(response)), 20)
        self.assertEqual(response.data["data"]["count"], 21)

    def test_valid_until_equal_to_now_is_already_hidden(self):
        source = self.make_request(valid_until=self.now)
        with patch("requests_app.views.timezone.now", return_value=self.now):
            self.assertEqual(self.ids(self.client.get(self.url)), [])
            self.assertEqual(self.detail(source).status_code, 404)
            self.client.force_authenticate(self.owner)
            self.assertEqual(self.detail(source).status_code, 200)

    def test_all_supported_orderings_and_page_size_limit(self):
        early = self.make_request(valid_until=self.now + timedelta(hours=1))
        late = self.make_request(valid_until=self.now + timedelta(hours=2))
        ResourceRequest.objects.filter(pk=early.pk).update(created_at=self.now - timedelta(hours=1))
        for ordering, expected in (("created_at", [early.pk, late.pk]), ("-created_at", [late.pk, early.pk]),
                                   ("valid_until", [early.pk, late.pk]), ("-valid_until", [late.pk, early.pk])):
            self.assertEqual(self.ids(self.client.get(self.url, {"ordering": ordering, "page_size": 100})), expected)

    def test_mine_requires_active_login(self):
        response = self.client.get(self.url, {"scope": "mine"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["errors"][0]["code"], "authentication_required")
        self.stranger.is_active = False
        self.client.force_authenticate(self.stranger)
        self.assertEqual(self.client.get(self.url, {"scope": "mine"}).status_code, 401)

    def test_mine_includes_all_own_statuses_and_archive_but_no_other_users(self):
        own = [self.make_request(status=status, valid_until=self.now - timedelta(days=1), archived_at=self.now)
               for status in RequestStatus.values]
        self.make_request(owner=self.stranger)
        self.client.force_authenticate(self.owner)
        self.assertEqual(set(self.ids(self.client.get(self.url, {"scope": "mine"}))), {row.pk for row in own})
        for source in own:
            self.assertEqual(self.ids(self.client.get(self.url, {"scope": "mine", "status": source.status})), [source.pk])
        self.assertEqual(self.client.get(self.url, {"scope": "mine", "status": "BAD"}).status_code, 400)
        self.assertEqual(self.client.get(self.url, {"status": "OPEN"}).status_code, 400)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.ids(self.client.get(self.url, {"scope": "mine"})), [])

    def test_nonpublic_detail_only_owner_participant_or_admin(self):
        source = self.make_request(status=RequestStatus.CLOSED, archived_at=self.now)
        self.offer(source, "2.000", status=ReservationStatus.CANCELLED)
        self.offer(source, "1.000", status=ReservationStatus.REJECTED)
        self.assertEqual(self.detail(source).status_code, 404)
        self.client.force_authenticate(self.stranger)
        self.assertEqual(self.detail(source).status_code, 404)
        for user in (self.owner, self.provider, self.admin):
            self.client.force_authenticate(user)
            self.assertEqual(self.detail(source).status_code, 200)
        self.client.force_authenticate(self.provider)
        self.assertEqual(self.ids(self.client.get(self.url, {"scope": "mine"})), [])

    def test_every_nonpublic_case_is_hidden_from_strangers(self):
        sources = [self.make_request(status=status) for status in RequestStatus.values if status != RequestStatus.OPEN]
        sources += [self.make_request(valid_until=self.now - timedelta(seconds=1)), self.make_request(archived_at=self.now)]
        full = self.make_request()
        self.offer(full, "20.000")
        sources.append(full)
        for source in sources:
            for user in (None, self.stranger):
                self.client.force_authenticate(user)
                self.assertEqual(self.detail(source).status_code, 404)

    def test_missing_and_malformed_ids_use_json_errors(self):
        for pk in ("abc", "-1", "0", "1.5", "999999999999999999999999"):
            response = self.client.get(f"{self.url}{pk}/")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.data["errors"][0]["field"], "id")
        self.assertEqual(self.client.get(f"{self.url}999999/").status_code, 404)

    def test_inactive_owner_has_no_private_detail_access(self):
        source = self.make_request(status=RequestStatus.CLOSED)
        self.owner.is_active = False
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.detail(source).status_code, 404)

    def test_contract_fields_never_expose_contacts_or_internal_creator(self):
        source = self.make_request()
        expected = {"id", "created_at", "updated_at", "version", "resource_type", "category", "intended_use",
                    "quantity_needed", "unit", "location", "donate_only", "max_unit_price", "valid_until",
                    "owner", "status", "quantity_remaining", "quantity_allocated", "quantity_fulfilled", "archived_at"}
        for user in (None, self.owner, self.admin):
            self.client.force_authenticate(user)
            data = self.detail(source).data["data"]
            self.assertEqual(set(data), expected)
            self.assertEqual(data["owner"], {"id": self.owner.pk, "display_name": self.owner.display_name})
            self.assertNotIn(self.owner.email, str(data))
            self.assertNotIn(self.owner.whatsapp_number, str(data))

    def test_reads_preserve_terminal_states_reservations_and_versions(self):
        source = self.make_request(status=RequestStatus.FULFILLED, valid_until=self.now - timedelta(days=1))
        offer = self.offer(source, "20.000", pickup_status=PickupStatus.COMPLETED)
        self.client.force_authenticate(self.owner)
        before = ResourceRequest.objects.values().get(pk=source.pk)
        before_offer = Reservation.objects.values().get(pk=offer.pk)
        self.client.get(self.url, {"scope": "mine"})
        self.assertEqual(self.detail(source).data["data"]["status"], RequestStatus.FULFILLED)
        self.assertEqual(ResourceRequest.objects.values().get(pk=source.pk), before)
        self.assertEqual(Reservation.objects.values().get(pk=offer.pk), before_offer)

    def test_expired_request_retains_accepted_allocation_and_completed_history(self):
        source = self.make_request(valid_until=self.now - timedelta(seconds=1))
        self.offer(source, "4.000")
        self.offer(source, "3.000", pickup_status=PickupStatus.COMPLETED)
        self.client.force_authenticate(self.owner)
        data = self.detail(source).data["data"]
        self.assertEqual(data["quantity_allocated"], "4.000")
        self.assertEqual(data["quantity_fulfilled"], "3.000")
        self.assertEqual(data["quantity_remaining"], "13.000")
        self.assertEqual(self.ids(self.client.get(self.url)), [])

    def test_list_does_not_query_each_owner_or_reservation_separately(self):
        for _ in range(4):
            source = self.make_request()
            self.offer(source, "2.000")
        with self.assertNumQueries(2):
            response = self.client.get(self.url)
        self.assertEqual(len(self.ids(response)), 4)
