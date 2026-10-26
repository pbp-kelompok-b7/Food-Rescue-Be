from rest_framework import serializers
from common.serializers import StrictSerializer
from .models import User


class GoogleLoginSerializer(StrictSerializer):
    credential = serializers.CharField(max_length=8192, trim_whitespace=False)


class UserSelfSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id", "email", "display_name", "whatsapp_number",
            "is_admin", "is_active", "created_at", "updated_at", "version",
        )
        read_only_fields = fields

class LoginSerializer(StrictSerializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(
        write_only=True,
        trim_whitespace=False,
        max_length=128,
    )

    def validate_email(self, value):
        return value.lower()


class LogoutSerializer(StrictSerializer):
    pass
