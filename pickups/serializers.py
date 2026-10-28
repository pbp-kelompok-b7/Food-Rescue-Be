import re
from datetime import timezone as dt_timezone
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import ErrorDetail
from .models import Pickup

RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")

class StrictFieldsSerializer(serializers.Serializer):
    unknown_field_messages = {}

    def to_internal_value(self, data):
        if isinstance(data, dict):
            unknown = set(data) - set(self.fields)
            if unknown:
                raise serializers.ValidationError({
                    name: [ErrorDetail(
                        self.unknown_field_messages.get(name, "Field tidak dikenal."),
                        code="unknown_field",
                    )]
                    for name in sorted(unknown)
                })
        return super().to_internal_value(data)
    
class PickupScheduleSerializer(StrictFieldsSerializer):
    scheduled_at = serializers.DateTimeField()
    instructions = serializers.CharField(
        max_length=2000, allow_null=True, allow_blank=True, required=False, default=None
    )

    def validate_scheduled_at(self, value):
        raw = self.initial_data.get("scheduled_at")
        if not isinstance(raw, str) or not RFC3339.match(raw):
            raise serializers.ValidationError(
                "Gunakan format RFC3339 (2026-10-07T11:00:00+07:00).",
                code="invalid_datetime",
            )
        if value <= timezone.now():
            raise serializers.ValidationError("Jadwal harus di masa depan.", code="schedule_in_past")
        return value
 
    def validate_instructions(self, value):
        raw = self.initial_data.get("instructions")
        if raw is not None and not isinstance(raw, str):
            raise serializers.ValidationError("Harus berupa string atau null.", code="invalid")
        return value or None


class PickupSerializer(serializers.ModelSerializer):
    created_at = serializers.DateTimeField(default_timezone=dt_timezone.utc, read_only=True)
    updated_at = serializers.DateTimeField(default_timezone=dt_timezone.utc, read_only=True)
    scheduled_at = serializers.DateTimeField(default_timezone=dt_timezone.utc, read_only=True)
    completed_at = serializers.DateTimeField(default_timezone=dt_timezone.utc, read_only=True)
    no_show_at = serializers.DateTimeField(default_timezone=dt_timezone.utc, read_only=True)
    cancelled_at = serializers.DateTimeField(default_timezone=dt_timezone.utc, read_only=True)
    reservation_id = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = Pickup
        fields = [
            "id", "created_at", "updated_at", "version", "reservation_id",
            "scheduled_at", "location", "instructions", "status",
            "completed_at", "no_show_at", "cancelled_at",
        ]
        read_only_fields = fields
        
class PickupListQuerySerializer(StrictFieldsSerializer):
    status = serializers.ChoiceField(
        choices=["SCHEDULED", "COMPLETED", "NO_SHOW", "CANCELLED"], required=False
    )
    role = serializers.ChoiceField(choices=["provider", "receiver"], required=False)
    page = serializers.IntegerField(required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=100, required=False)
    
class PickupUpdateSerializer(PickupScheduleSerializer):
    scheduled_at = None
    
    unknown_field_messages = {
        "scheduled_at": (
            "Jadwal pickup tidak dapat diubah. Batalkan reservation "
            "dan ajukan reservation baru untuk jadwal yang berbeda."
        ),
        "location": (
            "Lokasi pickup tidak dapat diubah. Batalkan reservation "
            "dan ajukan reservation baru untuk lokasi yang berbeda."
        ),
    }

class PickupCompleteSerializer(StrictFieldsSerializer):
    pass

class PickupNoShowSerializer(StrictFieldsSerializer):
    reason = serializers.CharField(
        min_length=1, max_length=1000)
    
    def validate_reason(self, value):
        raw = self.initial_data.get("reason")
        if not isinstance(raw, str):
            raise serializers.ValidationError("Harus berupa string.", code="invalid")
        return value