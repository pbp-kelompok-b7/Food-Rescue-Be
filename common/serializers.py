from collections.abc import Mapping
from datetime import timezone as dt_timezone
import math
import re

from rest_framework import serializers


class StrictSerializer(serializers.Serializer):
    """Reject input fields that the endpoint does not accept."""

    def to_internal_value(self, data):
        if hasattr(data, "getlist"):
            validate_query_params(data, set(self.fields))
            data = {key: data[key] for key in data}
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
        if not isinstance(data, Mapping):
            raise serializers.ValidationError({"non_field_errors": ["Expected a JSON object."]})
        return super().to_internal_value(data)


class DecimalStringField(serializers.DecimalField):
    """Contract decimal input: never coerce JSON numbers or exponent notation."""
    def to_internal_value(self, data):
        if not isinstance(data, str) or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", data):
            self.fail("invalid")
        return super().to_internal_value(data)


class RFC3339DateTimeField(serializers.DateTimeField):
    def __init__(self, **kwargs):
        kwargs.setdefault("default_timezone", dt_timezone.utc)
        super().__init__(**kwargs)

    def to_internal_value(self, value):
        if not isinstance(value, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})", value
        ):
            self.fail("invalid", format="RFC3339 with timezone offset")
        return super().to_internal_value(value)


class CoordinateField(serializers.FloatField):
    def to_internal_value(self, value):
        try:
            valid = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            valid = False
        if not valid:
            self.fail("invalid")
        return super().to_internal_value(value)


def validate_query_params(data, allowed):
    errors = {key: serializers.ErrorDetail("Unknown query parameter.", code="unknown_field")
              for key in data if key not in allowed}
    for key in data:
        if hasattr(data, "getlist") and len(data.getlist(key)) != 1:
            errors[key] = "Supply this parameter only once."
        elif data[key] == "":
            errors[key] = "This parameter may not be blank."
        elif key in ("page", "page_size"):
            raw = str(data[key])
            digits = raw.removeprefix("-") if key == "page" else raw
            if not raw.isascii() or not digits.isdecimal():
                errors[key] = "Must be an integer."
    if errors:
        raise serializers.ValidationError(errors)


class StringField(serializers.CharField):
    def to_internal_value(self, value):
        if not isinstance(value, str):
            self.fail("invalid")
        return super().to_internal_value(value)
