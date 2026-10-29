import re
import math
from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers
from common.serializers import DecimalStringField, RFC3339DateTimeField, StringField
from common.serializers import StrictSerializer
from .models import ResourceListing, ListingStatus
from shared.enums import ResourceType, Suitability, Mode, Unit


CATEGORIES = {
    ResourceType.PACKAGED_FOOD_SURPLUS: ('prepared_meal', 'bakery', 'snack'),
    ResourceType.COMPOSTABLE_FOOD_WASTE: ('coffee_grounds', 'fruit_vegetable_scraps', 'eggshells'),
}


class ListingLocationSerializer(StrictSerializer):
    label = serializers.CharField(min_length=1, max_length=300)
    area = serializers.CharField(min_length=1, max_length=100)
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)

    def to_internal_value(self, data):
        attrs = super().to_internal_value(data)
        errors = {}
        for field in ('latitude', 'longitude'):
            if field not in attrs:
                continue
            raw = data[field]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(attrs[field]):
                errors[field] = 'Koordinat harus berupa angka yang valid.'
        if ('latitude' in attrs) != ('longitude' in attrs):
            errors['longitude' if 'latitude' in attrs else 'latitude'] = 'Koordinat harus dikirim berpasangan.'
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class ListingOwnerSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    display_name = serializers.CharField(read_only=True)


class ListingSerializer(StrictSerializer, serializers.ModelSerializer):
    owner = ListingOwnerSerializer(read_only=True)
    status = serializers.CharField(source='effective_status', read_only=True)
    quantity_available = serializers.DecimalField(max_digits=15, decimal_places=3, read_only=True)
    quantity_allocated = serializers.DecimalField(max_digits=15, decimal_places=3, read_only=True)
    quantity_completed = serializers.DecimalField(max_digits=15, decimal_places=3, read_only=True)
    resource_type = serializers.ChoiceField(choices=ResourceType.choices)
    category = serializers.ChoiceField(choices=[code for codes in CATEGORIES.values() for code in codes])
    description = serializers.CharField(min_length=1, max_length=5000)
    unit = serializers.ChoiceField(choices=Unit.choices)
    mode = serializers.ChoiceField(choices=Mode.choices)
    quantity_total = DecimalStringField(
        max_digits=12, decimal_places=3, min_value=Decimal('0.001'),
        max_value=Decimal('999999999.999'),
    )
    unit_price = DecimalStringField(max_digits=15, decimal_places=2, min_value=Decimal('0'))
    suitability = serializers.ListField(
        child=serializers.ChoiceField(choices=Suitability.choices), min_length=1,
    )
    location = ListingLocationSerializer()
    available_until = RFC3339DateTimeField()
    storage_notes = serializers.CharField(max_length=2000, required=False, allow_null=True, allow_blank=True, default=None)
    allergen_notes = serializers.CharField(max_length=2000, required=False, allow_null=True, allow_blank=True, default=None)

    class Meta:
        model = ResourceListing
        fields = [
            'id', 'created_at', 'updated_at', 'version',
            'owner', 'resource_type', 'title', 'category',
            'description', 'quantity_total', 'unit', 'mode', 'unit_price',
            'condition', 'suitability', 'location', 'available_until',
            'storage_notes', 'allergen_notes', 'packaging_status', 'status', 'archived_at',
            'quantity_available', 'quantity_allocated', 'quantity_completed',
        ]
        read_only_fields = (
            'id', 'created_at', 'updated_at', 'version', 'owner', 'status',
            'archived_at', 'quantity_available', 'quantity_allocated', 'quantity_completed',
        )

    def validate_available_until(self, value):
        raw = self.initial_data.get('available_until')
        if not isinstance(raw, str) or not re.search(
            r'T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$', raw,
        ):
            raise serializers.ValidationError('Gunakan timestamp RFC3339 dengan zona waktu.')
        if value <= timezone.now():
            raise serializers.ValidationError('Deadline harus di masa depan.')
        return value

    def validate(self, attrs):
        if self.instance is not None:
            if not attrs:
                raise serializers.ValidationError('Minimal satu field yang dapat diubah wajib dikirim.')
            if self.context.get('has_reservations'):
                for field in ('resource_type', 'category', 'unit'):
                    if field in attrs and attrs[field] != getattr(self.instance, field):
                        from common.exceptions import BusinessConflict
                        raise BusinessConflict(
                            {field: 'Field terkunci karena listing sudah memiliki reservation.'},
                            code='unit_locked',
                        )
            if 'location' in attrs:
                if not attrs['location']:
                    raise serializers.ValidationError({'location': 'Minimal satu field lokasi wajib dikirim.'})
                attrs['location'] = {**self.instance.location, **attrs['location']}
            values = {
                field: attrs.get(field, getattr(self.instance, field))
                for field in ('resource_type', 'category', 'suitability', 'unit', 'quantity_total', 'mode', 'unit_price')
            }
        else:
            values = attrs

        errors = {}
        if values['category'] not in CATEGORIES[values['resource_type']]:
            errors['category'] = 'Category harus sesuai resource_type.'

        suitability = values['suitability']
        if len(suitability) != len(set(suitability)):
            errors['suitability'] = 'Suitability tidak boleh berulang.'
        elif values['resource_type'] == ResourceType.PACKAGED_FOOD_SURPLUS:
            if suitability != [Suitability.HUMAN_CONSUMPTION]:
                errors['suitability'] = 'Makanan konsumsi hanya menerima HUMAN_CONSUMPTION.'
        elif Suitability.HUMAN_CONSUMPTION in suitability:
            errors['suitability'] = 'Food waste tidak menerima HUMAN_CONSUMPTION.'

        if values['unit'] != Unit.KG and values['quantity_total'] % 1:
            errors['quantity_total'] = 'Unit box, pcs, dan paket harus berjumlah bulat.'
        if values['mode'] == Mode.DONATE and values['unit_price'] != 0:
            errors['unit_price'] = 'Harga DONATE harus nol.'
        elif values['mode'] == Mode.SELL and values['unit_price'] <= 0:
            errors['unit_price'] = 'Harga SELL harus lebih dari nol.'
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class ListingCloseSerializer(StrictSerializer):
    reason = StringField(min_length=1, max_length=1000)


class ListingQuerySerializer(StrictSerializer):
    scope = serializers.ChoiceField(choices=['public', 'mine'], default='public')
    q = serializers.CharField(min_length=2, max_length=100, required=False)
    resource_type = serializers.ChoiceField(choices=ResourceType.choices, required=False)
    category = serializers.ChoiceField(
        choices=[code for codes in CATEGORIES.values() for code in codes],
        required=False,
    )
    mode = serializers.ChoiceField(choices=Mode.choices, required=False)
    suitability = serializers.ChoiceField(choices=Suitability.choices, required=False)
    area = serializers.CharField(required=False)
    status = serializers.ChoiceField(
        choices=ListingStatus.choices, required=False,
    )
    ordering = serializers.ChoiceField(
        choices=[
            "created_at", "-created_at",
            "available_until", "-available_until",
        ],
        required=False,
    )
    page = serializers.IntegerField(required=False)
    page_size = serializers.IntegerField(
        min_value=1, max_value=100, required=False,
    )

    def validate(self, attrs):
        if "status" in attrs and attrs["scope"] != "mine":
            raise serializers.ValidationError({
                "status": "Hanya boleh digunakan pada scope=mine.",
            })
        return attrs
