from django.contrib.auth.password_validation import validate_password
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


class RegisterSerializer(StrictSerializer):
    email = serializers.EmailField(max_length=254)
    password = serializers.CharField(
        write_only=True,
        trim_whitespace=False,
        min_length=8,
        max_length=128,
        validators=[validate_password]
    )
    display_name = serializers.CharField(min_length=1, max_length=100)
    whatsapp_number = serializers.RegexField(
        regex=r"^\+[0-9]{8,15}$",
        required=False,
        allow_null=True,
        default=None,
    )
    def validate_email(self, value):
        email = value.lower()

        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError("Email sudah terdaftar", code='email_exists')
        
        return email

class LogoutSerializer(StrictSerializer):
    pass

class ProfileSerializer(StrictSerializer):
    display_name=serializers.CharField(min_length=1, max_length=100, required=False)
    whatsapp_number = serializers.RegexField(
        regex=r"^\+[1-9]\d{7,14}$",
        required=False,
        allow_null=True,
    )

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                'Minimal mengisi satu field',
                code='empty_update',
            )

        return attrs

