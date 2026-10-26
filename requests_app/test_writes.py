from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Event
import json
import time

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone
from rest_framework.test import APIClient

from pickups.models import PickupStatus
from reservations.models import Reservation, ReservationStatus
from shared.enums import ResourceType
from .models import ResourceRequest, RequestStatus
from . import tests as read_tests


class RequestWriteTests(TestCase):
    url = "/api/v1/requests/"
    make_request = read_tests.RequestReadTests.make_request
    offer = read_tests.RequestReadTests.offer

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
    make_request = read_tests.RequestReadTests.make_request
    offer = read_tests.RequestReadTests.offer

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
