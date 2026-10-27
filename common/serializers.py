from collections.abc import Mapping

from rest_framework import serializers


class StrictSerializer(serializers.Serializer):
    """Reject input fields that the endpoint does not accept."""

    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            writable_fields = {
                name for name, field in self.fields.items() if not field.read_only
            }
            unknown_fields = set(data) - writable_fields
            if unknown_fields:
                raise serializers.ValidationError(
                    {field: ["Field tidak dikenal."] for field in sorted(unknown_fields)},
                    code="unknown_field",
                )
        return super().to_internal_value(data)
