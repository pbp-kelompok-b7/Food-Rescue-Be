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
        reservation.source_type = "REQUEST_OFFER"
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
