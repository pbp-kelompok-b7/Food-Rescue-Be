from rest_framework import serializers

from shared.enums import ResourceType, Suitability
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
