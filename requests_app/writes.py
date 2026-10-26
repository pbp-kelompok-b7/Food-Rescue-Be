"""Create/edit operations for RB-BE-02. Locks match the source-first approval flow."""

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotAuthenticated, NotFound, PermissionDenied, ValidationError

from common.exceptions import BusinessConflict
from common.responses import success
from .models import ResourceRequest, RequestStatus
from .queries import public_requests, requests_with_quantities
from .serializers import RequestWriteSerializer, ResourceRequestSerializer


def active_user(request):
    if not request.user.is_authenticated or not request.user.is_active:
        raise NotAuthenticated("Login with an active account is required.", code="authentication_required")
    return request.user


def create_request(request):
    user = active_user(request)
    serializer = RequestWriteSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    with transaction.atomic():
        instance = ResourceRequest.objects.create(owner=user, created_by=user, **serializer.validated_data)
        instance = requests_with_quantities().get(pk=instance.pk)
    return success(ResourceRequestSerializer(instance).data, message="Request created.", status=201)


def update_request(request, pk):
    user = active_user(request)
    pk = str(pk)
    if not pk.isascii() or not pk.isdecimal() or len(pk) > 19 or not 0 < int(pk) <= 9223372036854775807:
        raise ValidationError({"id": "Must be a positive integer ID."})
    with transaction.atomic():
        instance = ResourceRequest.objects.select_for_update().filter(pk=pk).first()
        if instance is None:
            raise NotFound("Request not found.")
        if instance.owner_id != user.pk and not user.is_staff:
            # Match GET visibility: outsiders cannot discover private records.
            is_public = public_requests(requests_with_quantities()).filter(pk=pk).exists()
            participant = instance.reservations.filter(Q(provider=user) | Q(receiver=user)).exists()
            if not is_public and not participant:
                raise NotFound("Request not found.")
            raise PermissionDenied("Only the owner or admin may edit this request.")

        serializer = RequestWriteSerializer(instance=instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        changes = serializer.validated_data
        locked_fields = ("resource_type", "category", "intended_use", "unit")
        if any(field in changes and changes[field] != getattr(instance, field) for field in locked_fields):
            if instance.reservations.exists():
                raise BusinessConflict("Resource type, category, intended use and unit are locked after a reservation.",
                                       code="unit_locked")
        quantities = requests_with_quantities().get(pk=pk)
        needed = changes.get("quantity_needed", instance.quantity_needed)
        if needed < quantities.quantity_allocated + quantities.quantity_fulfilled:
            raise BusinessConflict("Quantity cannot be less than allocated plus fulfilled.", code="quantity_floor")

        terminal = instance.status in (RequestStatus.CLOSED, RequestStatus.EXPIRED, RequestStatus.FULFILLED)
        expired = instance.valid_until <= timezone.now()
        for field, value in changes.items():
            setattr(instance, field, value)
        if not terminal:
            if expired or instance.valid_until <= timezone.now():
                instance.status = RequestStatus.EXPIRED
            elif needed == quantities.quantity_fulfilled:
                instance.status = RequestStatus.FULFILLED
            elif needed == quantities.quantity_allocated + quantities.quantity_fulfilled:
                instance.status = RequestStatus.ALLOCATED
            else:
                instance.status = RequestStatus.OPEN
        instance.version += 1
        instance.save(update_fields=[*changes, "status", "version", "updated_at"])
        instance = requests_with_quantities().get(pk=pk)
    return success(ResourceRequestSerializer(instance).data, message="Request updated.")
