import math
import re
from collections.abc import Mapping
from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers
from common.serializers import DecimalStringField, RFC3339DateTimeField

from common.exceptions import BusinessConflict
from shared.enums import ResourceType, Suitability, Unit
from .models import ResourceRequest, RequestStatus


# Proposed seed vocabulary from Technical Design Revision 2, section 7.6.1.
CATEGORY_RESOURCE_TYPES = {
    "prepared_meal": ResourceType.PACKAGED_FOOD_SURPLUS,
    "bakery": ResourceType.PACKAGED_FOOD_SURPLUS,
    "snack": ResourceType.PACKAGED_FOOD_SURPLUS,
    "coffee_grounds": ResourceType.COMPOSTABLE_FOOD_WASTE,
    "fruit_vegetable_scraps": ResourceType.COMPOSTABLE_FOOD_WASTE,
    "eggshells": ResourceType.COMPOSTABLE_FOOD_WASTE,
}


class RequestQuerySerializer(serializers.Serializer):
    scope = serializers.ChoiceField(choices=("public", "mine"), default="public")
    q = serializers.CharField(min_length=2, max_length=100, required=False)
    resource_type = serializers.ChoiceField(choices=ResourceType.values, required=False)
    category = serializers.ChoiceField(choices=tuple(CATEGORY_RESOURCE_TYPES), required=False)
    area = serializers.CharField(min_length=1, max_length=100, required=False)
    intended_use = serializers.ChoiceField(choices=Suitability.values, required=False)
    donate_only = serializers.ChoiceField(choices=("true", "false"), required=False)
    status = serializers.ChoiceField(choices=RequestStatus.values, required=False)
    ordering = serializers.ChoiceField(
        choices=("created_at", "-created_at", "valid_until", "-valid_until"),
        default="-created_at",
    )
    page = serializers.IntegerField(required=False)
    page_size = serializers.IntegerField(min_value=1, max_value=100, required=False)

    def to_internal_value(self, data):
        errors = {key: ["Unknown query parameter."] for key in data if key not in self.fields}
        if hasattr(data, "getlist"):
            errors.update({key: ["Supply this parameter only once."]
                           for key in data if len(data.getlist(key)) != 1})
        for key in ("page", "page_size"):
            if key in data:
                raw = str(data[key])
                digits = raw.removeprefix("-") if key == "page" else raw
                if not raw.isascii() or not digits.isdecimal():
                    errors[key] = ["Must be an integer."]
        if errors:
            raise serializers.ValidationError(errors)
        # QueryDict is HTML-form input to DRF: blank optional fields would be
        # silently dropped. Query parameters must be validated when supplied.
        return super().to_internal_value({key: data[key] for key in data})

    def validate(self, attrs):
        if "status" in attrs and attrs["scope"] != "mine":
            raise serializers.ValidationError({"status": "Only allowed with scope=mine."})
        return attrs


class RequestOwnerSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    display_name = serializers.CharField(read_only=True)


class ResourceRequestSerializer(serializers.ModelSerializer):
    owner = RequestOwnerSerializer(read_only=True)
    quantity_remaining = serializers.DecimalField(max_digits=15, decimal_places=3, read_only=True)
    quantity_allocated = serializers.DecimalField(max_digits=15, decimal_places=3, read_only=True)
    quantity_fulfilled = serializers.DecimalField(max_digits=15, decimal_places=3, read_only=True)

    class Meta:
        model = ResourceRequest
        fields = (
            "id", "created_at", "updated_at", "version", "resource_type", "category",
            "intended_use", "quantity_needed", "unit", "location", "donate_only",
            "max_unit_price", "valid_until", "owner", "status", "quantity_remaining",
            "quantity_allocated", "quantity_fulfilled", "archived_at",
        )
        read_only_fields = fields


class StrictObjectSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, Mapping):
            raise serializers.ValidationError({"non_field_errors": ["Expected a JSON object."]})
        unknown = set(data) - set(self.fields)
        if unknown:
            raise serializers.ValidationError({key: "Unknown or read-only field." for key in sorted(unknown)})
        return super().to_internal_value(data)


class CoordinateField(serializers.FloatField):
    def to_internal_value(self, data):
        try:
            valid_number = type(data) in (int, float) and math.isfinite(data)
        except OverflowError:
            valid_number = False
        if not valid_number:
            raise serializers.ValidationError("Expected a finite JSON number.")
        return super().to_internal_value(data)


class LocationTextField(serializers.CharField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            raise serializers.ValidationError("Expected a string.")
        return super().to_internal_value(data)


class RequestCloseSerializer(StrictObjectSerializer):
    reason = LocationTextField(min_length=1, max_length=1000)


class RequestDeleteSerializer(StrictObjectSerializer):
    pass


class RequestLocationSerializer(StrictObjectSerializer):
    label = LocationTextField(min_length=1, max_length=300)
    area = LocationTextField(min_length=1, max_length=100)
    latitude = CoordinateField(min_value=-90, max_value=90)
    longitude = CoordinateField(min_value=-180, max_value=180)


class RequestWriteSerializer(StrictObjectSerializer):
    resource_type = serializers.ChoiceField(choices=ResourceType.values)
    category = serializers.ChoiceField(choices=tuple(CATEGORY_RESOURCE_TYPES))
    intended_use = serializers.ChoiceField(choices=Suitability.values)
    quantity_needed = DecimalStringField(max_digits=15, decimal_places=3)
    unit = serializers.ChoiceField(choices=Unit.values)
    location = RequestLocationSerializer()
    donate_only = serializers.BooleanField(default=False)
    max_unit_price = DecimalStringField(max_digits=15, decimal_places=2, min_value=Decimal("0"),
                                       allow_null=True, default=None)
    valid_until = RFC3339DateTimeField()

    def validate_donate_only(self, value):
        if type(self.initial_data.get("donate_only", False)) is not bool:
            raise serializers.ValidationError("Expected a JSON boolean.")
        return value

    def validate_valid_until(self, value):
        raw = self.initial_data["valid_until"]
        pattern = r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})"
        if not isinstance(raw, str) or not re.fullmatch(pattern, raw):
            raise serializers.ValidationError("Use RFC3339 with an explicit timezone offset.")
        if value <= timezone.now():
            raise serializers.ValidationError("Must be in the future.")
        return value

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Supply at least one mutable field.")

        # A valid enum change can also fail cross-field compatibility. Report
        # the contractual lock conflict first once reservation history exists.
        locked_fields = ("resource_type", "category", "intended_use", "unit")
        if self.instance is not None and self.context.get("has_reservations", False):
            if any(field in attrs and attrs[field] != getattr(self.instance, field) for field in locked_fields):
                raise BusinessConflict("Resource type, category, intended use and unit are locked after a reservation.",
                                       code="unit_locked")

        def current(field):
            return attrs.get(field, getattr(self.instance, field, None))

        resource_type = current("resource_type")
        if CATEGORY_RESOURCE_TYPES[current("category")] != resource_type:
            raise serializers.ValidationError({"category": "Category must match resource_type."})
        human = current("intended_use") == Suitability.HUMAN_CONSUMPTION
        if human != (resource_type == ResourceType.PACKAGED_FOOD_SURPLUS):
            raise serializers.ValidationError({"intended_use": "Intended use must match resource_type."})
        quantity = current("quantity_needed")
        if quantity <= 0:
            raise serializers.ValidationError({"quantity_needed": "Must be greater than zero."})
        if current("unit") != Unit.KG and quantity != quantity.to_integral_value():
            raise serializers.ValidationError({"quantity_needed": "This unit requires a whole quantity."})
        if current("donate_only") and current("max_unit_price") not in (None, Decimal("0")):
            raise serializers.ValidationError({"max_unit_price": "Donate-only requires null or 0.00."})
        if "location" in attrs:
            location = attrs["location"]
            if not location:
                raise serializers.ValidationError({"location": "Supply at least one location field."})
            if ("latitude" in location) != ("longitude" in location):
                raise serializers.ValidationError({"location": "Supply latitude and longitude together."})
            merged = {**(self.instance.location if self.instance else {}), **location}
            complete = RequestLocationSerializer(data=merged)
            complete.is_valid(raise_exception=True)
            attrs["location"] = complete.validated_data
        return attrs
