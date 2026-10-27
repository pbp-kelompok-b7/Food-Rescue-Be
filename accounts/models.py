from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower
from .managers import UserManager


class User(AbstractUser):
    username = None
    email = models.EmailField(unique=True)
    google_sub = models.CharField(max_length=255, unique=True, null=True, blank=True)
    display_name = models.CharField(max_length=100)
    whatsapp_number = models.CharField(
        max_length=16, null=True, blank=True,
        validators=[RegexValidator(r"^\+[1-9]\d{7,14}$", "Use E.164 format.")],
    )
    updated_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["display_name"]
    objects = UserManager()

    @property
    def created_at(self):
        return self.date_joined

    @property
    def is_admin(self):
        return self.is_staff

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), name="user_email_case_insensitive")]
