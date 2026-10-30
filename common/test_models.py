from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from listings.models import ResourceListing
from reservations.models import Reservation
from pickups.models import Pickup


class ModelConstraintTests(TestCase):
    def setUp(self):
        user = get_user_model()
        self.provider = user.objects.create_user(email="provider@example.com", password="test")
        self.receiver = user.objects.create_user(email="receiver@example.com", password="test")
        self.listing = ResourceListing.objects.create(
            owner=self.provider, created_by=self.provider, resource_type="PACKAGED_FOOD_SURPLUS",
            title="Surplus", category="test", description="test", quantity_total="5", unit="box",
            mode="DONATE", unit_price="0", condition="test", available_until=timezone.now())

    def reservation(self, **overrides):
        values = dict(source_type="LISTING_CLAIM", listing=self.listing,
                      provider=self.provider, receiver=self.receiver, submitted_by=self.receiver,
                      quantity="1", unit="box", mode="DONATE", unit_price="0", resource_deadline=timezone.now())
        values.update(overrides)
        return Reservation.objects.create(**values)

    def test_rejects_missing_source_self_transaction_and_negative_quantity(self):
        for invalid in ({"listing": None}, {"receiver": self.provider}, {"quantity": "-1"}, {"unit_price": "100"}):
            with self.subTest(invalid=invalid):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self.reservation(**invalid)

    def test_only_one_pickup_per_reservation(self):
        reservation = self.reservation()
        Pickup.objects.create(reservation=reservation, scheduled_at=timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            Pickup.objects.create(reservation=reservation, scheduled_at=timezone.now())

    def test_source_type_and_both_source_constraints(self):
        from requests_app.models import ResourceRequest
        request = ResourceRequest.objects.create(owner=self.receiver, created_by=self.receiver,
            resource_type="PACKAGED_FOOD_SURPLUS", category="prepared_meal", intended_use="HUMAN_CONSUMPTION",
            quantity_needed="5", unit="box", location={}, donate_only=True, valid_until=timezone.now())
        for values in ({"request": request}, {"source_type": "REQUEST_OFFER"}, {"quantity": "0"},
                       {"mode": "SELL", "unit_price": "0"}):
            with self.subTest(values=values), self.assertRaises(IntegrityError), transaction.atomic():
                self.reservation(**values)
        valid = self.reservation(source_type="REQUEST_OFFER", listing=None, request=request)
        self.assertEqual(valid.request_id, request.pk)


class UserSchemaTests(TestCase):
    def test_email_login_manager_and_case_insensitive_uniqueness(self):
        user = get_user_model().objects.create_user(email="TEST@EXAMPLE.COM", password="test", display_name="Test")
        self.assertEqual(user.email, "test@example.com")
        self.assertTrue(user.check_password("test"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            get_user_model().objects.create_user(email="test@example.com", password="test")


from django.test import TransactionTestCase


class SharedMigrationPreservationTests(TransactionTestCase):
    def test_throttle_migration_preserves_existing_history(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        from requests_app.models import ResourceRequest
        from shared.models import AuditEvent
        old_target = [("shared", "0001_initial")]
        new_target = [("shared", "0003_merge_location_and_throttle")]
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(new_target))
        MigrationExecutor(connection).migrate(old_target)
        user = get_user_model().objects.create_user(email="migration@example.test", display_name="Owner")
        request = ResourceRequest.objects.create(owner=user, created_by=user,
            resource_type="PACKAGED_FOOD_SURPLUS", category="prepared_meal", intended_use="HUMAN_CONSUMPTION",
            quantity_needed="12", unit="box", location={}, donate_only=True, valid_until=timezone.now(),
            status="CLOSED", archived_at=timezone.now(), version=4)
        event = AuditEvent.objects.create(actor=user, entity_type="ResourceRequest", entity_id=str(request.pk),
                                          action="close", summary={"reason": "Riwayat lama"})
        before = (request.status, request.archived_at, request.version, event.created_at, event.summary, event.actor_id)
        MigrationExecutor(connection).migrate(new_target)
        request.refresh_from_db(); event.refresh_from_db()
        self.assertEqual((request.status, request.archived_at, request.version,
                          event.created_at, event.summary, event.actor_id), before)
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM shared_throttlebucket")
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_merge_preserves_existing_location_cache_and_provider_gate(self):
        from datetime import timedelta
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        from shared.models import LocationProviderState, LocationSearchCache, ThrottleBucket

        target = [("shared", "0003_merge_location_and_throttle")]
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(target))
        MigrationExecutor(connection).migrate([
            ("shared", "0002_locationproviderstate_locationsearchcache"),
        ])
        expires_at = timezone.now() + timedelta(days=1)
        gate_at = timezone.now() + timedelta(seconds=1)
        LocationSearchCache.objects.create(key='a' * 64, results=[{'label': 'Depok'}], expires_at=expires_at)
        LocationProviderState.objects.create(key='b' * 64, next_request_at=gate_at)
        before_cache = list(LocationSearchCache.objects.values())
        before_gate = list(LocationProviderState.objects.values())

        MigrationExecutor(connection).migrate(target)

        self.assertEqual(list(LocationSearchCache.objects.values()), before_cache)
        self.assertEqual(list(LocationProviderState.objects.values()), before_gate)
        self.assertEqual(ThrottleBucket.objects.count(), 0)
