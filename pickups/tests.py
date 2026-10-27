from datetime import timedelta
import threading

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections, transaction
from django.middleware.csrf import _get_new_csrf_string, _mask_cipher_secret
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient

from listings.models import ResourceListing
from reservations.models import Reservation, ReservationStatus, SourceType
from pickups.models import Pickup
from shared.enums import ResourceType, Mode, Unit

User = get_user_model()

RESPONSE_FIELDS = {
    "id", "created_at", "updated_at", "version", "reservation_id", "scheduled_at",
    "location", "instructions", "status", "completed_at", "no_show_at", "cancelled_at",
}

SNAPSHOT_LOCATION = {
    "label": "Kampus UI Depok", "area": "Depok",
    "latitude": -6.36, "longitude": 106.82,
}
OTHER_LOCATION = {
    "label": "Beji, Depok", "area": "Depok",
    "latitude": -6.37, "longitude": 106.83,
}


def make_user(email, *, whatsapp="+6281200000000", is_admin=False, **extra):
    """Create the project's custom User (email login; no username field)."""
    user = User.objects.create_user(
        email=f"{email}@example.com",
        password="pass12345",
        display_name=email.title(),
        is_staff=is_admin,
        **extra,
    )
    user.whatsapp_number = whatsapp
    user.save(update_fields=["whatsapp_number", "is_staff", "updated_at"])
    return user


def make_reservation(provider, receiver, *, status="ACCEPTED", deadline=None,
                     location=None, **extra):
    """Create a valid reservation backed by a ResourceListing."""
    deadline = deadline or (timezone.now() + timedelta(days=2)).replace(microsecond=0)
    location = location or SNAPSHOT_LOCATION

    listing = ResourceListing.objects.create(
        owner=provider,
        created_by=provider,
        resource_type=ResourceType.PACKAGED_FOOD_SURPLUS,
        title="Rice box",
        category="Prepared food",
        description="Test listing for pickup scheduling.",
        quantity_total="5.000",
        unit=Unit.BOX,
        mode=Mode.DONATE,
        unit_price="0.00",
        condition="Freshly prepared",
        suitability=["HUMAN_CONSUMPTION"],
        location=location,
        available_until=deadline,
    )

    snapshot = {
        "resource_type": ResourceType.PACKAGED_FOOD_SURPLUS,
        "title": listing.title,
        "quantity": "5.000",
        "unit": Unit.BOX,
        "mode": Mode.DONATE,
        "unit_price": "0.00",
        "total_price": "0.00",
        "location": location,
        "resource_deadline": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    return Reservation.objects.create(
        source_type=SourceType.LISTING_CLAIM,
        listing=listing,
        request=None,
        provider=provider,
        receiver=receiver,
        submitted_by=receiver,
        status=status,
        quantity="5.000",
        unit=Unit.BOX,
        mode=Mode.DONATE,
        unit_price="0.00",
        resource_data={},
        resource_deadline=deadline,
        accepted_snapshot=snapshot if status == ReservationStatus.ACCEPTED else None,
        accepted_at=timezone.now() if status == ReservationStatus.ACCEPTED else None,
        **extra,
    )


def change_source_location(reservation, new_location):
    """Change the live listing; accepted_snapshot should remain unchanged."""
    source = reservation.listing
    source.location = new_location
    source.save()


def make_pickup(reservation, status="SCHEDULED"):
    return Pickup.objects.create(
        reservation=reservation,
        scheduled_at=timezone.now() + timedelta(days=1),
        location=reservation.accepted_snapshot["location"],
        status=status,
    )


# ---------------------------------------------------------------- base class
class PickupCreateBase(TestCase):
    def setUp(self):
        self.provider = make_user("provider")
        self.receiver = make_user("receiver")
        self.stranger = make_user("stranger")
        self.admin = make_user("admin", is_admin=True)

        self.deadline = (timezone.now() + timedelta(days=2)).replace(microsecond=0)
        self.reservation = make_reservation(self.provider, self.receiver, deadline=self.deadline)

        self.scheduled = (timezone.now() + timedelta(days=1)).replace(microsecond=0)
        self.client = APIClient()
        self.client.force_login(self.provider)

    # helpers
    def url(self, reservation_id=None):
        pk = self.reservation.pk if reservation_id is None else reservation_id
        return f"/api/v1/reservations/{pk}/pickup/"

    def payload(self, **overrides):
        body = {
            "scheduled_at": self.scheduled.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "instructions": "Temui petugas di pintu utama",
        }
        body.update(overrides)
        return {k: v for k, v in body.items() if v is not ...}

    def post(self, body=None, client=None, reservation_id=None):
        return (client or self.client).post(
            self.url(reservation_id), self.payload() if body is None else body, format="json"
        )

    def assertError(self, resp, http_status, code=None, field=None):
        self.assertEqual(resp.status_code, http_status, resp.content)
        body = resp.json()
        self.assertIs(body["success"], False)
        self.assertIsInstance(body["message"], str)
        self.assertTrue(body["errors"], "errors must be a non-empty list")
        for err in body["errors"]:
            self.assertEqual(set(err), {"field", "code", "message"})
        if code:
            self.assertIn(code, [e["code"] for e in body["errors"]])
        if field:
            self.assertIn(field, [e["field"] for e in body["errors"]])

    def assertNoPickup(self):
        self.assertEqual(Pickup.objects.count(), 0, "failed request must not create a pickup")


# ---------------------------------------------------------------- happy path
class PickupCreateSuccessTests(PickupCreateBase):
    def test_provider_creates_pickup_201_with_contract_schema(self):
        resp = self.post()
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertIs(body["success"], True)
        self.assertIsInstance(body["message"], str)

        data = body["data"]
        self.assertEqual(set(data), RESPONSE_FIELDS)
        self.assertEqual(data["status"], "SCHEDULED")
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["reservation_id"], self.reservation.pk)
        self.assertEqual(data["instructions"], "Temui petugas di pintu utama")
        for field in ("completed_at", "no_show_at", "cancelled_at"):
            self.assertIsNone(data[field])
        self.assertEqual(parse_datetime(data["scheduled_at"]), self.scheduled)

    def test_admin_may_schedule_for_any_reservation(self):
        client = APIClient()
        client.force_login(self.admin)
        resp = self.post(client=client)
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(Pickup.objects.get().reservation_id, self.reservation.pk)

    def test_side_effects_pickup_row_and_reservation_untouched(self):
        before = Reservation.objects.get(pk=self.reservation.pk)
        self.post()
        pickup = Pickup.objects.get()
        self.assertEqual(pickup.status, "SCHEDULED")
        self.assertEqual(pickup.reservation_id, self.reservation.pk)

        after = Reservation.objects.get(pk=self.reservation.pk)
        self.assertEqual(after.status, "ACCEPTED")
        self.assertEqual(after.accepted_snapshot, before.accepted_snapshot)
        self.assertEqual(after.quantity, before.quantity)

    def test_offset_datetime_is_stored_as_same_instant(self):
        local = self.scheduled.astimezone(timezone.get_fixed_timezone(7 * 60))
        resp = self.post(self.payload(scheduled_at=local.isoformat()))
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(Pickup.objects.get().scheduled_at, self.scheduled)

    def test_instructions_default_to_null_when_omitted(self):
        resp = self.post(self.payload(instructions=...))
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIsNone(resp.json()["data"]["instructions"])

    def test_instructions_explicit_null_ok(self):
        resp = self.post(self.payload(instructions=None))
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertIsNone(resp.json()["data"]["instructions"])

    def test_instructions_exactly_2000_chars_ok(self):
        resp = self.post(self.payload(instructions="x" * 2000))
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_each_reservation_gets_its_own_pickup(self):
        second = make_reservation(self.provider, self.receiver, deadline=self.deadline)
        self.assertEqual(self.post().status_code, 201)
        self.assertEqual(self.post(reservation_id=second.pk).status_code, 201)
        self.assertEqual(Pickup.objects.count(), 2)


# ---------------------------------------------------------------- authn / authz / CSRF
class PickupCreateAccessTests(PickupCreateBase):
    def test_unauthenticated_401(self):
        resp = self.post(client=APIClient())
        self.assertError(resp, 401, "authentication_required")
        self.assertNoPickup()

    def test_inactive_account_401(self):
        self.provider.is_active = False
        self.provider.save()  # session dibuat di setUp, akun dinonaktifkan sesudahnya
        resp = self.post()
        self.assertError(resp, 401, "authentication_required")
        self.assertNoPickup()

    def test_missing_csrf_token_403_csrf_failed(self):
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.provider)
        resp = self.post(client=client)
        self.assertError(resp, 403, "csrf_failed")
        self.assertNoPickup()

    def test_valid_csrf_token_accepted(self):
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.provider)
        # GET /auth/csrf/ belum diimplementasi (issue lain), jadi cookie + header dibuat manual.
        secret = _get_new_csrf_string()
        client.cookies[settings.CSRF_COOKIE_NAME] = secret
        resp = client.post(
            self.url(), self.payload(), format="json",
            HTTP_X_CSRFTOKEN=_mask_cipher_secret(secret),
        )
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_receiver_cannot_schedule(self):
        client = APIClient()
        client.force_login(self.receiver)
        self.assertError(self.post(client=client), 403, "permission_denied")
        self.assertNoPickup()

    def test_unrelated_user_cannot_schedule(self):
        client = APIClient()
        client.force_login(self.stranger)
        resp = self.post(client=client)
        self.assertIn(resp.status_code, (403, 404), resp.content)
        self.assertIn(resp.json()["errors"][0]["code"], ("permission_denied", "not_found"))
        self.assertNoPickup()

    def test_reservation_not_found_404(self):
        resp = self.post(reservation_id=999999)
        self.assertError(resp, 404, "not_found")

    def test_non_positive_id_400(self):
        resp = self.post(reservation_id=0)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIs(resp.json()["success"], False)


# ---------------------------------------------------------------- 400 validation
class PickupCreateValidationTests(PickupCreateBase):
    def test_missing_scheduled_at_400(self):
        resp = self.post(self.payload(scheduled_at=...))
        self.assertError(resp, 400, field="scheduled_at")
        self.assertNoPickup()

    def test_scheduled_at_null_400(self):
        resp = self.post(self.payload(scheduled_at=None))
        self.assertError(resp, 400, field="scheduled_at")

    def test_scheduled_at_not_a_datetime_400(self):
        for bad in ("besok jam 11", "2026-13-45T10:00:00Z", 12345, ""):
            with self.subTest(value=bad):
                resp = self.post(self.payload(scheduled_at=bad))
                self.assertError(resp, 400, field="scheduled_at")
        self.assertNoPickup()

    def test_scheduled_at_without_timezone_400(self):
        resp = self.post(self.payload(scheduled_at=self.scheduled.strftime("%Y-%m-%dT%H:%M:%S")))
        self.assertError(resp, 400, field="scheduled_at")

    def test_scheduled_at_in_the_past_400(self):
        past = (timezone.now() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        resp = self.post(self.payload(scheduled_at=past))
        self.assertError(resp, 400, field="scheduled_at")
        self.assertNoPickup()

    def test_instructions_over_2000_chars_400(self):
        resp = self.post(self.payload(instructions="x" * 2001))
        self.assertError(resp, 400, field="instructions")
        self.assertNoPickup()

    def test_server_owned_and_unknown_fields_rejected_400(self):
        for extra in (
            {"status": "COMPLETED"},
            {"location": OTHER_LOCATION},
            {"id": 1},
            {"reservation_id": 1},
            {"version": 9},
            {"whatever": "x"},
        ):
            with self.subTest(extra=extra):
                resp = self.post(self.payload(**extra))
                self.assertError(resp, 400)
        self.assertNoPickup()


# ---------------------------------------------------------------- 409 business rules
class PickupCreateConflictTests(PickupCreateBase):
    def test_reservation_not_accepted_409_invalid_transition(self):
        for status in ("PENDING", "REJECTED", "CANCELLED"):
            with self.subTest(status=status):
                res = make_reservation(self.provider, self.receiver, status=status)
                resp = self.post(reservation_id=res.pk)
                self.assertError(resp, 409, "invalid_transition")
        self.assertNoPickup()

    def test_existing_pickup_409_pickup_exists(self):
        original = make_pickup(self.reservation)
        before = (original.scheduled_at, original.status, original.version)
        resp = self.post()
        self.assertError(resp, 409, "pickup_exists")
        self.assertEqual(Pickup.objects.count(), 1)
        original.refresh_from_db()
        self.assertEqual((original.scheduled_at, original.status, original.version), before)


    def test_terminal_pickup_also_blocks_a_new_one(self):
        for status in ("COMPLETED", "NO_SHOW", "CANCELLED"):
            with self.subTest(status=status):
                res = make_reservation(self.provider, self.receiver, deadline=self.deadline)
                make_pickup(res, status=status)
                resp = self.post(reservation_id=res.pk)
                self.assertError(resp, 409, "pickup_exists")
                self.assertEqual(Pickup.objects.filter(reservation=res).count(), 1)

    def test_second_post_cannot_reschedule(self):
        first = self.post()
        later = (self.scheduled + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        second = self.post(self.payload(scheduled_at=later))
        self.assertEqual(first.status_code, 201)
        self.assertError(second, 409, "pickup_exists")
        self.assertEqual(Pickup.objects.get().scheduled_at, self.scheduled)

    def test_provider_whatsapp_missing_409_contact_required(self):
        for empty in ("", None):
            with self.subTest(value=empty):
                self.provider.whatsapp_number = empty
                self.provider.save()
                self.assertError(self.post(), 409, "contact_required")
        self.assertNoPickup()

    def test_receiver_whatsapp_missing_409_contact_required(self):
        for empty in ("", None):
            with self.subTest(value=empty):
                self.receiver.whatsapp_number = empty
                self.receiver.save()
                self.assertError(self.post(), 409, "contact_required")
        self.assertNoPickup()

    def test_both_whatsapp_missing_409_contact_required(self):
        for user in (self.provider, self.receiver):
            user.whatsapp_number = ""
            user.save()
        self.assertError(self.post(), 409, "contact_required")
        self.assertNoPickup()

    def test_scheduled_at_equal_to_deadline_409(self):
        resp = self.post(self.payload(scheduled_at=self.deadline.strftime("%Y-%m-%dT%H:%M:%SZ")))
        self.assertError(resp, 409, "deadline_passed")
        self.assertNoPickup()

    def test_scheduled_at_after_deadline_409(self):
        after = (self.deadline + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertError(self.post(self.payload(scheduled_at=after)), 409, "deadline_passed")
        self.assertNoPickup()

    def test_scheduled_one_second_before_deadline_ok(self):
        just_before = (self.deadline - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self.post(self.payload(scheduled_at=just_before)).status_code, 201)

    def test_resource_deadline_already_passed_409(self):
        expired = make_reservation(
            self.provider, self.receiver, deadline=timezone.now() - timedelta(minutes=5)
        )
        self.assertError(self.post(reservation_id=expired.pk), 409, "deadline_passed")
        self.assertNoPickup()

    def test_deadline_uses_the_accepted_snapshot_not_the_live_source(self):
        # Snapshot: deadline 2 hari lagi. Sumber live diubah ke deadline lebih dekat dari
        # scheduled_at; hasil tidak boleh berubah karena snapshot bersifat beku.
        ResourceListing.objects.filter(pk=self.reservation.listing_id).update(
            available_until=timezone.now() + timedelta(minutes=5)
        )
        self.assertEqual(self.post().status_code, 201)

    def test_checks_precedence_state_before_contact(self):
        # Reservation yang bukan ACCEPTED tetap melaporkan invalid_transition, meski kontak kosong.
        pending = make_reservation(self.provider, self.receiver, status="PENDING")
        self.provider.whatsapp_number = ""
        self.provider.save()
        self.assertError(self.post(reservation_id=pending.pk), 409, "invalid_transition")


# ---------------------------------------------------------------- snapshot location
class PickupCreateLocationTests(PickupCreateBase):
    def test_location_copied_from_accepted_snapshot(self):
        resp = self.post()
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["data"]["location"], SNAPSHOT_LOCATION)
        self.assertEqual(Pickup.objects.get().location, SNAPSHOT_LOCATION)

    def test_location_in_body_is_rejected_not_used(self):
        resp = self.post(self.payload(location=OTHER_LOCATION))
        self.assertError(resp, 400)
        self.assertNoPickup()

    def test_location_ignores_later_edits_to_the_source(self):
        change_source_location(self.reservation, OTHER_LOCATION)
        resp = self.post()
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["data"]["location"], SNAPSHOT_LOCATION)
        self.assertNotEqual(resp.json()["data"]["location"], OTHER_LOCATION)


# ---------------------------------------------------------------- DB-level guarantee
class PickupUniquePerReservationTests(PickupCreateBase):
    def test_database_rejects_second_pickup_for_same_reservation(self):
        # OneToOneField harus menjaga keunikan walaupun pengecekan di API dilewati.
        original = make_pickup(self.reservation)

        # IntegrityError ditahan di dalam savepoint supaya transaksi tes Django
        # tetap bisa dipakai setelah kegagalan database yang memang diharapkan.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Pickup.objects.create(
                    reservation=self.reservation,
                    scheduled_at=timezone.now() + timedelta(days=1, hours=1),
                    location=self.reservation.accepted_snapshot["location"],
                    status="SCHEDULED",
                )

        self.assertEqual(Pickup.objects.filter(reservation=self.reservation).count(), 1)
        self.assertEqual(Pickup.objects.get(reservation=self.reservation).pk, original.pk)


# ---------------------------------------------------------------- concurrency (PostgreSQL)
class PickupCreateConcurrencyTests(TransactionTestCase):
    # Menguji request API yang berjalan bersamaan dengan koneksi DB yang terpisah.

    reset_sequences = True

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("concurrency test requires PostgreSQL")

        self.provider = make_user("concurrent-provider")
        self.receiver = make_user("concurrent-receiver")
        self.reservation = make_reservation(self.provider, self.receiver)
        self.scheduled = (timezone.now() + timedelta(days=1)).replace(microsecond=0)
        self.scheduled_text = self.scheduled.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _race(self, n=2):
        barrier = threading.Barrier(n)
        results = []
        errors = []
        result_lock = threading.Lock()

        def worker():
            try:
                # Setiap worker punya koneksi DB thread-local dan client sendiri.
                client = APIClient()
                client.force_authenticate(user=self.provider)
                barrier.wait(timeout=10)
                response = client.post(
                    f"/api/v1/reservations/{self.reservation.pk}/pickup/",
                    {"scheduled_at": self.scheduled_text, "instructions": None},
                    format="json",
                )
                try:
                    body = response.json()
                except ValueError:
                    body = {"raw_response": response.content.decode("utf-8", errors="replace")}
                with result_lock:
                    results.append((response.status_code, body))
            except BaseException as exc:
                with result_lock:
                    errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertFalse(
            any(thread.is_alive() for thread in threads),
            "Concurrent request thread did not finish within 30 seconds.",
        )
        self.assertEqual(errors, [], f"Exceptions in concurrent request threads: {errors!r}")
        self.assertEqual(len(results), n, f"Expected {n} responses, got {results!r}")
        return results

    def test_simultaneous_schedule_creates_exactly_one_pickup(self):
        results = self._race(2)
        statuses = sorted(status for status, _ in results)
        self.assertEqual(statuses, [201, 409], results)

        loser = next(body for status, body in results if status == 409)
        self.assertIn("pickup_exists", [error["code"] for error in loser["errors"]])
        self.assertEqual(Pickup.objects.filter(reservation=self.reservation).count(), 1)

    def test_many_simultaneous_requests_still_create_one_pickup(self):
        results = self._race(5)
        statuses = [status for status, _ in results]
        self.assertEqual(statuses.count(201), 1, results)
        self.assertEqual(statuses.count(409), 4, results)
        self.assertEqual(Pickup.objects.filter(reservation=self.reservation).count(), 1)