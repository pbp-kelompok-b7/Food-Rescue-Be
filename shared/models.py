from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditEvent(models.Model):
    """Safe audit history, written atomically with domain changes."""
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    entity_type = models.CharField(max_length=80)
    entity_id = models.CharField(max_length=80)
    action = models.CharField(max_length=80)
    summary = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class ThrottleBucket(models.Model):
    """Shared sliding-window counters; keys contain no raw account/IP data."""
    key = models.CharField(max_length=64, primary_key=True)
    history = models.JSONField(default=list)
    expires_at = models.DateTimeField(db_index=True)


def audit_state(instance):
    """Only business state, never contacts, resource text, or snapshots."""
    fields = ("status", "version", "quantity", "quantity_total", "quantity_needed",
              "unit", "mode", "unit_price", "donate_only", "max_unit_price",
              "archived_at", "accepted_at", "scheduled_at", "completed_at",
              "no_show_at", "cancelled_at", "cancellation_code", "resource_deadline",
              "available_until", "valid_until", "resource_type", "category", "intended_use")
    return {name: getattr(instance, name) for name in fields if hasattr(instance, name)}


def record_audit(*, actor, entity_type, entity_id, action, summary):
    """Run inside the caller's transaction so audit failure rolls back the write.

    Summaries must contain selected business fields, never request bodies.
    Redaction is a second guard for nested credential/contact keys.
    """
    from datetime import date, datetime
    from decimal import Decimal
    from django.db import connection
    if not connection.in_atomic_block:
        raise RuntimeError("Audit writes require an atomic transaction.")

    def safe(value):
        if isinstance(value, dict):
            return {key: safe(child) for key, child in value.items()
                    if not any(word in key.lower() for word in (
                        "password", "token", "secret", "credential", "authorization",
                        "cookie", "whatsapp", "contact", "accepted_snapshot"))}
        if isinstance(value, (list, tuple)):
            return [safe(child) for child in value]
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        return value

    return AuditEvent.objects.create(actor=actor, entity_type=entity_type,
                                    entity_id=str(entity_id), action=action, summary=safe(summary))

class LocationSearchCache(models.Model):
    # Hash includes provider, query, area and limit; raw search text is not stored.
    key = models.CharField(max_length=64, primary_key=True)
    results = models.JSONField(default=list)
    expires_at = models.DateTimeField(db_index=True)


class LocationProviderState(models.Model):
    """One database-backed gate per provider host, shared by every worker."""

    key = models.CharField(max_length=64, primary_key=True)
    next_request_at = models.DateTimeField(default=timezone.now)
