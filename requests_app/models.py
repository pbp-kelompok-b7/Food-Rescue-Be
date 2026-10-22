from django.conf import settings
from django.db import models
from django.db.models import Q, F
from common.models import VersionedModel
from shared.enums import ResourceType, Mode, Unit


class RequestStatus(models.TextChoices):
    OPEN = "OPEN"
    ALLOCATED = "ALLOCATED"
    FULFILLED = "FULFILLED"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"


class ResourceRequest(VersionedModel):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="resource_requests")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_requests")
    title = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    resource_type = models.CharField(max_length=32, choices=ResourceType.choices)
    category = models.CharField(max_length=120)
    intended_use = models.CharField(max_length=32)
    quantity_needed = models.DecimalField(max_digits=15, decimal_places=3)
    unit = models.CharField(max_length=8, choices=Unit.choices)
    location = models.JSONField(default=dict)
    donate_only = models.BooleanField(default=False)
    max_unit_price = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    valid_until = models.DateTimeField()
    status = models.CharField(max_length=12, choices=RequestStatus.choices, default=RequestStatus.OPEN)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(condition=Q(quantity_needed__gt=0), name="request_quantity_positive"),
            models.CheckConstraint(condition=Q(max_unit_price__isnull=True) | Q(max_unit_price__gte=0), name="request_price_nonnegative"),
        ]
