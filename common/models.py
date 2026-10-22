from django.db import models


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class VersionedModel(TimestampedModel):
    version = models.PositiveIntegerField(default=1)

    class Meta:
        abstract = True
