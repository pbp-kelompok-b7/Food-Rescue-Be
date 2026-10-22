from django.conf import settings
from django.db import models
from django.db.models import Q, F
from common.models import VersionedModel
from shared.enums import ResourceType, Mode, Unit


class ReservationStatus(models.TextChoices):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class SourceType(models.TextChoices):
    LISTING_CLAIM = "LISTING_CLAIM"
    REQUEST_OFFER = "REQUEST_OFFER"


class Reservation(VersionedModel):
    source_type = models.CharField(max_length=16, choices=SourceType.choices)
    listing = models.ForeignKey("listings.ResourceListing", on_delete=models.PROTECT, null=True, blank=True, related_name="reservations")
    request = models.ForeignKey("requests_app.ResourceRequest", on_delete=models.PROTECT, null=True, blank=True, related_name="reservations")
    provider = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="provided_reservations")
    receiver = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="received_reservations")
    submitted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="submitted_reservations")
    quantity = models.DecimalField(max_digits=15, decimal_places=3)
    unit = models.CharField(max_length=8, choices=Unit.choices)
    mode = models.CharField(max_length=6, choices=Mode.choices)
    unit_price = models.DecimalField(max_digits=15, decimal_places=2)
    # Offer resource data is independent of a public listing.
    resource_data = models.JSONField(default=dict)
    resource_deadline = models.DateTimeField()
    accepted_snapshot = models.JSONField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=ReservationStatus.choices, default=ReservationStatus.PENDING)
    reason = models.TextField(blank=True)
    cancellation_code = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(condition=Q(source_type="LISTING_CLAIM", listing__isnull=False, request__isnull=True) | Q(source_type="REQUEST_OFFER", listing__isnull=True, request__isnull=False), name="reservation_exactly_one_source"),
            models.CheckConstraint(condition=~Q(provider=F("receiver")), name="reservation_distinct_participants"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="reservation_quantity_positive"),
            models.CheckConstraint(condition=Q(mode="DONATE", unit_price=0) | Q(mode="SELL", unit_price__gt=0), name="reservation_price_matches_mode"),
        ]
