from rest_framework import serializers
from common.serializers import StrictSerializer


class LocationSearchQuerySerializer(StrictSerializer):
    q = serializers.CharField(min_length=3, max_length=200)
    area = serializers.CharField(min_length=1, max_length=100, required=False)
    limit = serializers.IntegerField(min_value=1, max_value=10, default=5)

    def to_internal_value(self, data):
        if hasattr(data, "getlist"):
            repeated = {key: ["Kirim parameter satu kali saja."]
                        for key in data if len(data.getlist(key)) != 1}
            if repeated:
                raise serializers.ValidationError(repeated)
        if "limit" in data and (not str(data["limit"]).isascii()
                                or not str(data["limit"]).isdecimal()):
            raise serializers.ValidationError({"limit": "Harus berupa integer 1 sampai 10."})
        # Avoid DRF's HTML-input handling silently dropping explicitly blank fields.
        return super().to_internal_value(dict(data.items()))

    def validate_q(self, value):
        return " ".join(value.split())

    def validate_area(self, value):
        return " ".join(value.split())


class LocationSuggestionQuerySerializer(LocationSearchQuerySerializer):
    q = serializers.CharField(min_length=2, max_length=200)
