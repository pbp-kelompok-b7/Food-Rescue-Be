"""Bounded photo uploads. Multipart keeps the existing JSON types intact."""
import json
import warnings
from io import BytesIO
from uuid import uuid4

from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework import serializers
from rest_framework.exceptions import ParseError
from rest_framework.parsers import MultiPartParser


def photo_path(instance, filename):
    return f"photos/{uuid4().hex}.jpg"


class ResourceMultipartParser(MultiPartParser):
    def parse(self, stream, media_type=None, parser_context=None):
        parsed = super().parse(stream, media_type, parser_context)
        if set(parsed.data) != {"payload"} or set(parsed.files) - {"image"}:
            raise ParseError("Kirim payload JSON dan satu file image opsional.")
        if len(parsed.data.getlist("payload")) != 1 or len(parsed.files.getlist("image")) > 1:
            raise ParseError("Field upload tidak boleh berulang.")
        try:
            payload = json.loads(parsed.data["payload"])
        except (ValueError, TypeError) as exc:
            raise ParseError("Payload harus berupa JSON yang valid.") from exc
        if not isinstance(payload, dict):
            raise ParseError("Payload harus berupa objek JSON.")
        if "image" in parsed.files:
            if "image" in payload:
                raise ParseError("Image tidak boleh dikirim dua kali.")
            payload["image"] = parsed.files["image"]
        return payload


class PhotoField(serializers.FileField):
    def __init__(self, **kwargs):
        super().__init__(required=False, allow_null=True, **kwargs)

    def to_internal_value(self, data):
        upload = super().to_internal_value(data)
        if upload.size > 5 * 1024 * 1024:
            raise serializers.ValidationError("Ukuran foto maksimal 5 MB.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(upload) as image:
                    if image.format not in ("JPEG", "PNG", "WEBP"):
                        raise ValueError("Unsupported format")
                    if image.width * image.height > 20_000_000:
                        raise ValueError("Image too large")
                    image.load()
                    image = ImageOps.exif_transpose(image).convert("RGB")
                    image.thumbnail((1920, 1920))
                    output = BytesIO()
                    image.save(output, "JPEG", quality=85)
            # Fresh JPEG strips metadata and does not trust the filename/MIME type.
            return ContentFile(output.getvalue(), name="photo.jpg")
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombWarning,
                Image.DecompressionBombError) as exc:
            raise serializers.ValidationError("Gunakan foto JPG, PNG, atau WebP yang valid (maksimal 20 megapiksel).") from exc
