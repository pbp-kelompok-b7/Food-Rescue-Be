from types import SimpleNamespace
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient
from rest_framework.exceptions import ValidationError
from common.pagination import ContractPagination
from common.exceptions import exception_handler


class ContractScaffoldTests(SimpleTestCase):
    def setUp(self):
        self.client = APIClient()

    def test_private_and_admin_operations_are_protected(self):
        self.assertEqual(self.client.get("/api/v1/reservations/").status_code, 401)
        self.client.force_authenticate(SimpleNamespace(pk=2, is_authenticated=True, is_staff=False))
        self.assertEqual(self.client.get("/api/v1/admin/users/").status_code, 403)

    def test_anonymous_auth_writes_require_csrf(self):
        self.client = APIClient(enforce_csrf_checks=True)
        for operation in ("register", "login"):
            response = self.client.post(f"/api/v1/auth/{operation}/", {}, format="json")
            self.assertEqual(response.status_code, 403)

    def test_pagination_limits_and_validation_error_envelope(self):
        pagination = ContractPagination()
        self.assertEqual(pagination.get_page_size(SimpleNamespace(query_params={})), 20)
        self.assertEqual(pagination.get_page_size(SimpleNamespace(query_params={"page_size": "100"})), 100)
        for raw in ("0", "101", "abc"):
            with self.assertRaises(ValidationError):
                pagination.get_page_size(SimpleNamespace(query_params={"page_size": raw}))
        response = exception_handler(ValidationError({"quantity": ["Invalid quantity."]}), {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["errors"][0]["field"], "quantity")


class PublicBrowseTests(TestCase):
    def test_request_browse_returns_empty_results(self):
        response = APIClient().get("/api/v1/requests/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["success"])
        self.assertEqual(response.data["data"]["results"], [])

class SharedContractRegressionTests(SimpleTestCase):
    def test_rfc3339_rejects_out_of_range_offset_components(self):
        from common.serializers import RFC3339DateTimeField
        field = RFC3339DateTimeField()
        for offset in ("+00:60", "-00:99", "+24:00"):
            with self.subTest(offset=offset), self.assertRaises(ValidationError):
                field.run_validation("2099-01-01T12:00:00" + offset)

    def test_claim_quantity_rejects_json_numbers(self):
        from reservations.serializers import ListingClaimCreateSerializer
        field = ListingClaimCreateSerializer().fields["quantity"]
        for value in (1, 1.5, True, "1e2", " 1 "):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                field.run_validation(value)
        self.assertEqual(str(field.run_validation("1.125")), "1.125")

    def test_offer_deadline_requires_rfc3339_offset(self):
        from reservations.serializers import RequestOfferCreateSerializer
        field = RequestOfferCreateSerializer().fields["resource_deadline"]
        for value in ("2099-01-01T12:00:00", "2099-01-01", "2099-01-01 12:00:00+07:00"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                field.run_validation(value)
        value = field.run_validation("2099-01-01T12:00:00+07:00")
        self.assertEqual(value.utcoffset().total_seconds(), 0)

    def test_nested_offer_location_rejects_unknown_fields(self):
        from reservations.serializers import LocationSerializer
        serializer = LocationSerializer(data={"label": "Kampus", "area": "Depok",
                                               "latitude": -6.3, "longitude": 106.8,
                                               "whatsapp_number": "+628120000001"})
        self.assertFalse(serializer.is_valid())

    def test_private_error_responses_are_not_cacheable(self):
        response = APIClient().get("/api/v1/reservations/999999/")
        self.assertEqual(response.status_code, 401)
        self.assertIn("no-store", response.get("Cache-Control", ""))

    def test_unknown_api_routes_use_the_error_envelope(self):
        response = APIClient().get("/api/v1/reservations/not-an-id/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(set(response.json()), {"success", "message", "errors"})
        self.assertIsNone(response.json()["errors"][0]["field"])
        self.assertIn("no-store", response["Cache-Control"])

    def test_nonfield_errors_have_null_field_and_204_has_no_body(self):
        from common.responses import success
        response = exception_handler(ValidationError({"non_field_errors": ["Invalid object."]}), {})
        self.assertIsNone(response.data["errors"][0]["field"])
        response = success({"ignored": True}, status=204)
        from rest_framework.renderers import JSONRenderer
        response.accepted_renderer = JSONRenderer()
        response.accepted_media_type = "application/json"
        response.renderer_context = {}
        response.render()
        self.assertEqual(response.content, b"")


class SharedIntegrationTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        from datetime import timedelta
        from listings.models import ResourceListing
        User = get_user_model()
        self.owner = User.objects.create_user(email="shared-owner@example.test", display_name="Owner",
                                              whatsapp_number="+628120000001")
        self.receiver = User.objects.create_user(email="shared-receiver@example.test", display_name="Receiver",
                                                 whatsapp_number="+628120000002")
        self.outsider = User.objects.create_user(email="shared-outsider@example.test", display_name="Outsider")
        self.admin = User.objects.create_user(email="shared-admin@example.test", display_name="Staff", is_staff=True)
        self.source = ResourceListing.objects.create(owner=self.owner, created_by=self.owner,
            resource_type="PACKAGED_FOOD_SURPLUS", title="Surplus", category="prepared_meal",
            description="Makanan", quantity_total="20", unit="box", mode="DONATE", unit_price="0",
            condition="Baik", suitability=["HUMAN_CONSUMPTION"],
            location={"label": "Kampus", "area": "Depok", "latitude": -6.3, "longitude": 106.8},
            available_until=timezone.now()+timedelta(days=2))
        self.client = APIClient()
        self.client.force_authenticate(self.receiver)

    def claim(self):
        response = self.client.post(f"/api/v1/listings/{self.source.pk}/claims/", {"quantity": "2"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data["data"]["id"]

    def test_privacy_and_no_store_across_accounts_and_statuses(self):
        from reservations.models import Reservation
        pk = self.claim()
        url = f"/api/v1/reservations/{pk}/"
        response = self.client.get(url)
        self.assertIsNone(response.data["data"]["counterparty_contact"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn(self.owner.whatsapp_number, str(response.data))
        self.client.force_authenticate(self.outsider)
        for method, data in (("get", None), ("patch", {"quantity": "3", "expected_version": 1})):
            response = getattr(self.client, method)(url, data, format="json")
            self.assertEqual(response.status_code, 404)
            self.assertIn("no-store", response["Cache-Control"])
        self.client.force_authenticate(self.admin)
        response = self.client.get(f"/api/v1/admin/reservations/{pk}/")
        self.assertEqual(response.data["data"]["provider_contact"]["whatsapp_number"], self.owner.whatsapp_number)
        self.client.force_authenticate(self.owner)
        response = self.client.post(url+"accept/", {"expected_version": 1}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.client.force_authenticate(self.receiver)
        self.assertEqual(self.client.get(url).data["data"]["counterparty_contact"]["whatsapp_number"], self.owner.whatsapp_number)
        Reservation.objects.filter(pk=pk).update(status="CANCELLED")
        self.assertIsNone(self.client.get(url).data["data"]["counterparty_contact"])

    def test_invalid_decimal_and_unknown_body_never_create_reservations(self):
        from reservations.models import Reservation
        for data in ({"quantity": 2}, {"quantity": "2e0"}, {"quantity": "1.1"},
                     {"quantity": "2", "owner": self.outsider.pk}, {"quantity": "0.0001"}):
            response = self.client.post(f"/api/v1/listings/{self.source.pk}/claims/", data, format="json")
            self.assertEqual(response.status_code, 400, response.data)
        self.assertFalse(Reservation.objects.exists())

    def test_inbox_rejects_unknown_repeated_blank_and_malformed_pagination(self):
        for query in ("secret=yes", "status=PENDING&status=ACCEPTED", "role=", "page_size=+2", "page_size=1.5", "page=last"):
            response = self.client.get("/api/v1/reservations/?"+query)
            self.assertEqual(response.status_code, 400, response.data)
        response = self.client.get("/api/v1/reservations/?page=999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["errors"][0]["code"], "page_not_found")

    def test_audit_failure_rolls_back_reservation_creation_and_acceptance(self):
        from unittest.mock import patch
        from reservations.models import Reservation
        self.client.raise_request_exception = False
        with patch("shared.models.AuditEvent.objects.create", side_effect=RuntimeError("audit unavailable")):
            response = self.client.post(f"/api/v1/listings/{self.source.pk}/claims/", {"quantity": "20"}, format="json")
        self.assertEqual(response.status_code, 500)
        self.assertFalse(Reservation.objects.exists())
        pk = self.claim()
        self.client.force_authenticate(self.owner)
        with patch("shared.models.AuditEvent.objects.create", side_effect=RuntimeError("audit unavailable")):
            response = self.client.post(f"/api/v1/reservations/{pk}/accept/", {"expected_version": 1}, format="json")
        self.assertEqual(response.status_code, 500)
        reservation = Reservation.objects.get(pk=pk)
        self.assertEqual(reservation.status, "PENDING")
        self.assertIsNone(reservation.accepted_snapshot)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "AVAILABLE")

    def test_audit_redacts_nested_credentials_and_records_actor_time(self):
        from django.db import transaction
        from shared.models import record_audit
        with transaction.atomic():
            event = record_audit(actor=self.owner, entity_type="Reservation", entity_id=99, action="cancel",
                summary={"reason": "Permintaan dibatalkan", "before": {"status": "PENDING", "password": "hidden"},
                         "after": {"status": "CANCELLED"}, "nested": [{"access_token": "hidden", "quantity": "2"}],
                         "whatsapp_number": self.owner.whatsapp_number})
        self.assertEqual(event.actor, self.owner)
        self.assertIsNotNone(event.created_at)
        self.assertEqual(event.summary["before"], {"status": "PENDING"})
        self.assertEqual(event.summary["nested"], [{"quantity": "2"}])
        self.assertNotIn("hidden", str(event.summary))
        self.assertNotIn(self.owner.whatsapp_number, str(event.summary))

    def test_rate_limits_are_scoped_and_return_retry_after(self):
        from django.test import override_settings
        from common.authentication import SharedRateThrottle
        from shared.models import ThrottleBucket
        rates = {key: "2/min" for key in SharedRateThrottle.default_rates}
        with override_settings(CONTRACT_THROTTLE_RATES=rates):
            for _ in range(2):
                self.assertEqual(self.client.get("/api/v1/locations/search/?q=Depok").status_code, 501)
            response = self.client.get("/api/v1/locations/search/?q=Depok")
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.data["errors"][0]["code"], "throttled")
            self.assertGreater(int(response["Retry-After"]), 0)
            self.claim(); self.claim()
            response = self.client.post(f"/api/v1/listings/{self.source.pk}/claims/", {"quantity": "2"}, format="json")
            self.assertEqual(response.status_code, 429)
            self.client.force_authenticate(self.outsider)
            self.assertEqual(self.client.get("/api/v1/locations/search/?q=Depok").status_code, 501)
            self.client.force_authenticate(self.admin)
            for _ in range(2):
                response = self.client.patch("/api/v1/admin/reservations/999999/", {"reason": "Review"}, format="json")
                self.assertEqual(response.status_code, 404)
            self.assertEqual(self.client.patch("/api/v1/admin/reservations/999999/", {}, format="json").status_code, 429)
        self.assertNotIn("127.0.0.1", str(list(ThrottleBucket.objects.values_list("key", flat=True))))

    def test_stored_resource_extras_never_leak_through_terms(self):
        from reservations.models import Reservation
        from reservations.serializers import ReservationSerializer
        reservation = Reservation.objects.get(pk=self.claim())
        from datetime import timedelta
        from django.utils import timezone
        from requests_app.models import ResourceRequest
        source = ResourceRequest.objects.create(owner=self.receiver, created_by=self.receiver,
            resource_type="PACKAGED_FOOD_SURPLUS", category="prepared_meal", intended_use="HUMAN_CONSUMPTION",
            quantity_needed="20", unit="box", location=self.source.location, donate_only=True,
            valid_until=timezone.now()+timedelta(days=1))
        reservation.source_type = "REQUEST_OFFER"
        reservation.listing = None
        reservation.request = source
        reservation.resource_data = {"title": "Resource", "whatsapp_number": self.owner.whatsapp_number,
                                     "password": "hidden", "extra": "hidden"}
        data = ReservationSerializer(reservation, context={"viewer": self.receiver}).data
        self.assertNotIn("hidden", str(data))
        self.assertNotIn(self.owner.whatsapp_number, str(data))

    def test_reservation_audit_records_lifecycle_versions_and_reason(self):
        from shared.models import AuditEvent
        pk = self.claim()
        self.client.force_authenticate(self.owner)
        response = self.client.post(f"/api/v1/reservations/{pk}/accept/", {"expected_version": 1}, format="json")
        self.assertEqual(response.status_code, 200)
        self.client.force_authenticate(self.receiver)
        response = self.client.post(f"/api/v1/reservations/{pk}/cancel/", {"reason": "Tidak jadi"}, format="json")
        self.assertEqual(response.status_code, 200)
        events = list(AuditEvent.objects.filter(entity_type="Reservation", entity_id=str(pk)).order_by("pk"))
        self.assertEqual([event.action for event in events], ["create", "accept", "cancel"])
        self.assertEqual(events[1].summary["before"]["status"], "PENDING")
        self.assertEqual(events[1].summary["after"]["status"], "ACCEPTED")
        self.assertEqual(events[2].summary["before"]["version"], 2)
        self.assertEqual(events[2].summary["after"]["version"], 3)
        self.assertEqual(events[2].summary["reason"], "Tidak jadi")

    def test_throttle_window_expires_without_a_worker(self):
        import time
        from unittest.mock import patch
        from django.test import override_settings
        with override_settings(CONTRACT_THROTTLE_RATES={"location": "1/min"}):
            self.assertEqual(self.client.get("/api/v1/locations/search/?q=Depok").status_code, 501)
            self.assertEqual(self.client.get("/api/v1/locations/search/?q=Depok").status_code, 429)
            with patch("common.authentication.SharedRateThrottle.timer", return_value=time.time()+61):
                self.assertEqual(self.client.get("/api/v1/locations/search/?q=Depok").status_code, 501)


from django.test import TransactionTestCase


class SharedThrottleConcurrencyTests(TransactionTestCase):
    def test_parallel_workers_share_a_single_limit(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connection, close_old_connections
        from django.test import override_settings
        from common.authentication import ContractThrottle
        from shared.models import ThrottleBucket
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row-lock test")
        barrier = Barrier(3)

        def attempt(_):
            close_old_connections()
            try:
                request = SimpleNamespace(path="/api/v1/locations/search/", method="GET",
                                          user=SimpleNamespace(is_authenticated=True, pk=777))
                throttle = ContractThrottle()
                barrier.wait(timeout=15)
                return throttle.allow_request(request, None)
            finally:
                close_old_connections()

        with override_settings(CONTRACT_THROTTLE_RATES={"location": "2/min"}):
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(attempt, i) for i in range(3)]
                results = [future.result(timeout=30) for future in futures]
        self.assertEqual(results.count(True), 2)
        self.assertEqual(results.count(False), 1)
        self.assertEqual(len(ThrottleBucket.objects.get().history), 2)


class ExpiryContractTests(TestCase):
    def test_released_request_allocation_is_open_before_worker_without_read_writes(self):
        from datetime import timedelta
        self.source.status = "ALLOCATED"
        self.source.save()
        expired = self.offer(quantity="20", deadline=self.now-timedelta(seconds=1))
        before = (self.source.version, expired.version)
        detail = APIClient().get(f"/api/v1/requests/{self.source.pk}/")
        self.assertEqual(detail.status_code, 200, detail.data)
        self.assertEqual(detail.data["data"]["status"], "OPEN")
        rows = self.client.get("/api/v1/requests/?scope=mine&status=OPEN").data["data"]["results"]
        self.assertEqual([row["id"] for row in rows], [self.source.pk])
        self.source.refresh_from_db(); expired.refresh_from_db()
        self.assertEqual((self.source.status, expired.status), ("ALLOCATED", "ACCEPTED"))
        self.assertEqual((self.source.version, expired.version), before)

    def test_acceptance_reuses_expired_allocation_before_worker(self):
        from datetime import timedelta
        self.source.status = "ALLOCATED"
        self.source.save()
        self.offer(quantity="20", deadline=self.now-timedelta(seconds=1))
        pending = self.offer(quantity="4", status="PENDING")
        response = self.client.post(f"/api/v1/reservations/{pending.pk}/accept/",
                                   {"expected_version": 1}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, "OPEN")

    def test_new_offer_reuses_expired_allocation_before_worker(self):
        from datetime import timedelta
        self.source.status = "ALLOCATED"
        self.source.save()
        self.offer(quantity="20", deadline=self.now-timedelta(seconds=1))
        self.client.force_authenticate(self.provider)
        response = self.client.post(f"/api/v1/requests/{self.source.pk}/offers/", {
            "resource_type": self.source.resource_type, "category": self.source.category,
            "quantity": "4", "unit": "box", "mode": "DONATE", "unit_price": "0.00",
            "title": "Makanan", "description": "Makanan", "condition": "Baik",
            "suitability": [self.source.intended_use], "location": self.source.location,
            "resource_deadline": (self.now+timedelta(hours=6)).isoformat(),
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)

    def setUp(self):
        from datetime import timedelta
        from django.contrib.auth import get_user_model
        from django.utils import timezone
        from requests_app.models import ResourceRequest
        self.now = timezone.now()
        User = get_user_model()
        self.provider = User.objects.create_user(email="expiry-provider@example.test", display_name="Provider")
        self.receiver = User.objects.create_user(email="expiry-receiver@example.test", display_name="Receiver")
        self.source = ResourceRequest.objects.create(owner=self.receiver, created_by=self.receiver,
            resource_type="PACKAGED_FOOD_SURPLUS", category="prepared_meal", intended_use="HUMAN_CONSUMPTION",
            quantity_needed="20", unit="box", location={"label": "Kampus", "area": "Depok", "latitude": -6.3, "longitude": 106.8},
            donate_only=True, valid_until=self.now+timedelta(days=1))
        self.client = APIClient()
        self.client.force_authenticate(self.receiver)

    def offer(self, quantity="4", status="ACCEPTED", deadline=None, pickup=None):
        from datetime import timedelta
        from reservations.models import Reservation
        from pickups.models import Pickup
        row = Reservation.objects.create(source_type="REQUEST_OFFER", request=self.source,
            provider=self.provider, receiver=self.receiver, submitted_by=self.provider,
            quantity=quantity, unit="box", mode="DONATE", unit_price="0", status=status,
            resource_deadline=deadline or self.now+timedelta(hours=6),
            resource_data={"resource_type":self.source.resource_type,"category":self.source.category,
                           "suitability":[self.source.intended_use],"location":self.source.location,
                           "title":"Makanan","description":"Makanan","condition":"Baik"},
            accepted_snapshot={"quantity": quantity, "location": self.source.location} if status=="ACCEPTED" else None)
        if pickup:
            Pickup.objects.create(reservation=row, scheduled_at=self.now+timedelta(hours=1),
                                  location=self.source.location, status=pickup)
        return row

    def test_expired_offer_does_not_hold_effective_request_quantity(self):
        from datetime import timedelta
        row = self.offer(deadline=self.now-timedelta(seconds=1))
        before = (row.status, row.version)
        data = self.client.get(f"/api/v1/requests/{self.source.pk}/").data["data"]
        self.assertEqual(data["quantity_allocated"], "0.000")
        self.assertEqual(data["quantity_remaining"], "20.000")
        row.refresh_from_db()
        self.assertEqual((row.status,row.version), before)

    def test_request_deadline_is_effective_before_worker_without_a_read_write(self):
        from datetime import timedelta
        self.source.valid_until = self.now-timedelta(seconds=1)
        self.source.save()
        row = self.offer()
        data = self.client.get(f"/api/v1/requests/{self.source.pk}/").data["data"]
        self.assertEqual(data["status"], "EXPIRED")
        self.assertEqual(data["quantity_allocated"], "4.000")
        history = self.client.get("/api/v1/requests/?scope=mine&status=EXPIRED").data["data"]["results"]
        self.assertEqual([item["id"] for item in history], [self.source.pk])
        self.source.refresh_from_db(); row.refresh_from_db()
        self.assertEqual(self.source.status, "OPEN")
        self.assertEqual(row.status, "ACCEPTED")

    def test_expiry_command_exists_and_cancels_only_pending_at_request_deadline(self):
        from datetime import timedelta
        from django.core.management import call_command
        from io import StringIO
        self.source.valid_until = self.now-timedelta(seconds=1)
        self.source.save()
        pending = self.offer(status="PENDING")
        accepted = self.offer()
        call_command("expire_resources", stdout=StringIO())
        self.source.refresh_from_db(); pending.refresh_from_db(); accepted.refresh_from_db()
        self.assertEqual(self.source.status, "EXPIRED")
        self.assertEqual(pending.status, "CANCELLED")
        self.assertEqual(accepted.status, "ACCEPTED")

    def listing(self, **changes):
        from datetime import timedelta
        from listings.models import ResourceListing
        values = dict(owner=self.provider, created_by=self.provider,
            resource_type="PACKAGED_FOOD_SURPLUS", title="Surplus", category="prepared_meal", description="Makanan",
            quantity_total="20", unit="box", mode="DONATE", unit_price="0", condition="Baik",
            suitability=["HUMAN_CONSUMPTION"], location=self.source.location,
            available_until=self.now+timedelta(hours=6))
        values.update(changes)
        return ResourceListing.objects.create(**values)

    def claim(self, source, quantity="4", status="ACCEPTED", deadline=None, pickup=None):
        from reservations.models import Reservation
        from pickups.models import Pickup
        row = Reservation.objects.create(source_type="LISTING_CLAIM", listing=source,
            provider=self.provider, receiver=self.receiver, submitted_by=self.receiver,
            quantity=quantity, unit="box", mode="DONATE", unit_price="0", status=status,
            resource_deadline=deadline or source.available_until,
            accepted_snapshot={"quantity": quantity, "location": source.location} if status=="ACCEPTED" else None)
        if pickup:
            Pickup.objects.create(reservation=row, scheduled_at=self.now, location=source.location, status=pickup)
        return row

    def test_listing_expiry_cancels_unfinished_and_preserves_completed_snapshots(self):
        from datetime import timedelta
        from copy import deepcopy
        from shared.services import expire_source
        source = self.listing(available_until=self.now-timedelta(seconds=1))
        pending = self.claim(source, status="PENDING")
        active = self.claim(source, pickup="SCHEDULED")
        completed = self.claim(source, quantity="3", pickup="COMPLETED")
        rejected = self.claim(source, status="REJECTED")
        snapshots = {row.pk: deepcopy(row.accepted_snapshot) for row in (active, completed)}
        result = expire_source("listing", source.pk, now=self.now)
        self.assertEqual(result["reservations_cancelled"], 2)
        self.assertEqual(result["pickups_cancelled"], 1)
        source.refresh_from_db(); pending.refresh_from_db(); active.refresh_from_db(); completed.refresh_from_db(); rejected.refresh_from_db()
        self.assertEqual(source.status, "EXPIRED")
        self.assertEqual((pending.status,active.status,completed.status,rejected.status), ("CANCELLED","CANCELLED","ACCEPTED","REJECTED"))
        self.assertEqual(active.cancellation_code, "RESOURCE_EXPIRED")
        self.assertEqual(active.pickup.status, "CANCELLED")
        self.assertIsNotNone(active.pickup.cancelled_at)
        self.assertEqual(completed.pickup.status, "COMPLETED")
        for row in (active,completed):
            self.assertEqual(row.accepted_snapshot, snapshots[row.pk])
        data = self.client.get(f"/api/v1/listings/{source.pk}/").data["data"]
        self.assertEqual((data["quantity_available"],data["quantity_allocated"],data["quantity_completed"]), ("17.000","0.000","3.000"))

    def test_completed_and_fulfilled_sources_never_become_expired(self):
        from datetime import timedelta
        from shared.services import run_expiry
        listing = self.listing(available_until=self.now-timedelta(seconds=1))
        claim = self.claim(listing, quantity="20", pickup="COMPLETED")
        self.source.valid_until = self.now-timedelta(seconds=1)
        self.source.save()
        offer = self.offer(quantity="20", deadline=self.now-timedelta(seconds=1), pickup="COMPLETED")
        run_expiry(now=self.now)
        listing.refresh_from_db(); self.source.refresh_from_db(); claim.refresh_from_db(); offer.refresh_from_db()
        self.assertEqual(listing.status, "COMPLETED")
        self.assertEqual(self.source.status, "FULFILLED")
        self.assertEqual((claim.status,offer.status), ("ACCEPTED","ACCEPTED"))

    def test_expiry_releases_last_offer_but_does_not_reopen_terminal_request(self):
        from datetime import timedelta
        from shared.services import expire_source
        for status in ("CLOSED","EXPIRED"):
            self.source.status = status; self.source.save()
            row = self.offer(quantity="20", deadline=self.now-timedelta(seconds=1), pickup="SCHEDULED")
            expire_source("request", self.source.pk, now=self.now)
            self.source.refresh_from_db(); row.refresh_from_db()
            self.assertEqual(self.source.status, status)
            self.assertEqual(row.status, "CANCELLED")
            self.assertEqual(row.pickup.status, "CANCELLED")

    def test_worker_repetition_does_not_bump_versions_or_duplicate_audit(self):
        from datetime import timedelta
        from shared.models import AuditEvent
        from shared.services import run_expiry
        row = self.offer(quantity="20", deadline=self.now-timedelta(seconds=1), pickup="SCHEDULED")
        self.source.status = "ALLOCATED"; self.source.save()
        run_expiry(now=self.now)
        self.source.refresh_from_db(); row.refresh_from_db()
        first = (self.source.version,row.version,row.pickup.version,AuditEvent.objects.count())
        result = run_expiry(now=self.now)
        self.source.refresh_from_db(); row.refresh_from_db()
        self.assertEqual((self.source.version,row.version,row.pickup.version,AuditEvent.objects.count()), first)
        self.assertEqual((result["sources_changed"],result["reservations_cancelled"],result["pickups_cancelled"]), (0,0,0))
        self.assertEqual(self.source.status, "OPEN")

    def test_audit_failure_rolls_back_all_changes_for_the_source(self):
        from datetime import timedelta
        from unittest.mock import patch
        from reservations.models import Reservation
        from pickups.models import Pickup
        from shared.models import AuditEvent
        from shared.services import expire_source
        row = self.offer(deadline=self.now-timedelta(seconds=1), pickup="SCHEDULED")
        before = (list(Reservation.objects.values()),list(Pickup.objects.values()))
        from shared.models import record_audit
        count = 0
        def audit(**kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("audit unavailable")
            return record_audit(**kwargs)
        with patch("shared.services.record_audit", side_effect=audit), self.assertRaises(RuntimeError):
            expire_source("request", self.source.pk, now=self.now)
        self.assertEqual((list(Reservation.objects.values()),list(Pickup.objects.values())), before)
        self.assertFalse(AuditEvent.objects.exists())

    def test_effective_serialization_matches_worker_and_has_no_read_side_effects(self):
        from datetime import timedelta
        from shared.models import AuditEvent
        from shared.services import expire_source
        row = self.offer(deadline=self.now-timedelta(seconds=1), pickup="SCHEDULED")
        before = (row.status,row.version,row.accepted_snapshot,AuditEvent.objects.count())
        first = self.client.get(f"/api/v1/reservations/{row.pk}/").data["data"]
        self.assertEqual(first["status"], "CANCELLED")
        self.assertEqual(first["pickup"]["status"], "CANCELLED")
        self.assertIsNone(first["counterparty_contact"])
        row.refresh_from_db()
        self.assertEqual((row.status,row.version,row.accepted_snapshot,AuditEvent.objects.count()), before)
        expire_source("request", self.source.pk, now=self.now)
        after = self.client.get(f"/api/v1/reservations/{row.pk}/").data["data"]
        self.assertEqual((after["status"],after["pickup"]["status"]), (first["status"],first["pickup"]["status"]))

    def test_live_accepted_offer_can_schedule_after_request_validity_expires(self):
        from datetime import timedelta
        from shared.services import expire_source
        for user in (self.provider,self.receiver):
            user.whatsapp_number = "+628120000001"; user.save()
        self.source.valid_until = self.now-timedelta(seconds=1); self.source.save()
        pending = self.offer(status="PENDING")
        row = self.offer()
        expire_source("request",self.source.pk,now=self.now)
        self.client.force_authenticate(self.provider)
        response = self.client.post(f"/api/v1/reservations/{row.pk}/pickup/",
            {"scheduled_at": (self.now+timedelta(hours=1)).isoformat()}, format="json")
        self.assertEqual(response.status_code,201,response.data)
        row.refresh_from_db(); pending.refresh_from_db()
        self.assertEqual(row.status,"ACCEPTED")
        self.assertEqual(pending.status,"CANCELLED")

    def test_acceptance_uses_live_allocation_and_completed_quantities(self):
        from datetime import timedelta
        self.offer(quantity="15", deadline=self.now-timedelta(seconds=1))
        self.offer(quantity="3", pickup="COMPLETED")
        self.offer(quantity="2", pickup="SCHEDULED")
        pending = self.offer(quantity="15", status="PENDING")
        self.client.force_authenticate(self.receiver)
        response = self.client.post(f"/api/v1/reservations/{pending.pk}/accept/", {"expected_version": 1}, format="json")
        self.assertEqual(response.status_code,200,response.data)
        self.source.refresh_from_db()
        self.assertEqual(self.source.status,"ALLOCATED")
        data = self.client.get(f"/api/v1/requests/{self.source.pk}/").data["data"]
        self.assertEqual((data["quantity_allocated"],data["quantity_fulfilled"],data["quantity_remaining"]), ("17.000","3.000","0.000"))

    def test_resource_deadline_boundary_and_utc_offset_are_equivalent(self):
        from datetime import timedelta, timezone as tz
        from shared.services import expire_source
        row = self.offer(deadline=self.now)
        result = expire_source("request", self.source.pk, now=self.now.astimezone(tz(timedelta(hours=7))))
        self.assertEqual(result["reservations_cancelled"], 1)
        row.refresh_from_db()
        self.assertEqual(row.status,"CANCELLED")

    def test_command_runs_once_by_default_and_reports_json_counts(self):
        import json
        from io import StringIO
        from unittest.mock import patch
        from django.core.management import call_command
        output = StringIO()
        with patch("shared.management.commands.expire_resources.time.sleep") as sleep:
            call_command("expire_resources", stdout=output)
        sleep.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertIn("checked_at",result)
        self.assertIn("duration_ms",result)
        self.assertEqual(result["sources_scanned"],1)
        self.assertEqual(result["reservations_cancelled"],0)

    def test_watch_cadence_is_explicit_configurable_and_stoppable(self):
        from io import StringIO
        from unittest.mock import patch
        from django.test import override_settings
        from django.core.management import call_command, CommandError
        with override_settings(EXPIRY_INTERVAL_SECONDS=7):
            with patch("shared.management.commands.expire_resources.time.sleep", side_effect=KeyboardInterrupt) as sleep:
                call_command("expire_resources", watch=True, stdout=StringIO(), stderr=StringIO())
            sleep.assert_called_once_with(7)
        for options in ({"interval":0},{"interval":float("nan")},{"batch_size":0}):
            with self.subTest(options=options), self.assertRaises(CommandError):
                call_command("expire_resources",stdout=StringIO(),**options)

    def test_command_failures_are_observable_and_leave_source_transaction_rolled_back(self):
        from datetime import timedelta
        from io import StringIO
        from unittest.mock import patch
        from django.core.management import call_command, CommandError
        row = self.offer(deadline=self.now-timedelta(seconds=1))
        with patch("shared.services.record_audit",side_effect=RuntimeError("failure")), self.assertRaises(CommandError):
            call_command("expire_resources", stdout=StringIO())
        row.refresh_from_db()
        self.assertEqual(row.status,"ACCEPTED")

    def test_status_filters_match_effective_expiry_before_worker(self):
        from datetime import timedelta
        row = self.offer(deadline=self.now-timedelta(seconds=1), pickup="SCHEDULED")
        for url in ("/api/v1/reservations/", "/api/v1/pickups/"):
            cancelled = self.client.get(url+"?status=CANCELLED").data["data"]["results"]
            self.assertEqual(len(cancelled),1)
            self.assertEqual(cancelled[0]["status"],"CANCELLED")
            status = "ACCEPTED" if "reservations" in url else "SCHEDULED"
            self.assertEqual(self.client.get(url+"?status="+status).data["data"]["results"],[])
        row.refresh_from_db()
        self.assertEqual(row.status,"ACCEPTED")
        self.assertEqual(row.pickup.status,"SCHEDULED")

    def test_expired_pending_offer_cannot_be_revised_or_rejected(self):
        from datetime import timedelta
        row=self.offer(status="PENDING",deadline=self.now-timedelta(seconds=1))
        self.client.force_authenticate(self.provider)
        response=self.client.patch(f"/api/v1/reservations/{row.pk}/",{"quantity":"2","expected_version":1},format="json")
        self.assertEqual(response.status_code,409,response.data)
        self.client.force_authenticate(self.receiver)
        response=self.client.post(f"/api/v1/reservations/{row.pk}/reject/",{"reason":"Terlambat","expected_version":1},format="json")
        self.assertEqual(response.status_code,409,response.data)
        row.refresh_from_db()
        self.assertEqual((row.status,row.version),("PENDING",1))


class ExpiryConcurrencyTests(TransactionTestCase):
    setUp = ExpiryContractTests.setUp
    offer = ExpiryContractTests.offer
    listing = ExpiryContractTests.listing
    claim = ExpiryContractTests.claim

    def race(self, first, second):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        from django.db import close_old_connections, connection, transaction
        from listings.models import ResourceListing
        if connection.vendor != "postgresql":
            self.skipTest("PostgreSQL row-lock test")
        locked = Event(); started = Event()
        def winning_operation():
            close_old_connections()
            try:
                with transaction.atomic():
                    type(self.listing_row).objects.select_for_update().get(pk=self.listing_row.pk)
                    locked.set()
                    if not started.wait(timeout=15):
                        raise AssertionError("Concurrent worker did not start")
                    return first()
            finally:
                close_old_connections()
        def waiting_operation():
            close_old_connections()
            try:
                if not locked.wait(timeout=15):
                    raise AssertionError("Source lock was not acquired")
                started.set()
                return second()
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(winning_operation); b=pool.submit(waiting_operation)
            return a.result(timeout=30), b.result(timeout=30)

    def api(self, user, path, data):
        client=APIClient(); client.force_authenticate(user)
        return client.post(path,data,format="json").status_code

    def test_accept_wins_then_worker_expires_once(self):
        from datetime import timedelta
        from shared.services import expire_source
        from shared.models import AuditEvent
        self.listing_row=self.listing()
        row=self.claim(self.listing_row,status="PENDING")
        accepted,expired=self.race(
            lambda:self.api(self.provider,f"/api/v1/reservations/{row.pk}/accept/",{"expected_version":1}),
            lambda:expire_source("listing",self.listing_row.pk,now=self.now+timedelta(days=1)))
        self.assertEqual(accepted,200)
        row.refresh_from_db(); self.listing_row.refresh_from_db()
        self.assertEqual((row.status,row.version), ("CANCELLED",3))
        self.assertIsNotNone(row.accepted_snapshot)
        self.assertEqual(self.listing_row.status,"EXPIRED")
        self.assertEqual(AuditEvent.objects.filter(entity_type="Reservation",entity_id=str(row.pk),action="expire").count(),1)

    def test_worker_wins_then_accept_has_no_partial_effects(self):
        from datetime import timedelta
        from shared.services import expire_source
        self.listing_row=self.listing()
        row=self.claim(self.listing_row,status="PENDING")
        expired,accepted=self.race(
            lambda:expire_source("listing",self.listing_row.pk,now=self.now+timedelta(days=1)),
            lambda:self.api(self.provider,f"/api/v1/reservations/{row.pk}/accept/",{"expected_version":1}))
        self.assertEqual(accepted,409)
        row.refresh_from_db()
        self.assertEqual((row.status,row.version), ("CANCELLED",2))
        self.assertIsNone(row.accepted_snapshot)

    def test_complete_wins_and_worker_preserves_completed_history(self):
        from datetime import timedelta
        from shared.services import expire_source
        self.listing_row=self.listing(status="RESERVED")
        row=self.claim(self.listing_row,quantity="20",pickup="SCHEDULED")
        pickup=row.pickup
        completed,expired=self.race(
            lambda:self.api(self.receiver,f"/api/v1/pickups/{pickup.pk}/complete/",{}),
            lambda:expire_source("listing",self.listing_row.pk,now=self.now+timedelta(days=1)))
        self.assertEqual(completed,200)
        row.refresh_from_db(); pickup.refresh_from_db(); self.listing_row.refresh_from_db()
        self.assertEqual((row.status,pickup.status,self.listing_row.status),("ACCEPTED","COMPLETED","COMPLETED"))
        self.assertEqual(expired["reservations_cancelled"],0)

    def test_worker_wins_and_completion_cannot_revive_pickup(self):
        from datetime import timedelta
        from shared.services import expire_source
        self.listing_row=self.listing(status="RESERVED")
        row=self.claim(self.listing_row,quantity="20",pickup="SCHEDULED")
        pickup=row.pickup
        expired,completed=self.race(
            lambda:expire_source("listing",self.listing_row.pk,now=self.now+timedelta(days=1)),
            lambda:self.api(self.receiver,f"/api/v1/pickups/{pickup.pk}/complete/",{}))
        self.assertEqual(completed,409)
        row.refresh_from_db(); pickup.refresh_from_db(); self.listing_row.refresh_from_db()
        self.assertEqual((row.status,pickup.status,self.listing_row.status),("CANCELLED","CANCELLED","EXPIRED"))

    def test_cancel_and_expiry_release_allocation_once(self):
        from datetime import timedelta
        from shared.services import expire_source, source_quantities
        from shared.models import AuditEvent
        self.listing_row=self.listing(status="RESERVED")
        row=self.claim(self.listing_row,quantity="20",pickup="SCHEDULED")
        cancelled,expired=self.race(
            lambda:self.api(self.receiver,f"/api/v1/reservations/{row.pk}/cancel/",{"reason":"Tidak jadi"}),
            lambda:expire_source("listing",self.listing_row.pk,now=self.now+timedelta(days=1)))
        self.assertEqual(cancelled,200)
        row.refresh_from_db(); self.listing_row.refresh_from_db()
        self.assertEqual((row.status,row.version),("CANCELLED",2))
        self.assertEqual(self.listing_row.status,"EXPIRED")
        self.assertEqual(source_quantities(self.listing_row)["allocated"],0)
        self.assertEqual(AuditEvent.objects.filter(entity_type="Reservation",entity_id=str(row.pk)).count(),1)

    def test_two_expiry_workers_do_not_duplicate_mutations(self):
        from datetime import timedelta
        from shared.services import expire_source
        from shared.models import AuditEvent
        self.listing_row=self.listing()
        row=self.claim(self.listing_row,pickup="SCHEDULED")
        first,second=self.race(
            lambda:expire_source("listing",self.listing_row.pk,now=self.now+timedelta(days=1)),
            lambda:expire_source("listing",self.listing_row.pk,now=self.now+timedelta(days=1)))
        self.assertEqual(first["reservations_cancelled"]+second["reservations_cancelled"],1)
        row.refresh_from_db()
        self.assertEqual(row.version,2)
        self.assertEqual(AuditEvent.objects.filter(entity_type="Reservation",entity_id=str(row.pk),action="expire").count(),1)

    def test_request_accept_wins_before_validity_expiry_and_agreement_continues(self):
        from datetime import timedelta
        from shared.services import expire_source
        self.source.valid_until=self.now+timedelta(hours=1); self.source.save()
        self.listing_row=self.source
        row=self.offer(status="PENDING")
        accepted,expired=self.race(
            lambda:self.api(self.receiver,f"/api/v1/reservations/{row.pk}/accept/",{"expected_version":1}),
            lambda:expire_source("request",self.source.pk,now=self.now+timedelta(hours=2)))
        self.assertEqual(accepted,200)
        row.refresh_from_db(); self.source.refresh_from_db()
        self.assertEqual((row.status,row.version,self.source.status),("ACCEPTED",2,"EXPIRED"))
        self.assertIsNotNone(row.accepted_snapshot)
        self.assertEqual(expired["reservations_cancelled"],0)

    def test_request_expiry_wins_then_pending_offer_cannot_be_accepted(self):
        from datetime import timedelta
        from shared.services import expire_source
        self.source.valid_until=self.now+timedelta(hours=1); self.source.save()
        self.listing_row=self.source
        row=self.offer(status="PENDING")
        expired,accepted=self.race(
            lambda:expire_source("request",self.source.pk,now=self.now+timedelta(hours=2)),
            lambda:self.api(self.receiver,f"/api/v1/reservations/{row.pk}/accept/",{"expected_version":1}))
        self.assertEqual(accepted,409)
        row.refresh_from_db(); self.source.refresh_from_db()
        self.assertEqual((row.status,row.version,self.source.status),("CANCELLED",2,"EXPIRED"))
        self.assertEqual(row.cancellation_code,"REQUEST_EXPIRED")
        self.assertIsNone(row.accepted_snapshot)

    def test_worker_wins_then_cancel_does_not_release_twice(self):
        from datetime import timedelta
        from shared.services import expire_source
        from shared.models import AuditEvent
        self.listing_row=self.listing(status="RESERVED")
        row=self.claim(self.listing_row,quantity="20",pickup="SCHEDULED")
        expired,cancelled=self.race(
            lambda:expire_source("listing",self.listing_row.pk,now=self.now+timedelta(days=1)),
            lambda:self.api(self.receiver,f"/api/v1/reservations/{row.pk}/cancel/",{"reason":"Tidak jadi"}))
        self.assertEqual(cancelled,409)
        row.refresh_from_db()
        self.assertEqual((row.status,row.version),("CANCELLED",2))
        self.assertEqual(AuditEvent.objects.filter(entity_type="Reservation",entity_id=str(row.pk)).count(),1)
