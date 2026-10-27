from datetime import timedelta
import threading

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient

from listings.models import ResourceListing
from reservations.models import Reservation, ReservationStatus, SourceType
from pickups.models import Pickup
from shared.enums import ResourceType, Mode, Unit

FAST_HASHER = override_settings(
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"]
)

User = get_user_model()
PROVIDER_WA = "+6281211110001"
RECEIVER_WA = "+6281211110002"

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

# ---------------------------------------------------------------- factories
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


def make_pickup(reservation, scheduled_at=None, status="SCHEDULED"):
    return Pickup.objects.create(
        reservation=reservation,
        scheduled_at=scheduled_at or (timezone.now() + timedelta(days=1)),
        location=reservation.accepted_snapshot["location"],
        status=status,
    )


# ---------------------------------------------------------------- pickup creation tests
@FAST_HASHER
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
        
    def csrf_token_for(self, client):
            """Start the CSRF flow like the frontend does (contract 7.1.1)."""
            resp = client.get("/api/v1/auth/csrf/")
            assert resp.status_code == 200, resp.content
            return resp.json()["data"]["csrf_token"]


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
        
    def test_blank_instructions_stored_as_null(self):
        for blank in ("", "   "):
            with self.subTest(value=blank):
                res = make_reservation(self.provider, self.receiver, deadline=self.deadline)
                resp = self.post(self.payload(instructions=blank), reservation_id=res.pk)
                self.assertEqual(resp.status_code, 201, resp.content)
                self.assertIsNone(resp.json()["data"]["instructions"])
                self.assertIsNone(Pickup.objects.get(reservation=res).instructions)


# ---------------------------------------------------------------- authn / authz / CSRF
class PickupCreateAccessTests(PickupCreateBase):
    def test_unauthenticated_401(self):
        resp = self.post(client=APIClient())
        self.assertError(resp, 401, "authentication_required")
        self.assertNoPickup()

    def test_inactive_account_401(self):
        self.provider.is_active = False
        self.provider.save()  # session is created in setUp; the account is deactivated afterwards
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
        token = self.csrf_token_for(client)
        resp = client.post(
            self.url(), self.payload(), format="json", HTTP_X_CSRFTOKEN=token,
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
        # Snapshot: deadline is 2 days away. The live source is changed to a deadline earlier than
        # scheduled_at; the result must not change because the snapshot is frozen.
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
        # The OneToOneField must enforce uniqueness even if the API-level check is bypassed.
        original = make_pickup(self.reservation)

        # The IntegrityError is caught inside a savepoint so the Django test transaction
        # remains usable after the expected database failure.
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


# ---------------------------------------------------------------- Pickup list, detail, and instruction update tests

# ---------------------------------------------------------------- Shared fixture and assertion helpers-----------------------
class PickupReadUpdateBase(PickupCreateBase):
    def setUp(self):
        super().setUp()
        self.provider.whatsapp_number = PROVIDER_WA
        self.provider.save()
        self.receiver.whatsapp_number = RECEIVER_WA
        self.receiver.save()
 
        self.pickup = make_pickup(self.reservation, self.scheduled)
 
    # helpers
    def client_for(self, user, **kwargs):
        client = APIClient(**kwargs)
        client.force_login(user)
        return client
 
    def list_url(self):
        return "/api/v1/pickups/"
 
    def detail_url(self, pk=None):
        return f"/api/v1/pickups/{self.pickup.pk if pk is None else pk}/"
 
    def get_list(self, user=None, client=None, **params):
        client = client or self.client_for(user or self.provider)
        return client.get(self.list_url(), params)
 
    def get_detail(self, user=None, client=None, pk=None):
        client = client or self.client_for(user or self.provider)
        return client.get(self.detail_url(pk))
 
    def patch(self, body=None, client=None, pk=None):
        body = {"instructions": "Temui petugas di lobi"} if body is None else body
        return (client or self.client).patch(self.detail_url(pk), body, format="json")
 
    def snapshot_of(self, pickup):
        pickup.refresh_from_db()
        return (
            pickup.version, pickup.instructions, pickup.scheduled_at,
            pickup.location, pickup.status, pickup.updated_at,
        )
 
    def assertPickupSchema(self, data):
        self.assertEqual(set(data), RESPONSE_FIELDS)
 
    def assertNoContactLeak(self, resp):
        text = resp.content.decode("utf-8")
        for secret in (PROVIDER_WA, RECEIVER_WA, "whatsapp"):
            self.assertNotIn(secret, text, "pickup response must not leak contact data")
 
    def make_other_pickup(self, provider=None, receiver=None, status="SCHEDULED", when=None):
        res = make_reservation(
            provider or self.provider, receiver or self.receiver, deadline=self.deadline
        )
        return make_pickup(res, when or self.scheduled, status=status)


# ----------------------------------------------------------------
# LIST PICKUP: access control, visibility, filters, and pagination
class PickupListAccessTests(PickupReadUpdateBase):
    def test_unauthenticated_401(self):
        resp = APIClient().get(self.list_url())
        self.assertError(resp, 401, "authentication_required")
 
    def test_inactive_account_401(self):
        client = self.client_for(self.provider)
        self.provider.is_active = False
        self.provider.save()
        self.assertError(client.get(self.list_url()), 401, "authentication_required")
 
 
class PickupListVisibilityTests(PickupReadUpdateBase):
    def test_provider_sees_own_pickup_with_contract_schema(self):
        resp = self.get_list(self.provider)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertIs(body["success"], True)
        self.assertIsInstance(body["message"], str)
 
        data = body["data"]
        self.assertEqual(set(data), {"count", "next", "previous", "results"})
        self.assertEqual(data["count"], 1)
        self.assertIsNone(data["next"])
        self.assertIsNone(data["previous"])
        self.assertEqual(len(data["results"]), 1)
        item = data["results"][0]
        self.assertPickupSchema(item)
        self.assertEqual(item["id"], self.pickup.pk)
        self.assertEqual(item["reservation_id"], self.reservation.pk)
        self.assertEqual(item["status"], "SCHEDULED")
        self.assertEqual(item["location"], SNAPSHOT_LOCATION)
 
    def test_receiver_sees_the_same_pickup(self):
        resp = self.get_list(self.receiver)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual([r["id"] for r in resp.json()["data"]["results"]], [self.pickup.pk])
 
    def test_stranger_sees_empty_list(self):
        resp = self.get_list(self.stranger)
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["results"], [])
        self.assertIsNone(data["next"])
        self.assertIsNone(data["previous"])
 
    def test_admin_non_participant_only_sees_own_pickups(self):
        resp = self.get_list(self.admin)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["results"], [])
 
    def test_other_users_pickups_are_excluded(self):
        other_provider = make_user("other-provider")
        other_receiver = make_user("other-receiver")
        self.make_other_pickup(other_provider, other_receiver)
        resp = self.get_list(self.provider)
        ids = [r["id"] for r in resp.json()["data"]["results"]]
        self.assertEqual(ids, [self.pickup.pk])
 
    def test_terminal_history_stays_visible(self):
        completed = self.make_other_pickup(status="COMPLETED")
        no_show = self.make_other_pickup(status="NO_SHOW")
        cancelled = self.make_other_pickup(status="CANCELLED")
        resp = self.get_list(self.receiver)
        ids = {r["id"] for r in resp.json()["data"]["results"]}
        self.assertEqual(ids, {self.pickup.pk, completed.pk, no_show.pk, cancelled.pk})
 
    def test_response_does_not_leak_contact(self):
        resp = self.get_list(self.receiver)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertNoContactLeak(resp)
 
 
class PickupListFilterTests(PickupReadUpdateBase):
    def test_filter_by_each_status(self):
        expected = {
            status: self.make_other_pickup(status=status).pk
            for status in ("COMPLETED", "NO_SHOW", "CANCELLED")
        }
        expected["SCHEDULED"] = self.pickup.pk
        for status, pk in expected.items():
            with self.subTest(status=status):
                resp = self.get_list(self.provider, status=status)
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertEqual([r["id"] for r in resp.json()["data"]["results"]], [pk])
 
    def test_filter_by_role(self):
        # self.provider is the provider in self.pickup and the receiver in the other pickup.
        other = make_user("role-other")
        as_receiver = self.make_other_pickup(provider=other, receiver=self.provider)
 
        cases = (
            ("provider", {self.pickup.pk}),
            ("receiver", {as_receiver.pk}),
        )
        for role, expected in cases:
            with self.subTest(role=role):
                resp = self.get_list(self.provider, role=role)
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertEqual({r["id"] for r in resp.json()["data"]["results"]}, expected)
 
        resp = self.get_list(self.provider)  # no role → both
        self.assertEqual(
            {r["id"] for r in resp.json()["data"]["results"]}, {self.pickup.pk, as_receiver.pk}
        )
 
    def test_role_and_status_combined(self):
        other = make_user("combo-other")
        self.make_other_pickup(provider=other, receiver=self.provider, status="COMPLETED")
        done = self.make_other_pickup(status="COMPLETED")
        resp = self.get_list(self.provider, role="provider", status="COMPLETED")
        self.assertEqual([r["id"] for r in resp.json()["data"]["results"]], [done.pk])
 
    def test_invalid_filter_values_400(self):
        for params, field in (
            ({"status": "DONE"}, "status"),
            ({"status": "scheduled"}, "status"),
            ({"role": "admin"}, "role"),
        ):
            with self.subTest(params=params):
                self.assertError(self.get_list(self.provider, **params), 400, field=field)
 
    def test_unknown_query_param_400(self):
        resp = self.get_list(self.provider, whatever="x")
        self.assertError(resp, 400, "unknown_field", "whatever")
 
 
class PickupListPaginationTests(PickupReadUpdateBase):
    def setUp(self):
        super().setUp()
        for i in range(1, 4):
            self.make_other_pickup(when=self.scheduled + timedelta(hours=i))
 
    def test_default_page_size_is_20(self):
        for i in range(20):
            self.make_other_pickup(when=self.scheduled + timedelta(days=1, minutes=i))
        resp = self.get_list(self.provider)
        data = resp.json()["data"]
        self.assertEqual(data["count"], 24)
        self.assertEqual(len(data["results"]), 20)
        self.assertIsNotNone(data["next"])
 
    def test_page_size_and_next_previous_links(self):
        first = self.get_list(self.provider, page_size=2).json()["data"]
        self.assertEqual(first["count"], 4)
        self.assertEqual(len(first["results"]), 2)
        self.assertIsNone(first["previous"])
        self.assertIn("page=2", first["next"])
        self.assertIn("page_size=2", first["next"])
 
        second_response = self.get_list(self.provider, page=2, page_size=2)
        self.assertEqual(second_response.status_code, 200, second_response.content)
        second = second_response.json()["data"]
        self.assertEqual(second["count"], 4)
        self.assertEqual(len(second["results"]), 2)
        self.assertIsNone(second["next"])
        self.assertIsNotNone(second["previous"])
 
        ids = [r["id"] for r in first["results"]] + [r["id"] for r in second["results"]]
        self.assertEqual(len(set(ids)), 4, "pages must not overlap")
 
    def test_page_size_boundaries_ok(self):
        for size in (1, 100):
            with self.subTest(page_size=size):
                resp = self.get_list(self.provider, page_size=size)
                self.assertEqual(resp.status_code, 200, resp.content)
 
    def test_page_size_invalid_400(self):
        for size in (0, 101, -1, "abc"):
            with self.subTest(page_size=size):
                self.assertError(self.get_list(self.provider, page_size=size), 400, field="page_size")
 
    def test_page_out_of_range_404_page_not_found(self):
        resp = self.get_list(self.provider, page=99)
        self.assertError(resp, 404, "page_not_found")
 
    def test_page_below_one_404_page_not_found(self):
        for page in (0, -1):
            with self.subTest(page=page):
                self.assertError(self.get_list(self.provider, page=page), 404, "page_not_found")
 
    def test_page_not_integer_400(self):
        self.assertError(self.get_list(self.provider, page="abc"), 400, field="page")

# ----------------------------------------------------------------
# DETAIL PICKUP: visibility and schema
class PickupDetailTests(PickupReadUpdateBase):
    def test_unauthenticated_401(self):
        self.assertError(APIClient().get(self.detail_url()), 401, "authentication_required")
 
    def test_inactive_account_401(self):
        client = self.client_for(self.receiver)
        self.receiver.is_active = False
        self.receiver.save()
        self.assertError(client.get(self.detail_url()), 401, "authentication_required")
 
    def test_provider_receiver_and_admin_can_read_with_contract_schema(self):
        for user in (self.provider, self.receiver, self.admin):
            with self.subTest(user=user.email):
                resp = self.get_detail(user)
                self.assertEqual(resp.status_code, 200, resp.content)
                body = resp.json()
                self.assertIs(body["success"], True)
                self.assertIsInstance(body["message"], str)
                data = body["data"]
                self.assertPickupSchema(data)
                self.assertEqual(data["id"], self.pickup.pk)
                self.assertEqual(data["reservation_id"], self.reservation.pk)
                self.assertEqual(data["version"], 1)
                self.assertEqual(data["status"], "SCHEDULED")
                self.assertEqual(data["location"], SNAPSHOT_LOCATION)
                self.assertEqual(parse_datetime(data["scheduled_at"]), self.pickup.scheduled_at)
                for field in ("completed_at", "no_show_at", "cancelled_at"):
                    self.assertIsNone(data[field])
 
    def test_non_participant_gets_404_not_403(self):
        resp = self.get_detail(self.stranger)
        self.assertError(resp, 404, "not_found")
 
    def test_pickup_not_found_404(self):
        self.assertError(self.get_detail(self.provider, pk=999999), 404, "not_found")
 
    def test_non_positive_id_400(self):
        resp = self.get_detail(self.provider, pk=0)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIs(resp.json()["success"], False)
 
    def test_terminal_pickup_remains_readable(self):
        for status in ("COMPLETED", "NO_SHOW", "CANCELLED"):
            with self.subTest(status=status):
                pickup = self.make_other_pickup(status=status)
                for user in (self.provider, self.receiver):
                    resp = self.get_detail(user, pk=pickup.pk)
                    self.assertEqual(resp.status_code, 200, resp.content)
                    self.assertEqual(resp.json()["data"]["status"], status)
 
    def test_response_does_not_leak_contact(self):
        for user in (self.provider, self.receiver, self.admin):
            with self.subTest(user=user.email):
                resp = self.get_detail(user)
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertNoContactLeak(resp)

# ---------------------------------------------------------------- PATCH: auth and CSRF
class PickupPatchAccessTests(PickupReadUpdateBase):
    def test_unauthenticated_401(self):
        resp = self.patch(client=APIClient())
        self.assertError(resp, 401, "authentication_required")
        self.assertIsNone(self.snapshot_of(self.pickup)[1])
 
    def test_inactive_account_401(self):
        self.provider.is_active = False
        self.provider.save()
        self.assertError(self.patch(), 401, "authentication_required")
 
    def test_missing_csrf_token_403_csrf_failed(self):
        client = self.client_for(self.provider, enforce_csrf_checks=True)
        resp = self.patch(client=client)
        self.assertError(resp, 403, "csrf_failed")
        self.assertIsNone(self.snapshot_of(self.pickup)[1])
 
    def test_valid_csrf_token_accepted(self):
        client = self.client_for(self.provider, enforce_csrf_checks=True)
        token = self.csrf_token_for(client)
        resp = client.patch(
            self.detail_url(), {"instructions": "Dengan CSRF"}, format="json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(resp.status_code, 200, resp.content)
 
    def test_receiver_is_participant_but_forbidden_403(self):
        resp = self.patch(client=self.client_for(self.receiver))
        self.assertError(resp, 403, "permission_denied")
        self.assertIsNone(self.snapshot_of(self.pickup)[1])
 
    def test_non_participant_gets_404_not_403(self):
        resp = self.patch(client=self.client_for(self.stranger))
        self.assertError(resp, 404, "not_found")
        self.assertIsNone(self.snapshot_of(self.pickup)[1])
 
    def test_pickup_not_found_404(self):
        self.assertError(self.patch(pk=999999), 404, "not_found")
 
    def test_non_positive_id_400(self):
        resp = self.patch(pk=0)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertIs(resp.json()["success"], False)
 
    def test_permission_is_checked_before_validation(self):
        bad = {"instructions": "x" * 2001}
        self.assertError(self.patch(bad, client=self.client_for(self.receiver)), 403, "permission_denied")
        self.assertError(self.patch(bad, client=self.client_for(self.stranger)), 404, "not_found")
 
 
class PickupPatchSuccessTests(PickupReadUpdateBase):
    def test_provider_updates_instructions_200_with_contract_schema(self):
        before = self.snapshot_of(self.pickup)
        resp = self.patch({"instructions": "Temui petugas di pintu utama"})
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertIs(body["success"], True)
        self.assertIsInstance(body["message"], str)
 
        data = body["data"]
        self.assertPickupSchema(data)
        self.assertEqual(data["id"], self.pickup.pk)
        self.assertEqual(data["instructions"], "Temui petugas di pintu utama")
        self.assertEqual(data["version"], before[0] + 1)
        self.assertEqual(data["status"], "SCHEDULED")
        self.assertEqual(data["reservation_id"], self.reservation.pk)
        self.assertEqual(data["location"], SNAPSHOT_LOCATION)
        self.assertEqual(parse_datetime(data["scheduled_at"]), self.scheduled)
        for field in ("completed_at", "no_show_at", "cancelled_at"):
            self.assertIsNone(data[field])

        # persisted: only instructions, version, and updated_at change
        after = self.snapshot_of(self.pickup)
        self.assertEqual(after[1], "Temui petugas di pintu utama")  # instructions
        self.assertEqual(after[0], before[0] + 1)                    # version
        self.assertGreater(after[5], before[5])                      # updated_at
        self.assertEqual(after[2:5], before[2:5])                    # scheduled_at, location, status

    def test_admin_who_is_not_a_participant_can_patch(self):
        resp = self.patch({"instructions": "Dari admin"}, client=self.client_for(self.admin))
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["data"]["instructions"], "Dari admin")
        self.assertEqual(self.snapshot_of(self.pickup)[1], "Dari admin")
 
    def test_reservation_untouched(self):
        before = Reservation.objects.get(pk=self.reservation.pk)
        self.patch({"instructions": "Baru"})
        after = Reservation.objects.get(pk=self.reservation.pk)
        self.assertEqual(after.status, "ACCEPTED")
        self.assertEqual(after.accepted_snapshot, before.accepted_snapshot)
        self.assertEqual(after.resource_deadline, before.resource_deadline)
        self.assertEqual(after.quantity, before.quantity)
        self.assertEqual(Pickup.objects.count(), 1)
 
    def test_instructions_can_be_set_to_null(self):
        self.pickup.instructions = "Lama"
        self.pickup.save()
        resp = self.patch({"instructions": None})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertIsNone(resp.json()["data"]["instructions"])
        self.assertIsNone(self.snapshot_of(self.pickup)[1])
 
    def test_instructions_exactly_2000_chars_ok(self):
        resp = self.patch({"instructions": "x" * 2000})
        self.assertEqual(resp.status_code, 200, resp.content)
 
    def test_repeated_updates_increment_version_each_time(self):
        for i in range(1, 4):
            resp = self.patch({"instructions": f"Versi {i}"})
            self.assertEqual(resp.json()["data"]["version"], 1 + i)

    def test_noop_requests_change_nothing(self):
        # Tanpa field, nilai sama, dan ""/spasi di atas null: 200, tidak ada perubahan, version tetap.
        cases = (
            ("Sama", {}),
            ("Sama", {"instructions": "Sama"}),
            (None, {"instructions": ""}),
            (None, {"instructions": "   "}),
        )
        for current, body in cases:
            with self.subTest(current=current, body=body):
                Pickup.objects.filter(pk=self.pickup.pk).update(instructions=current)
                before = self.snapshot_of(self.pickup)
                resp = self.patch(body)
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertEqual(self.snapshot_of(self.pickup), before)
 
    def test_response_does_not_leak_contact(self):
        resp = self.patch({"instructions": "Baru"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertNoContactLeak(resp)

    def test_blank_instructions_normalised_to_null(self):
        for blank in ("", "   "):
            with self.subTest(value=blank):
                Pickup.objects.filter(pk=self.pickup.pk).update(instructions="Lama")
                resp = self.patch({"instructions": blank})
                self.assertEqual(resp.status_code, 200, resp.content)
                self.assertIsNone(resp.json()["data"]["instructions"])
                self.assertIsNone(self.snapshot_of(self.pickup)[1])


class PickupPatchValidationTests(PickupReadUpdateBase):
    def assertUnchanged(self):
        self.assertEqual(self.snapshot_of(self.pickup)[:5], (
            1, None, self.scheduled, SNAPSHOT_LOCATION, "SCHEDULED",
        ))
 
    def test_instructions_over_2000_chars_400(self):
        resp = self.patch({"instructions": "x" * 2001})
        self.assertError(resp, 400, field="instructions")
        self.assertUnchanged()
 
    def test_instructions_wrong_type_400(self):
        for bad in (123, 1.5, True, ["a"], {"a": 1}):
            with self.subTest(value=bad):
                self.assertError(self.patch({"instructions": bad}), 400, field="instructions")
        self.assertUnchanged()
 
    def test_schedule_and_location_edits_rejected_400(self):
        later = (self.scheduled + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for extra in (
            {"scheduled_at": later},
            {"location": OTHER_LOCATION},
            {"scheduled_at": later, "location": OTHER_LOCATION, "instructions": "Baru"},
        ):
            with self.subTest(extra=extra):
                resp = self.patch(extra)
                self.assertError(resp, 400)
                self.assertIn("unknown_field", [e["code"] for e in resp.json()["errors"]])
        self.assertUnchanged()
 
    def test_server_owned_and_unknown_fields_rejected_400(self):
        for extra in (
            {"status": "COMPLETED"},
            {"id": 1},
            {"reservation_id": 1},
            {"version": 9},
            {"created_at": "2026-01-01T00:00:00Z"},
            {"updated_at": "2026-01-01T00:00:00Z"},
            {"completed_at": "2026-01-01T00:00:00Z"},
            {"no_show_at": "2026-01-01T00:00:00Z"},
            {"cancelled_at": "2026-01-01T00:00:00Z"},
            {"whatever": "x"},
        ):
            with self.subTest(extra=extra):
                body = {"instructions": "Baru", **extra}
                resp = self.patch(body)
                self.assertError(resp, 400)
                field = next(iter(extra))
                self.assertIn(field, [e["field"] for e in resp.json()["errors"]])
        self.assertUnchanged()
 
 
class PickupPatchConflictTests(PickupReadUpdateBase):
    def test_terminal_pickup_409_invalid_transition(self):
        for status in ("COMPLETED", "NO_SHOW", "CANCELLED"):
            with self.subTest(status=status):
                pickup = self.make_other_pickup(status=status)
                before = self.snapshot_of(pickup)
                resp = self.patch({"instructions": "Terlambat"}, pk=pickup.pk)
                self.assertError(resp, 409, "invalid_transition")
                self.assertEqual(self.snapshot_of(pickup), before)
 
    def test_reservation_not_accepted_409_invalid_transition(self):
        Reservation.objects.filter(pk=self.reservation.pk).update(status="CANCELLED")
        before = self.snapshot_of(self.pickup)
        resp = self.patch({"instructions": "Baru"})
        self.assertError(resp, 409, "invalid_transition")
        self.assertEqual(self.snapshot_of(self.pickup), before)
 
    def test_resource_deadline_passed_409_deadline_passed(self):
        Reservation.objects.filter(pk=self.reservation.pk).update(
            resource_deadline=timezone.now() - timedelta(minutes=1)
        )
        before = self.snapshot_of(self.pickup)
        resp = self.patch({"instructions": "Baru"})
        self.assertError(resp, 409, "deadline_passed")
        self.assertEqual(self.snapshot_of(self.pickup), before)

    def test_admin_is_bound_by_the_same_state_rules(self):
        admin_client = self.client_for(self.admin)
        terminal = self.make_other_pickup(status="COMPLETED")
        resp = self.patch({"instructions": "Admin"}, client=admin_client, pk=terminal.pk)
        self.assertError(resp, 409, "invalid_transition")

        Reservation.objects.filter(pk=self.reservation.pk).update(
            resource_deadline=timezone.now() - timedelta(minutes=1)
        )
        resp = self.patch({"instructions": "Admin"}, client=admin_client)
        self.assertError(resp, 409, "deadline_passed")
        
# ---------------------------------------------------------------- concurrency (PostgreSQL)

# ---------------------------------------------------------------- concurrent pickup creation
@FAST_HASHER
class PickupCreateConcurrencyTests(TransactionTestCase):
    # Tests concurrent API requests, each running on its own separate DB connection.

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
                # Each worker has its own thread-local DB connection and its own client.
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
        

# ---------------------------------------------------------------- 
# concurrent instruction update (PostgreSQL)
@FAST_HASHER       
class PickupPatchConcurrencyTests(TransactionTestCase):
    # Concurrent PATCH requests, each on its own separate DB connection.
 
    reset_sequences = True
 
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("concurrency test requires PostgreSQL")
 
        self.provider = make_user("patch-provider")
        self.receiver = make_user("patch-receiver")
        self.reservation = make_reservation(self.provider, self.receiver)
        self.pickup = make_pickup(self.reservation)
 
    def _race(self, n):
        barrier = threading.Barrier(n)
        results, errors = [], []
        lock = threading.Lock()
 
        def worker(index):
            try:
                client = APIClient()
                client.force_authenticate(user=self.provider)
                barrier.wait(timeout=10)
                response = client.patch(
                    f"/api/v1/pickups/{self.pickup.pk}/",
                    {"instructions": f"instruksi-{index}"},
                    format="json",
                )
                try:
                    body = response.json()
                except ValueError:
                    body = {"raw_response": response.content.decode("utf-8", errors="replace")}
                with lock:
                    results.append((response.status_code, body))
            except BaseException as exc:
                with lock:
                    errors.append(exc)
            finally:
                connections.close_all()
 
        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
 
        self.assertFalse(any(t.is_alive() for t in threads), "PATCH thread did not finish in 30s.")
        self.assertEqual(errors, [], f"Exceptions in concurrent threads: {errors!r}")
        self.assertEqual(len(results), n, f"Expected {n} responses, got {results!r}")
        return results
 
    def test_simultaneous_patches_are_serialized_without_lost_updates(self):
        n = 5
        results = self._race(n)
        self.assertEqual([s for s, _ in results], [200] * n, results)
 
        versions = sorted(body["data"]["version"] for _, body in results)
        self.assertEqual(versions, list(range(2, n + 2)), "each PATCH must bump version exactly once")
 
        self.pickup.refresh_from_db()
        self.assertEqual(self.pickup.version, n + 1)
        self.assertIn(self.pickup.instructions, {f"instruksi-{i}" for i in range(n)})
        self.assertEqual(self.pickup.status, "SCHEDULED")
        self.assertEqual(Pickup.objects.filter(reservation=self.reservation).count(), 1)