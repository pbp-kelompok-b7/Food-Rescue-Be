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


class UserSchemaTests(TestCase):
    def test_email_login_manager_and_case_insensitive_uniqueness(self):
        user = get_user_model().objects.create_user(email="TEST@EXAMPLE.COM", password="test", display_name="Test")
        self.assertEqual(user.email, "test@example.com")
        self.assertTrue(user.check_password("test"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            get_user_model().objects.create_user(email="test@example.com", password="test")
