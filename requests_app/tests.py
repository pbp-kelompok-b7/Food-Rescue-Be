from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Event
from unittest.mock import patch
import json
import time

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone
from rest_framework.test import APIClient

from pickups.models import Pickup, PickupStatus
from reservations.models import Reservation, ReservationStatus, SourceType
from shared.enums import Mode, ResourceType, Suitability
from shared.models import AuditEvent
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


class RequestWriteTests(TestCase):
    url = "/api/v1/requests/"
    make_request = RequestReadTests.make_request
    offer = RequestReadTests.offer

    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_user(email="receiver@example.com", password="secret", display_name="Receiver")
        cls.provider = users.create_user(email="provider@example.com", password="secret", display_name="Provider")
        cls.stranger = users.create_user(email="other@example.com", password="secret", display_name="Other")
        cls.admin = users.create_user(email="admin@example.com", password="secret", display_name="Admin", is_staff=True)

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.owner)
        self.now = timezone.now()
        self.body = {
            "resource_type": "PACKAGED_FOOD_SURPLUS", "category": "prepared_meal",
            "intended_use": "HUMAN_CONSUMPTION", "quantity_needed": "20.000", "unit": "box",
            "location": {"label": "Kampus UI", "area": "Depok", "latitude": -6.36, "longitude": 106.82},
            "valid_until": (self.now + timedelta(days=1)).isoformat(),
        }

    def post(self, data):
        return self.client.post(self.url, data, format="json")

    def edit(self, source, data):
        return self.client.patch(f"{self.url}{source.pk}/", data, format="json")

    def test_create_sets_session_owner_defaults_and_quantity_response(self):
        response = self.post(self.body)
        self.assertEqual(response.status_code, 201, response.data)
        data = response.data["data"]
        row = ResourceRequest.objects.get(pk=data["id"])
        self.assertEqual((row.owner_id, row.created_by_id), (self.owner.pk, self.owner.pk))
        self.assertEqual(data["status"], "OPEN")
        self.assertEqual(data["quantity_needed"], data["quantity_remaining"])
        self.assertEqual(data["quantity_allocated"], "0.000")
        self.assertEqual(data["quantity_fulfilled"], "0.000")
        self.assertFalse(data["donate_only"])
        self.assertIsNone(data["max_unit_price"])
        self.assertEqual(data["version"], 1)

    def test_required_and_unknown_read_only_fields_rejected_without_insert(self):
        for field in self.body:
            body = deepcopy(self.body)
            del body[field]
            with self.subTest(missing=field):
                self.assertEqual(self.post(body).status_code, 400)
        for field in ("owner", "owner_id", "created_by", "status", "version", "quantity_remaining",
                      "quantity_allocated", "quantity_fulfilled", "archived_at", "unexpected", "title"):
            with self.subTest(field=field):
                self.assertEqual(self.post({**self.body, field: 1}).status_code, 400)
        self.assertEqual(ResourceRequest.objects.count(), 0)

    def test_invalid_json_shapes_and_malformed_json(self):
        for body in ([], [self.body], "hello", 1, None):
            response = self.client.generic("POST", self.url, json.dumps(body), content_type="application/json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(self.client.generic("POST", self.url, "{", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.generic("POST", self.url, "quantity_needed=20",
                                             content_type="application/x-www-form-urlencoded").status_code, 415)

    def test_quantity_precision_and_integer_units(self):
        for quantity in ("0", "-1", "1.0001", "NaN", "Infinity", "1e2", 2, True, None, "1000000000000"):
            with self.subTest(quantity=quantity):
                self.assertEqual(self.post({**self.body, "quantity_needed": quantity}).status_code, 400)
        for unit in ("box", "pcs", "paket"):
            self.assertEqual(self.post({**self.body, "unit": unit, "quantity_needed": "1.125"}).status_code, 400)
            self.assertEqual(self.post({**self.body, "unit": unit, "quantity_needed": "2.000"}).status_code, 201)
        self.assertEqual(self.post({**self.body, "unit": "kg", "quantity_needed": "1.125"}).status_code, 201)
        self.assertEqual(self.post({**self.body, "unit": "litre"}).status_code, 400)

    def test_resource_category_and_intended_use_must_match(self):
        for changes in ({"category": "coffee_grounds"}, {"category": "unknown"}, {"intended_use": "BSF"},
                        {"resource_type": "unknown"}, {"intended_use": "unknown"}):
            self.assertEqual(self.post({**self.body, **changes}).status_code, 400)
        organic = {**self.body, "resource_type": "COMPOSTABLE_FOOD_WASTE", "category": "coffee_grounds"}
        self.assertEqual(self.post(organic).status_code, 400)
        for intended in ("COMPOSTING", "BSF", "ANIMAL_FEED", "UPCYCLING"):
            self.assertEqual(self.post({**organic, "intended_use": intended}).status_code, 201)

    def test_location_required_complete_bounded_and_no_unknown_fields(self):
        original = self.body["location"]
        invalid = [None, [], {}, {**original, "unknown": 1}]
        invalid += [{k: v for k, v in original.items() if k != missing} for missing in original]
        invalid += [{**original, field: value} for field, value in (
            ("label", ""), ("label", "x" * 301), ("area", ""), ("area", "x" * 101),
            ("latitude", 91), ("longitude", -181), ("latitude", "-6.36"), ("latitude", True),
            ("latitude", 10 ** 400), ("label", 42))]
        for location in invalid:
            with self.subTest(location=location):
                self.assertEqual(self.post({**self.body, "location": location}).status_code, 400)

    def test_valid_until_requires_future_rfc3339_with_offset(self):
        future = self.now + timedelta(days=2)
        for value in (None, "invalid", future.date().isoformat(), future.replace(tzinfo=None).isoformat(),
                      future.strftime("%Y-%m-%d %H:%M:%S+00:00"), self.now.isoformat(),
                      (self.now - timedelta(days=1)).isoformat()):
            self.assertEqual(self.post({**self.body, "valid_until": value}).status_code, 400)
        self.assertEqual(self.post({**self.body, "valid_until": future.isoformat().replace("+00:00", "Z")}).status_code, 201)
        self.assertEqual(self.post({**self.body, "valid_until": future.strftime("%Y-%m-%dT%H:%M:%S+07:00")}).status_code, 201)

    def test_price_precision_nullability_and_donate_only(self):
        for price in ("-1", "1.001", "NaN", 10, True):
            self.assertEqual(self.post({**self.body, "max_unit_price": price}).status_code, 400)
        for price in (None, "0.00"):
            self.assertEqual(self.post({**self.body, "donate_only": True, "max_unit_price": price}).status_code, 201)
        self.assertEqual(self.post({**self.body, "max_unit_price": "12500.50"}).status_code, 201)
        self.assertEqual(self.post({**self.body, "donate_only": True, "max_unit_price": "1.00"}).status_code, 400)
        for value in ("true", "false", 1, 0, None):
            self.assertEqual(self.post({**self.body, "donate_only": value}).status_code, 400)

    def test_writes_require_active_authentication(self):
        source = self.make_request()
        for user in (None, self.stranger):
            if user:
                user.is_active = False
            self.client.force_authenticate(user)
            self.assertEqual(self.post(self.body).status_code, 401)
            self.assertEqual(self.edit(source, {"quantity_needed": "30"}).status_code, 401)

    def test_real_session_writes_require_csrf(self):
        source = self.make_request()
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(self.owner)
        for response in (self.post(self.body), self.edit(source, {"quantity_needed": "30"})):
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.data["errors"][0]["code"], "csrf_failed")
        from django.middleware.csrf import get_token
        from django.test import RequestFactory
        request = RequestFactory().get("/")
        token = get_token(request)
        self.client.cookies["csrftoken"] = request.META["CSRF_COOKIE"]
        self.client.credentials(HTTP_X_CSRFTOKEN=token)
        self.assertEqual(self.post(self.body).status_code, 201)
        self.assertEqual(self.edit(source, {"quantity_needed": "30"}).status_code, 200)

    def test_patch_changes_only_supplied_fields_and_increments_version(self):
        source = self.make_request()
        old_until, old_location = source.valid_until, source.location.copy()
        response = self.edit(source, {"quantity_needed": "25.000"})
        self.assertEqual(response.status_code, 200, response.data)
        source.refresh_from_db()
        self.assertEqual(source.quantity_needed, Decimal("25"))
        self.assertEqual(source.version, 2)
        self.assertEqual(source.valid_until, old_until)
        self.assertEqual(source.location, old_location)
        self.assertTrue(source.donate_only)

    def test_patch_empty_unknown_readonly_and_invalid_data_does_not_write(self):
        source = self.make_request()
        before = ResourceRequest.objects.values().get(pk=source.pk)
        for changes in ({}, {"status": "CLOSED"}, {"owner": self.stranger.pk}, {"quantity_fulfilled": "10"},
                        {"unknown": 1}, {"quantity_needed": "0"}, {"category": "coffee_grounds"},
                        {"quantity_needed": "25", "max_unit_price": "1.00"}):
            self.assertEqual(self.edit(source, changes).status_code, 400)
            self.assertEqual(ResourceRequest.objects.values().get(pk=source.pk), before)

    def test_patch_rejects_nonobject_payloads_without_writing(self):
        source = self.make_request()
        before = ResourceRequest.objects.values().get(pk=source.pk)
        for body in ([], [self.body], "hello", 1, None):
            response = self.client.generic("PATCH", f"{self.url}{source.pk}/", json.dumps(body),
                                           content_type="application/json")
            self.assertEqual(response.status_code, 400, response.data)
            self.assertEqual(ResourceRequest.objects.values().get(pk=source.pk), before)

    def test_locked_fields_return_conflict_before_cross_field_validation(self):
        source = self.make_request()
        self.offer(source, "1")
        before = ResourceRequest.objects.values().get(pk=source.pk)
        for changes in ({"resource_type": "COMPOSTABLE_FOOD_WASTE"}, {"category": "coffee_grounds"},
                        {"intended_use": "BSF"}):
            response = self.edit(source, changes)
            self.assertEqual(response.status_code, 409, response.data)
            self.assertEqual(response.data["errors"][0]["code"], "unit_locked")
            self.assertEqual(ResourceRequest.objects.values().get(pk=source.pk), before)

    def test_patch_authorization_public_private_participant_and_admin(self):
        public = self.make_request()
        private = self.make_request(status=RequestStatus.CLOSED)
        self.client.force_authenticate(self.stranger)
        self.assertEqual(self.edit(public, {"quantity_needed": "25"}).status_code, 403)
        self.assertEqual(self.edit(private, {"quantity_needed": "25"}).status_code, 404)
        self.offer(private, "2", status=ReservationStatus.REJECTED)
        self.client.force_authenticate(self.provider)
        self.assertEqual(self.edit(private, {"quantity_needed": "25"}).status_code, 403)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.edit(private, {"quantity_needed": "25"}).status_code, 200)
        private.refresh_from_db()
        self.assertEqual(private.owner_id, self.owner.pk)
        self.assertEqual(private.created_by_id, self.owner.pk)

    def test_quantity_floor_is_conflict_and_rolls_back_all_fields(self):
        source = self.make_request()
        self.offer(source, "5")
        self.offer(source, "4", pickup_status=PickupStatus.COMPLETED)
        before = ResourceRequest.objects.values().get(pk=source.pk)
        response = self.edit(source, {"quantity_needed": "8", "donate_only": False})
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data["errors"][0]["code"], "quantity_floor")
        self.assertEqual(ResourceRequest.objects.values().get(pk=source.pk), before)
        response = self.edit(source, {"quantity_needed": "9"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["status"], "ALLOCATED")

    def test_locked_resource_fields_after_any_reservation(self):
        changes_list = [{"unit": "kg"}, {"category": "bakery"},
                        {"resource_type": "COMPOSTABLE_FOOD_WASTE", "category": "coffee_grounds", "intended_use": "BSF"}]
        for status in ReservationStatus.values:
            source = self.make_request()
            self.offer(source, "1", status=status)
            for changes in changes_list:
                response = self.edit(source, changes)
                self.assertEqual(response.status_code, 409, response.data)
                self.assertEqual(response.data["errors"][0]["code"], "unit_locked")
            self.assertEqual(self.edit(source, {"unit": "box"}).status_code, 200)

    def test_resource_fields_can_change_without_reservations(self):
        source = self.make_request()
        response = self.edit(source, {"resource_type": "COMPOSTABLE_FOOD_WASTE", "category": "coffee_grounds",
                                      "intended_use": "COMPOSTING", "unit": "kg", "quantity_needed": "1.125"})
        self.assertEqual(response.status_code, 200, response.data)

    def test_terminal_and_archived_sources_never_reopen(self):
        for status in (RequestStatus.CLOSED, RequestStatus.EXPIRED, RequestStatus.FULFILLED):
            source = self.make_request(status=status, valid_until=self.now - timedelta(days=1), archived_at=self.now)
            response = self.edit(source, {"quantity_needed": "30", "valid_until": self.body["valid_until"]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["data"]["status"], status)
            source.refresh_from_db()
            self.assertIsNotNone(source.archived_at)

    def test_expired_open_record_does_not_reopen_when_deadline_extended(self):
        source = self.make_request(valid_until=self.now - timedelta(seconds=1))
        response = self.edit(source, {"valid_until": self.body["valid_until"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["status"], "EXPIRED")

    def test_location_partial_merge_and_coordinate_pair(self):
        source = self.make_request()
        response = self.edit(source, {"location": {"label": "New address"}})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["location"]["area"], "Depok")
        self.assertEqual(self.edit(source, {"location": {}}).status_code, 400)
        self.assertEqual(self.edit(source, {"location": {"latitude": 0}}).status_code, 400)
        self.assertEqual(self.edit(source, {"location": {"latitude": 0, "longitude": 0}}).status_code, 200)

    def test_price_preferences_merge_with_existing_values_and_leave_offers_unchanged(self):
        source = self.make_request(donate_only=False, max_unit_price="100.00")
        accepted = self.offer(source, "2", accepted_snapshot={"quantity": "2.000", "unit_price": "50.00"})
        pending = self.offer(source, "1", status=ReservationStatus.PENDING)
        before = list(Reservation.objects.filter(pk__in=(accepted.pk, pending.pk)).values())
        self.assertEqual(self.edit(source, {"donate_only": True}).status_code, 400)
        self.assertEqual(self.edit(source, {"donate_only": True, "max_unit_price": None}).status_code, 200)
        source.refresh_from_db()
        self.assertTrue(source.donate_only)
        self.assertIsNone(source.max_unit_price)
        self.assertEqual(list(Reservation.objects.filter(pk__in=(accepted.pk, pending.pk)).values()), before)

    def test_pending_offers_do_not_set_quantity_floor_and_status_recomputes(self):
        source = self.make_request(status=RequestStatus.ALLOCATED)
        self.offer(source, "10", status=ReservationStatus.PENDING)
        response = self.edit(source, {"quantity_needed": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["status"], "OPEN")

    def test_patch_bad_and_missing_ids(self):
        for pk in ("0", "-1", "abc", "9" * 30):
            self.assertEqual(self.client.patch(f"{self.url}{pk}/", {"quantity_needed": "1"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(f"{self.url}999999/", {"quantity_needed": "1"}, format="json").status_code, 404)


@skipUnlessDBFeature("has_select_for_update")
class RequestWriteConcurrencyTests(TransactionTestCase):
    make_request = RequestReadTests.make_request
    offer = RequestReadTests.offer

    def setUp(self):
        self.now = timezone.now()
        self.owner = get_user_model().objects.create_user(email="receiver@example.com", display_name="Receiver")
        self.provider = get_user_model().objects.create_user(email="provider@example.com", display_name="Provider")
        self.source = self.make_request()

    def patch_in_thread(self, changes, ready):
        close_old_connections()
        try:
            client = APIClient()
            client.force_authenticate(self.owner)
            ready()
            response = client.patch(f"/api/v1/requests/{self.source.pk}/", changes, format="json")
            return response.status_code, response.data
        finally:
            connection.close()

    def test_simultaneous_edits_preserve_each_others_fields_and_versions(self):
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.patch_in_thread, {"quantity_needed": "25"}, lambda: barrier.wait(timeout=5))
            second = pool.submit(self.patch_in_thread, {"donate_only": False}, lambda: barrier.wait(timeout=5))
            self.assertEqual(first.result(timeout=15)[0], 200)
            self.assertEqual(second.result(timeout=15)[0], 200)
        self.source.refresh_from_db()
        self.assertEqual(self.source.quantity_needed, Decimal("25"))
        self.assertFalse(self.source.donate_only)
        self.assertEqual(self.source.version, 3)

    def test_edit_rechecks_floor_after_source_locked_acceptance_commits(self):
        started = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                # Approval must acquire the same source lock before allocating.
                ResourceRequest.objects.select_for_update().get(pk=self.source.pk)
                future = pool.submit(self.patch_in_thread, {"quantity_needed": "6"}, started.set)
                self.assertTrue(started.wait(timeout=5))
                time.sleep(0.1)
                self.assertFalse(future.done())
                self.offer(self.source, "8")
            status, data = future.result(timeout=15)
        self.assertEqual(status, 409, data)
        self.assertEqual(data["errors"][0]["code"], "quantity_floor")
        self.source.refresh_from_db()
        self.assertEqual(self.source.quantity_needed, Decimal("20"))
        self.assertEqual(self.source.version, 1)


class RequestLifecycleTests(TestCase):
    make_request = RequestReadTests.make_request
    offer = RequestReadTests.offer

    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_user(email="owner@example.com", password="secret", display_name="Owner",
                                     whatsapp_number="+6281234567890")
        cls.provider = users.create_user(email="provider@example.com", password="secret", display_name="Provider",
                                        whatsapp_number="+6281234567891")
        cls.stranger = users.create_user(email="other@example.com", display_name="Other")
        cls.admin = users.create_user(email="admin@example.com", display_name="Admin", is_staff=True)

    def setUp(self):
        self.now = timezone.now()
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def close(self, source, data=None):
        return self.client.post(f"/api/v1/requests/{source.pk}/close/",
                                {"reason": "Kebutuhan dihentikan."} if data is None else data, format="json")

    def delete(self, source, data=None):
        return self.client.delete(f"/api/v1/requests/{source.pk}/", data, format="json")

    def test_close_cancels_only_pending_and_preserves_every_accepted_field_and_pickup(self):
        source = self.make_request()
        pending = self.offer(source, "3", status=ReservationStatus.PENDING)
        accepted = self.offer(source, "5", pickup_status=PickupStatus.SCHEDULED,
                              accepted_snapshot={"location": source.location, "quantity": "5.000"})
        rejected = self.offer(source, "2", status=ReservationStatus.REJECTED)
        cancelled = self.offer(source, "1", status=ReservationStatus.CANCELLED)
        preserved = list(Reservation.objects.exclude(pk=pending.pk).values())
        pickup_before = list(Pickup.objects.values())
        response = self.close(source)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["data"]["status"], "CLOSED")
        self.assertEqual(response.data["data"]["quantity_allocated"], "5.000")
        self.assertEqual(response.data["data"]["quantity_remaining"], "15.000")
        pending.refresh_from_db()
        self.assertEqual((pending.status, pending.cancellation_code), ("CANCELLED", "SOURCE_CLOSED"))
        self.assertEqual(pending.reason, "Kebutuhan dihentikan.")
        self.assertEqual(pending.version, 2)
        self.assertEqual(list(Reservation.objects.exclude(pk=pending.pk).values()), preserved)
        self.assertEqual(list(Pickup.objects.values()), pickup_before)

    def test_close_open_and_allocated_but_reject_all_terminal_states(self):
        for status in (RequestStatus.OPEN, RequestStatus.ALLOCATED):
            self.assertEqual(self.close(self.make_request(status=status)).status_code, 200)
        for status in (RequestStatus.CLOSED, RequestStatus.EXPIRED, RequestStatus.FULFILLED):
            source = self.make_request(status=status)
            pending = self.offer(source, "1", status=ReservationStatus.PENDING)
            before = ResourceRequest.objects.values().get(pk=source.pk)
            response = self.close(source)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.data["errors"][0]["code"], "invalid_transition")
            self.assertEqual(ResourceRequest.objects.values().get(pk=source.pk), before)
            pending.refresh_from_db()
            self.assertEqual(pending.status, "PENDING")

    def test_close_expired_deadline_and_archived_source_are_conflicts(self):
        for changes in ({"valid_until": self.now}, {"archived_at": self.now}):
            self.assertEqual(self.close(self.make_request(**changes)).status_code, 409)

    def test_close_fully_fulfilled_with_stale_open_status_is_conflict(self):
        source = self.make_request()
        self.offer(source, "20", pickup_status="COMPLETED")
        response = self.close(source)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["errors"][0]["code"], "invalid_transition")
        source.refresh_from_db()
        self.assertEqual(source.status, "OPEN")
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_close_reason_required_typed_bounded_and_unknown_fields_rejected(self):
        source = self.make_request()
        for data in ({}, {"reason": ""}, {"reason": "   "}, {"reason": None}, {"reason": 42},
                     {"reason": "x" * 1001}, {"reason": "ok", "status": "CLOSED"}, []):
            self.assertEqual(self.close(source, data).status_code, 400)
        source.refresh_from_db()
        self.assertEqual(source.status, "OPEN")
        self.assertEqual(AuditEvent.objects.count(), 0)
        self.assertEqual(self.close(source, {"reason": "x" * 1000}).status_code, 200)

    def test_delete_without_reservations_hard_deletes_any_status_with_no_body(self):
        for status in RequestStatus.values:
            source = self.make_request(status=status)
            pk = source.pk
            response = self.delete(source)
            self.assertEqual(response.status_code, 204, response.data)
            self.assertEqual(response.content, b"")
            self.assertFalse(ResourceRequest.objects.filter(pk=pk).exists())
            audit = AuditEvent.objects.get(entity_type="ResourceRequest", entity_id=str(pk))
            self.assertEqual(audit.action, "delete")
            self.assertIsNone(audit.summary["after"])

    def test_delete_with_any_reservation_status_archives_and_keeps_relations(self):
        for status in ReservationStatus.values:
            source = self.make_request()
            reservation = self.offer(source, "5", status=status)
            response = self.delete(source)
            self.assertEqual(response.status_code, 204, response.data)
            self.assertEqual(response.content, b"")
            source.refresh_from_db()
            reservation.refresh_from_db()
            self.assertEqual(source.status, "CLOSED")
            self.assertIsNotNone(source.archived_at)
            self.assertEqual(reservation.request_id, source.pk)
            self.assertEqual(reservation.status, "CANCELLED" if status == "PENDING" else status)

    def test_archive_preserves_terminal_statuses_and_records_expired_active_status(self):
        for status in RequestStatus.values:
            source = self.make_request(status=status, valid_until=self.now - timedelta(days=1))
            accepted = self.offer(source, "5")
            pending = self.offer(source, "1", status=ReservationStatus.PENDING)
            self.assertEqual(self.delete(source).status_code, 204)
            source.refresh_from_db()
            expected = "EXPIRED" if status in ("OPEN", "ALLOCATED") else status
            self.assertEqual(source.status, expected)
            accepted.refresh_from_db()
            self.assertEqual(accepted.status, "ACCEPTED")
            pending.refresh_from_db()
            self.assertEqual(pending.status, "CANCELLED")

    def test_delete_rejects_unknown_reason_and_nonobject_bodies(self):
        source = self.make_request()
        for body in ({"reason": "not accepted here"}, {"owner": self.owner.pk}, [1], "text"):
            self.assertEqual(self.delete(source, body).status_code, 400)
        self.assertTrue(ResourceRequest.objects.filter(pk=source.pk).exists())
        self.assertEqual(self.delete(source, {}).status_code, 204)

    def test_repeated_close_conflicts_and_repeated_archive_is_noop(self):
        source = self.make_request()
        self.offer(source, "1", status=ReservationStatus.PENDING)
        self.assertEqual(self.close(source).status_code, 200)
        count = AuditEvent.objects.count()
        self.assertEqual(self.close(source).status_code, 409)
        self.assertEqual(AuditEvent.objects.count(), count)
        self.assertEqual(self.delete(source).status_code, 204)
        before = ResourceRequest.objects.values().get(pk=source.pk)
        count = AuditEvent.objects.count()
        self.assertEqual(self.delete(source).status_code, 204)
        self.assertEqual(ResourceRequest.objects.values().get(pk=source.pk), before)
        self.assertEqual(AuditEvent.objects.count(), count)

    def test_authorization_and_hidden_resources(self):
        public = self.make_request()
        private = self.make_request(status="CLOSED")
        self.client.force_authenticate(self.stranger)
        for action in (self.close, self.delete):
            self.assertEqual(action(public).status_code, 403)
            self.assertEqual(action(private).status_code, 404)
        self.offer(private, "1", status="REJECTED")
        self.client.force_authenticate(self.provider)
        self.assertEqual(self.delete(private).status_code, 403)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.close(public).status_code, 200)
        self.assertEqual(self.delete(private).status_code, 204)
        self.assertEqual(AuditEvent.objects.filter(actor=self.admin, entity_type="ResourceRequest").count(), 2)

    def test_anonymous_and_inactive_writes_rejected(self):
        source = self.make_request()
        for user in (None, self.stranger):
            if user:
                user.is_active = False
            self.client.force_authenticate(user)
            self.assertEqual(self.close(source).status_code, 401)
            self.assertEqual(self.delete(source).status_code, 401)

    def test_actual_session_csrf_protection(self):
        source = self.make_request()
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(self.owner)
        self.assertEqual(self.close(source).status_code, 403)
        self.assertEqual(self.delete(source).status_code, 403)
        csrf = self.client.get("/api/v1/auth/csrf/")
        self.client.credentials(HTTP_X_CSRFTOKEN=csrf.data["data"]["csrf_token"])
        self.assertEqual(self.close(source).status_code, 200)
        self.assertEqual(self.delete(source).status_code, 204)

    def test_audit_has_actor_reason_before_after_without_contact_or_snapshot(self):
        source = self.make_request()
        pending = self.offer(source, "1", status="PENDING")
        self.close(source)
        audit = AuditEvent.objects.get(entity_type="ResourceRequest", entity_id=str(source.pk))
        self.assertEqual(audit.actor_id, self.owner.pk)
        self.assertEqual(audit.summary["reason"], "Kebutuhan dihentikan.")
        self.assertEqual(audit.summary["before"]["status"], "OPEN")
        self.assertEqual(audit.summary["after"]["status"], "CLOSED")
        self.assertTrue(AuditEvent.objects.filter(entity_type="Reservation", entity_id=str(pending.pk)).exists())
        self.assertNotIn(self.owner.whatsapp_number, str(audit.summary))

    def test_audit_failure_rolls_back_close_archive_and_hard_delete(self):
        for action, history in ((self.close, True), (self.delete, True), (self.delete, False)):
            source = self.make_request()
            if history:
                self.offer(source, "1", status="PENDING")
            before = ResourceRequest.objects.values().get(pk=source.pk)
            before_offers = list(source.reservations.values())
            with patch("requests_app.views._audit", side_effect=RuntimeError("Audit unavailable")):
                with self.assertLogs("common.exceptions", level="ERROR"):
                    self.assertEqual(action(source).status_code, 500)
            self.assertEqual(ResourceRequest.objects.values().get(pk=source.pk), before)
            self.assertEqual(list(source.reservations.values()), before_offers)

    def test_archived_history_visible_only_owner_participant_admin(self):
        source = self.make_request()
        self.offer(source, "1", status="REJECTED")
        self.delete(source)
        for user in (self.owner, self.provider, self.admin):
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get(f"/api/v1/requests/{source.pk}/").status_code, 200)
        for user in (None, self.stranger):
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get(f"/api/v1/requests/{source.pk}/").status_code, 404)

    def test_completed_or_released_allocation_does_not_reopen_closed_or_expired_source(self):
        for status in ("CLOSED", "EXPIRED"):
            source = self.make_request(status=status)
            accepted = self.offer(source, "20", pickup_status="SCHEDULED")
            Pickup.objects.filter(reservation=accepted).update(status="COMPLETED")
            self.assertEqual(self.client.get(f"/api/v1/requests/{source.pk}/").data["data"]["status"], status)
            Reservation.objects.filter(pk=accepted.pk).update(status="CANCELLED")
            self.assertEqual(self.client.get(f"/api/v1/requests/{source.pk}/").data["data"]["status"], status)

    def test_closed_and_archived_request_still_allows_accepted_pickup_scheduling(self):
        for action in (self.close, self.delete):
            self.client.force_authenticate(self.owner)
            source = self.make_request()
            accepted = self.offer(source, "5", accepted_snapshot={"location": source.location})
            action(source)
            self.client.force_authenticate(self.provider)
            response = self.client.post(f"/api/v1/reservations/{accepted.pk}/pickup/",
                                        {"scheduled_at": (self.now + timedelta(hours=1)).isoformat()}, format="json")
            self.assertEqual(response.status_code, 201, response.data)

    def test_malformed_missing_ids_and_malformed_json(self):
        for pk in ("0", "-1", "abc", "9" * 30):
            self.assertEqual(self.client.post(f"/api/v1/requests/{pk}/close/", {"reason": "ok"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/v1/requests/999999/close/", {"reason": "ok"}, format="json").status_code, 404)
        source = self.make_request()
        self.assertEqual(self.client.generic("POST", f"/api/v1/requests/{source.pk}/close/", "{",
                                             content_type="application/json").status_code, 400)


@skipUnlessDBFeature("has_select_for_update")
class RequestLifecycleConcurrencyTests(TransactionTestCase):
    make_request = RequestReadTests.make_request
    offer = RequestReadTests.offer

    def setUp(self):
        self.now = timezone.now()
        self.owner = get_user_model().objects.create_user(email="owner@example.com", display_name="Owner")
        self.provider = get_user_model().objects.create_user(email="provider@example.com", display_name="Provider")
        self.source = self.make_request()

    def action(self, method, ready):
        close_old_connections()
        try:
            client = APIClient()
            client.force_authenticate(self.owner)
            ready()
            url = f"/api/v1/requests/{self.source.pk}/"
            response = client.post(url + "close/", {"reason": "Close"}, format="json") if method == "close" else client.delete(url)
            return response.status_code
        finally:
            connection.close()

    def test_simultaneous_close_has_one_success_and_one_conflict(self):
        self.offer(self.source, "1", status="PENDING")
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.action, "close", lambda: barrier.wait(timeout=5)) for _ in range(2)]
            self.assertEqual(sorted(f.result(timeout=15) for f in futures), [200, 409])
        self.assertEqual(AuditEvent.objects.filter(entity_type="ResourceRequest", action="close").count(), 1)

    def test_close_waits_for_acceptance_and_does_not_cancel_newly_accepted(self):
        pending = self.offer(self.source, "5", status="PENDING")
        started = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                # Simulate the documented source-first acceptance transaction.
                ResourceRequest.objects.select_for_update().get(pk=self.source.pk)
                future = pool.submit(self.action, "close", started.set)
                self.assertTrue(started.wait(timeout=5))
                pending.status = "ACCEPTED"
                pending.save(update_fields=["status"])
            self.assertEqual(future.result(timeout=15), 200)
        pending.refresh_from_db()
        self.assertEqual(pending.status, "ACCEPTED")
        self.assertEqual(pending.cancellation_code, "")

    def test_simultaneous_archive_preserves_history_and_audits_once(self):
        self.offer(self.source, "1", status="REJECTED")
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.action, "delete", lambda: barrier.wait(timeout=5)) for _ in range(2)]
            self.assertEqual([f.result(timeout=15) for f in futures], [204, 204])
        self.assertEqual(AuditEvent.objects.filter(entity_type="ResourceRequest", action="archive").count(), 1)
        self.assertEqual(Reservation.objects.filter(request=self.source).count(), 1)


class RequestModerationTests(TestCase):
    url = "/api/v1/admin/requests/"
    make_request = RequestReadTests.make_request
    offer = RequestReadTests.offer

    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.owner = users.create_user(email="owner@example.com", display_name="Owner", whatsapp_number="+6281234567890")
        cls.provider = users.create_user(email="provider@example.com", display_name="Provider", whatsapp_number="+6281234567891")
        cls.admin = users.create_user(email="admin@example.com", display_name="Moderator", is_staff=True)
        cls.inactive = users.create_user(email="inactive@example.com", display_name="Inactive", is_active=False)

    def setUp(self):
        self.now = timezone.now()
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.body = {
            "resource_type": "PACKAGED_FOOD_SURPLUS", "category": "prepared_meal",
            "intended_use": "HUMAN_CONSUMPTION", "quantity_needed": "20.000", "unit": "box",
            "location": {"label": "Kampus UI", "area": "Depok", "latitude": -6.36, "longitude": 106.82},
            "valid_until": (self.now + timedelta(days=1)).isoformat(),
            "owner_id": self.owner.pk, "reason": "Moderator correction",
        }

    def detail_url(self, source):
        return f"{self.url}{source.pk}/"

    def edit(self, source, data):
        return self.client.patch(self.detail_url(source), data, format="json")

    def close(self, source, data=None):
        return self.client.post(self.detail_url(source) + "close/",
                                {"reason": "Moderator closure"} if data is None else data, format="json")

    def delete(self, source, data=None):
        return self.client.delete(self.detail_url(source),
                                  {"reason": "Moderator removal"} if data is None else data, format="json")

    def test_all_six_operations_require_active_admin(self):
        source = self.make_request()
        for user, status in ((None, 401), (self.owner, 403), (self.inactive, 401)):
            self.client.force_authenticate(user)
            responses = [self.client.get(self.url), self.client.get(self.detail_url(source)),
                         self.client.post(self.url, self.body, format="json"),
                         self.edit(source, {"quantity_needed": "25", "reason": "Correction"}),
                         self.close(source), self.delete(source)]
            for response in responses:
                self.assertEqual(response.status_code, status, response.data)
        source.refresh_from_db()
        self.assertEqual(source.version, 1)
        self.assertEqual(AuditEvent.objects.count(), 0)
        self.assertEqual(ResourceRequest.objects.count(), 1)
        self.admin.is_active = False
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(self.url).status_code, 401)

    def test_list_detail_include_every_status_expired_and_archived_without_contact(self):
        sources = [self.make_request(status=status, archived_at=self.now,
                                     valid_until=self.now - timedelta(days=1)) for status in RequestStatus.values]
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual({r["id"] for r in response.data["data"]["results"]}, {r.pk for r in sources})
        for source in sources:
            response = self.client.get(self.detail_url(source))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["data"]["owner"], {"id": self.owner.pk, "display_name": "Owner"})
            self.assertNotIn("whatsapp", str(response.data))
            self.assertNotIn("email", str(response.data))
        self.assertEqual(AuditEvent.objects.count(), 0)
        self.assertTrue(all(v == 1 for v in ResourceRequest.objects.values_list("version", flat=True)))

    def test_admin_filters_and_pagination(self):
        target = self.make_request(status="CLOSED", archived_at=self.now)
        self.make_request(owner=self.provider, status="EXPIRED")
        filters = {"q": "kampus", "resource_type": "PACKAGED_FOOD_SURPLUS", "category": "prepared_meal",
                   "intended_use": "HUMAN_CONSUMPTION", "area": "DEPOK", "donate_only": "true",
                   "status": "CLOSED", "owner_id": self.owner.pk, "archived": "true"}
        response = self.client.get(self.url, filters)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual([r["id"] for r in response.data["data"]["results"]], [target.pk])
        self.assertEqual(self.client.get(self.url, {"archived": "false"}).data["data"]["count"], 1)
        self.assertEqual(self.client.get(self.url, {"owner_id": 999999}).data["data"]["count"], 0)
        for ordering in ("created_at", "-created_at", "valid_until", "-valid_until"):
            response = self.client.get(self.url, {"ordering": ordering, "page_size": 1})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.data["data"]["results"]), 1)
            self.assertIsNotNone(response.data["data"]["next"])
        for page in (0, -1, 9):
            response = self.client.get(self.url, {"page": page})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.data["errors"][0]["code"], "page_not_found")
        for query in ({"owner_id": 0}, {"owner_id": "1.0"}, {"owner_id": "9" * 30}, {"archived": "yes"},
                      {"scope": "mine"}, {"status": "INVALID"}, {"page_size": 101}, {"unknown": 1}):
            self.assertEqual(self.client.get(self.url, query).status_code, 400)
        self.assertEqual(self.client.get(self.url + "?owner_id=1&owner_id=2").status_code, 400)

    def test_create_on_behalf_separates_owner_creator_and_audits(self):
        response = self.client.post(self.url, self.body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        source = ResourceRequest.objects.get(pk=response.data["data"]["id"])
        self.assertEqual((source.owner_id, source.created_by_id), (self.owner.pk, self.admin.pk))
        self.assertFalse(source.donate_only)
        self.assertIsNone(source.max_unit_price)
        self.assertEqual(response.data["data"]["quantity_remaining"], "20.000")
        audit = AuditEvent.objects.get()
        self.assertEqual((audit.actor_id, audit.action), (self.admin.pk, "create"))
        self.assertEqual(audit.summary["reason"], self.body["reason"])
        self.assertIsNone(audit.summary["before"])
        self.assertEqual(audit.summary["after"]["owner"]["id"], self.owner.pk)
        self.assertEqual(audit.summary["after"]["created_by_id"], self.admin.pk)
        self.assertNotIn(self.owner.whatsapp_number, str(audit.summary))

    def test_create_owner_missing_inactive_invalid_and_required(self):
        for owner, status, code in ((999999, 404, "not_found"), (self.inactive.pk, 409, "owner_inactive"),
                                    (0, 400, None), (True, 400, None), (1.0, 400, None), ("9" * 30, 400, None)):
            response = self.client.post(self.url, {**self.body, "owner_id": owner}, format="json")
            self.assertEqual(response.status_code, status, response.data)
            if code:
                self.assertEqual(response.data["errors"][0]["code"], code)
        for missing in self.body:
            body = {k: v for k, v in self.body.items() if k != missing}
            self.assertEqual(self.client.post(self.url, body, format="json").status_code, 400)
        self.assertEqual(ResourceRequest.objects.count(), 0)
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_create_uses_owner_resource_quantity_price_location_and_deadline_rules(self):
        for changes in ({"category": "coffee_grounds"}, {"intended_use": "COMPOSTING"},
                        {"quantity_needed": "0"}, {"quantity_needed": "1.1"}, {"quantity_needed": 2},
                        {"donate_only": True, "max_unit_price": "1.00"}, {"donate_only": "true"},
                        {"max_unit_price": "-1"}, {"location": {}}, {"valid_until": self.now.isoformat()},
                        {"status": "CLOSED"}, {"created_by": self.owner.pk}, {"owner": self.owner.pk}):
            response = self.client.post(self.url, {**self.body, **changes}, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(ResourceRequest.objects.count(), 0)

    def test_reason_required_typed_bounded_on_all_writes_including_partial_patch(self):
        source = self.make_request()
        for reason in (None, "", "   ", 42, "x" * 1001):
            self.assertEqual(self.client.post(self.url, {**self.body, "reason": reason}, format="json").status_code, 400)
            self.assertEqual(self.edit(source, {"quantity_needed": "25", "reason": reason}).status_code, 400)
            self.assertEqual(self.close(source, {"reason": reason}).status_code, 400)
            self.assertEqual(self.delete(source, {"reason": reason}).status_code, 400)
        self.assertEqual(self.edit(source, {"quantity_needed": "25"}).status_code, 400)
        self.assertEqual(self.edit(source, {"reason": "Correction only"}).status_code, 400)
        self.assertEqual(self.close(source, {}).status_code, 400)
        self.assertEqual(self.delete(source, {}).status_code, 400)
        self.assertEqual(self.client.delete(self.detail_url(source)).status_code, 400)
        source.refresh_from_db()
        self.assertEqual(source.version, 1)
        self.assertEqual(AuditEvent.objects.count(), 0)
        self.assertEqual(self.edit(source, {"quantity_needed": "25", "reason": "x" * 1000}).status_code, 200)

    def test_patch_audits_before_after_and_keeps_owner_creator_and_accepted_snapshot(self):
        source = self.make_request()
        accepted = self.offer(source, "5", accepted_snapshot={"unit": "box", "location": source.location})
        before_reservation = Reservation.objects.values().get(pk=accepted.pk)
        response = self.edit(source, {"quantity_needed": "25", "donate_only": False,
                                      "max_unit_price": "500.00", "reason": "Preference correction"})
        self.assertEqual(response.status_code, 200, response.data)
        source.refresh_from_db()
        self.assertEqual((source.owner_id, source.created_by_id), (self.owner.pk, self.owner.pk))
        self.assertEqual(Reservation.objects.values().get(pk=accepted.pk), before_reservation)
        self.assertEqual(response.data["data"]["quantity_remaining"], "20.000")
        audit = AuditEvent.objects.get(action="update")
        self.assertEqual(audit.summary["before"]["quantity_needed"], "20.000")
        self.assertEqual(audit.summary["after"]["quantity_needed"], "25.000")
        self.assertEqual(audit.summary["after"]["version"], 2)
        self.assertEqual(audit.summary["reason"], "Preference correction")
        for field in ("owner_id", "created_by", "status", "quantity_allocated", "accepted_snapshot"):
            self.assertEqual(self.edit(source, {field: 1, "reason": "Correction"}).status_code, 400)

    def test_patch_quantity_floor_and_resource_locks_are_atomic(self):
        source = self.make_request()
        self.offer(source, "5")
        self.offer(source, "3", pickup_status="COMPLETED")
        before = ResourceRequest.objects.values().get(pk=source.pk)
        for changes, code in (({"quantity_needed": "7"}, "quantity_floor"), ({"unit": "kg"}, "unit_locked"),
                              ({"category": "bakery"}, "unit_locked"),
                              ({"resource_type": "COMPOSTABLE_FOOD_WASTE"}, "unit_locked"),
                              ({"intended_use": "COMPOSTING"}, "unit_locked")):
            response = self.edit(source, {**changes, "reason": "Correction"})
            self.assertEqual(response.status_code, 409, response.data)
            self.assertEqual(response.data["errors"][0]["code"], code)
            self.assertEqual(ResourceRequest.objects.values().get(pk=source.pk), before)
        self.assertEqual(AuditEvent.objects.count(), 0)
        response = self.edit(source, {"quantity_needed": "8", "reason": "Correction"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["status"], "ALLOCATED")

    def test_patch_terminal_and_archived_records_does_not_reopen(self):
        for status in ("CLOSED", "EXPIRED", "FULFILLED"):
            source = self.make_request(status=status, archived_at=self.now)
            response = self.edit(source, {"quantity_needed": "25", "reason": "History correction"})
            self.assertEqual(response.status_code, 200)
            source.refresh_from_db()
            self.assertEqual(source.status, status)
            self.assertIsNotNone(source.archived_at)

    def test_close_and_archive_preserve_accepted_reservation_pickup_and_history(self):
        for action in (self.close, self.delete):
            source = self.make_request()
            pending = self.offer(source, "1", status="PENDING")
            accepted = self.offer(source, "5", pickup_status="SCHEDULED", accepted_snapshot={"quantity": "5.000"})
            accepted_before = Reservation.objects.values().get(pk=accepted.pk)
            pickup_before = Pickup.objects.values().get(reservation=accepted)
            response = action(source)
            self.assertEqual(response.status_code, 200 if action == self.close else 204, response.data)
            source.refresh_from_db()
            pending.refresh_from_db()
            self.assertEqual(source.status, "CLOSED")
            self.assertEqual((pending.status, pending.cancellation_code), ("CANCELLED", "SOURCE_CLOSED"))
            self.assertEqual(Reservation.objects.values().get(pk=accepted.pk), accepted_before)
            self.assertEqual(Pickup.objects.values().get(reservation=accepted), pickup_before)
            audit = AuditEvent.objects.get(entity_type="ResourceRequest", entity_id=str(source.pk))
            self.assertEqual(audit.actor_id, self.admin.pk)
            self.assertEqual(audit.summary["before"]["status"], "OPEN")
            self.assertEqual(audit.summary["after"]["status"], "CLOSED")
            self.assertEqual(audit.summary["after"]["quantity_allocated"], "5.000")
            self.assertEqual(pending.reason, audit.summary["reason"])
            self.client.force_authenticate(self.owner)
            self.assertEqual(self.client.get(f"/api/v1/requests/{source.pk}/").status_code, 200)
            self.client.force_authenticate(self.admin)
            if action == self.delete:
                self.assertEqual(response.content, b"")
                count = AuditEvent.objects.count()
                self.assertEqual(self.delete(source).status_code, 204)
                self.assertEqual(AuditEvent.objects.count(), count)

    def test_hard_delete_and_archive_with_rejected_cancelled_history(self):
        source = self.make_request()
        pk = source.pk
        response = self.delete(source)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertFalse(ResourceRequest.objects.filter(pk=pk).exists())
        audit = AuditEvent.objects.get(entity_type="ResourceRequest", entity_id=str(pk))
        self.assertEqual(audit.summary["reason"], "Moderator removal")
        self.assertIsNone(audit.summary["after"])
        for status in ("REJECTED", "CANCELLED"):
            source = self.make_request(status="EXPIRED")
            self.offer(source, "1", status=status)
            self.assertEqual(self.delete(source).status_code, 204)
            source.refresh_from_db()
            self.assertEqual(source.status, "EXPIRED")
            self.assertIsNotNone(source.archived_at)
            self.assertEqual(source.reservations.count(), 1)

    def test_close_terminal_or_fully_fulfilled_returns_409_without_audit(self):
        for status in ("CLOSED", "EXPIRED", "FULFILLED"):
            response = self.close(self.make_request(status=status))
            self.assertEqual(response.status_code, 409)
        source = self.make_request()
        self.offer(source, "20", pickup_status="COMPLETED")
        self.assertEqual(self.close(source).status_code, 409)
        self.assertEqual(AuditEvent.objects.count(), 0)

    def test_all_admin_writes_require_real_session_csrf(self):
        source = self.make_request()
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(self.admin)
        responses = [self.client.post(self.url, self.body, format="json"),
                     self.edit(source, {"quantity_needed": "25", "reason": "Correction"}),
                     self.close(source), self.delete(source)]
        for response in responses:
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.data["errors"][0]["code"], "csrf_failed")
        self.assertEqual(AuditEvent.objects.count(), 0)
        csrf = self.client.get("/api/v1/auth/csrf/")
        self.client.credentials(HTTP_X_CSRFTOKEN=csrf.data["data"]["csrf_token"])
        self.assertEqual(self.client.post(self.url, self.body, format="json").status_code, 201)
        self.assertEqual(self.edit(source, {"quantity_needed": "25", "reason": "Correction"}).status_code, 200)
        self.assertEqual(self.close(source).status_code, 200)
        self.assertEqual(self.delete(source).status_code, 204)

    def test_invalid_ids_bodies_unknown_fields_and_json_parser(self):
        for pk in ("0", "-1", "abc", "9" * 30):
            url = f"{self.url}{pk}/"
            for response in (self.client.get(url), self.client.patch(url, {"reason": "r"}, format="json"),
                             self.client.delete(url, {"reason": "r"}, format="json"),
                             self.client.post(url + "close/", {"reason": "r"}, format="json")):
                self.assertEqual(response.status_code, 400, response.data)
        url = self.url + "999999/"
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.patch(url, {"quantity_needed": "25", "reason": "r"}, format="json").status_code, 404)
        self.assertEqual(self.client.delete(url, {"reason": "r"}, format="json").status_code, 404)
        self.assertEqual(self.client.post(url + "close/", {"reason": "r"}, format="json").status_code, 404)
        source = self.make_request()
        for body in ([], "text", 42, None):
            for method, target in (("POST", self.url), ("PATCH", self.detail_url(source)),
                                   ("DELETE", self.detail_url(source)), ("POST", self.detail_url(source) + "close/")):
                response = self.client.generic(method, target, json.dumps(body), content_type="application/json")
                self.assertEqual(response.status_code, 400, response.data)
        for action in (self.close, self.delete):
            self.assertEqual(action(source, {"reason": "r", "unknown": 1}).status_code, 400)
        self.assertEqual(self.client.generic("POST", self.url, "{", content_type="application/json").status_code, 400)
        self.assertEqual(self.client.generic("POST", self.url, "quantity_needed=20", content_type="application/x-www-form-urlencoded").status_code, 415)

    def test_audit_failure_rolls_back_every_admin_write(self):
        source = self.make_request()
        self.offer(source, "1", status="PENDING")
        empty = self.make_request()
        operations = [lambda: self.client.post(self.url, self.body, format="json"),
                      lambda: self.edit(source, {"quantity_needed": "25", "reason": "r"}),
                      lambda: self.close(source), lambda: self.delete(source), lambda: self.delete(empty)]
        for operation in operations:
            before = list(ResourceRequest.objects.values())
            offers = list(Reservation.objects.values())
            with patch("requests_app.views._audit", side_effect=RuntimeError("Audit unavailable")):
                with self.assertLogs("common.exceptions", level="ERROR"):
                    self.assertEqual(operation().status_code, 500)
            self.assertEqual(list(ResourceRequest.objects.values()), before)
            self.assertEqual(list(Reservation.objects.values()), offers)
            self.assertEqual(AuditEvent.objects.count(), 0)

    def test_accepted_pickup_can_be_scheduled_after_admin_close_or_archive(self):
        for action in (self.close, self.delete):
            source = self.make_request()
            accepted = self.offer(source, "5", accepted_snapshot={"location": source.location})
            action(source)
            self.client.force_authenticate(self.provider)
            response = self.client.post(f"/api/v1/reservations/{accepted.pk}/pickup/",
                                        {"scheduled_at": (self.now + timedelta(hours=1)).isoformat()}, format="json")
            self.assertEqual(response.status_code, 201, response.data)
            self.client.force_authenticate(self.admin)


@skipUnlessDBFeature("has_select_for_update")
class RequestModerationConcurrencyTests(TransactionTestCase):
    make_request = RequestReadTests.make_request
    offer = RequestReadTests.offer

    def setUp(self):
        self.now = timezone.now()
        self.owner = get_user_model().objects.create_user(email="owner@example.com", display_name="Owner")
        self.provider = get_user_model().objects.create_user(email="provider@example.com", display_name="Provider")
        self.admin = get_user_model().objects.create_user(email="admin@example.com", display_name="Admin", is_staff=True)
        self.source = self.make_request()

    def action(self, method, ready, quantity="25"):
        close_old_connections()
        try:
            client = APIClient()
            client.force_authenticate(self.admin)
            ready()
            url = f"/api/v1/admin/requests/{self.source.pk}/"
            if method == "close":
                response = client.post(url + "close/", {"reason": "Moderation"}, format="json")
            elif method == "edit":
                response = client.patch(url, {"quantity_needed": quantity, "reason": "Correction"}, format="json")
            else:
                response = client.delete(url, {"reason": "Removal"}, format="json")
            return response.status_code
        finally:
            connection.close()

    def test_simultaneous_admin_close_audits_once(self):
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.action, "close", lambda: barrier.wait(timeout=5)) for _ in range(2)]
            self.assertEqual(sorted(f.result(timeout=15) for f in futures), [200, 409])
        self.assertEqual(AuditEvent.objects.filter(entity_type="ResourceRequest", action="close").count(), 1)

    def test_simultaneous_admin_archive_preserves_history_and_audits_once(self):
        self.offer(self.source, "1", status="REJECTED")
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.action, "delete", lambda: barrier.wait(timeout=5)) for _ in range(2)]
            self.assertEqual([f.result(timeout=15) for f in futures], [204, 204])
        self.assertEqual(AuditEvent.objects.filter(entity_type="ResourceRequest", action="archive").count(), 1)
        self.assertEqual(self.source.reservations.count(), 1)

    def test_admin_edit_waits_for_allocation_and_preserves_floor(self):
        started = Event()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with transaction.atomic():
                ResourceRequest.objects.select_for_update().get(pk=self.source.pk)
                future = pool.submit(self.action, "edit", started.set, "6")
                self.assertTrue(started.wait(timeout=5))
                self.offer(self.source, "8")
            self.assertEqual(future.result(timeout=15), 409)
        self.source.refresh_from_db()
        self.assertEqual(self.source.quantity_needed, Decimal("20"))
        self.assertEqual(AuditEvent.objects.count(), 0)
