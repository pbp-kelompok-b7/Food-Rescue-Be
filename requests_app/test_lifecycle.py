from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone
from rest_framework.test import APIClient

from pickups.models import Pickup, PickupStatus
from reservations.models import Reservation, ReservationStatus
from shared.models import AuditEvent
from .models import ResourceRequest, RequestStatus
from . import tests as read_tests


class RequestLifecycleTests(TestCase):
    make_request = read_tests.RequestReadTests.make_request
    offer = read_tests.RequestReadTests.offer

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
            with patch("requests_app.lifecycle._audit", side_effect=RuntimeError("Audit unavailable")):
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
    make_request = read_tests.RequestReadTests.make_request
    offer = read_tests.RequestReadTests.offer

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
