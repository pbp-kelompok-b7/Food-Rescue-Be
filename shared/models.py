from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditEvent(models.Model):
    """Proposed audit storage; write services must explicitly record safe summaries."""
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    entity_type = models.CharField(max_length=80)
    entity_id = models.CharField(max_length=80)
    action = models.CharField(max_length=80)
    summary = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class LocationSearchCache(models.Model):
    # Hash includes provider, query, area and limit; raw search text is not stored.
    key = models.CharField(max_length=64, primary_key=True)
    results = models.JSONField(default=list)
    expires_at = models.DateTimeField(db_index=True)


class LocationProviderState(models.Model):
    """One database-backed gate per provider host, shared by every worker."""

    key = models.CharField(max_length=64, primary_key=True)
    next_request_at = models.DateTimeField(default=timezone.now)
