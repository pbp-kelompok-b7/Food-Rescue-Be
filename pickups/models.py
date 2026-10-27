from django.conf import settings
from django.db import models
from django.db.models import Q, F
from common.models import VersionedModel
from shared.enums import ResourceType, Mode, Unit


class PickupStatus(models.TextChoices):
    SCHEDULED = "SCHEDULED"
    COMPLETED = "COMPLETED"
    NO_SHOW = "NO_SHOW"
    CANCELLED = "CANCELLED"


class Pickup(VersionedModel):
    reservation = models.OneToOneField("reservations.Reservation", on_delete=models.PROTECT, related_name="pickup")
    scheduled_at = models.DateTimeField()
    location = models.JSONField(default=dict)
    instructions = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=PickupStatus.choices, default=PickupStatus.SCHEDULED)
    completed_at = models.DateTimeField(null=True, blank=True)
    no_show_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ["-scheduled_at", "-pk"]
