from django.conf import settings
from django.db import models
from django.db.models import Q, F
from common.models import VersionedModel
from shared.enums import ResourceType, Mode, Unit
from common.photos import photo_path


class ListingStatus(models.TextChoices):
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"


class ResourceListing(VersionedModel):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="listings")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_listings")
    resource_type = models.CharField(max_length=32, choices=ResourceType.choices)
    title = models.CharField(max_length=200)
    category = models.CharField(max_length=120)
    description = models.TextField()
    image = models.ImageField(upload_to=photo_path, blank=True, null=True)
    quantity_total = models.DecimalField(max_digits=15, decimal_places=3)
    unit = models.CharField(max_length=8, choices=Unit.choices)
    mode = models.CharField(max_length=6, choices=Mode.choices)
    unit_price = models.DecimalField(max_digits=15, decimal_places=2)
    condition = models.CharField(max_length=1000)
    suitability = models.JSONField(default=list)
    location = models.JSONField(default=dict)
    available_until = models.DateTimeField()
    storage_notes = models.TextField(null=True, blank=True)
    allergen_notes = models.TextField(null=True, blank=True)
    packaging_status = models.CharField(max_length=200, null=True, blank=True)
    status = models.CharField(max_length=12, choices=ListingStatus.choices, default=ListingStatus.AVAILABLE)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity_total__gt=0), name="listing_quantity_positive"),
            models.CheckConstraint(condition=Q(mode="DONATE", unit_price=0) | Q(mode="SELL", unit_price__gt=0), name="listing_price_matches_mode"),
        ]
