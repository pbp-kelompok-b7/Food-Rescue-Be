from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import DecimalField, Exists, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import JSONParser
from rest_framework.exceptions import NotAuthenticated, NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated, IsAuthenticatedOrReadOnly

from common.exceptions import BusinessConflict
from common.pagination import ContractPagination
from common.responses import success
from pickups.models import PickupStatus
from reservations.models import Reservation, ReservationStatus
from shared.models import AuditEvent
from .models import ResourceRequest, RequestStatus
from .serializers import (
    AdminRequestCreateSerializer, AdminRequestQuerySerializer, AdminRequestWriteSerializer,
    RequestCloseSerializer, RequestDeleteSerializer, RequestQuerySerializer,
    RequestWriteSerializer, ResourceRequestSerializer,
)


class RequestPagination(ContractPagination):
    invalid_page_message = "Page does not exist."

    def paginate_queryset(self, queryset, request, view=None):
        try:
            return super().paginate_queryset(queryset, request, view)
        except NotFound as exc:
            raise NotFound("Page does not exist.", code="page_not_found") from exc


def requests_with_quantities():
    quantity_field = DecimalField(max_digits=15, decimal_places=3)
    accepted = Reservation.objects.filter(
        request_id=OuterRef("pk"), status=ReservationStatus.ACCEPTED,
    ).order_by()

    def total(query):
        subtotal = query.values("request_id").annotate(total=Sum("quantity")).values("total")
        return Coalesce(Subquery(subtotal, output_field=quantity_field),
                        Value(Decimal("0.000")), output_field=quantity_field)

    return ResourceRequest.objects.select_related("owner").annotate(
        quantity_allocated=total(accepted.exclude(pickup__status__in=(
            PickupStatus.COMPLETED, PickupStatus.CANCELLED, PickupStatus.NO_SHOW,
        ))),
        quantity_fulfilled=total(accepted.filter(pickup__status=PickupStatus.COMPLETED)),
    ).annotate(quantity_remaining=F("quantity_needed") - F("quantity_allocated") - F("quantity_fulfilled"))


def public_requests(queryset):
    return queryset.filter(status=RequestStatus.OPEN, valid_until__gt=timezone.now(),
                           quantity_remaining__gt=0, archived_at__isnull=True)


def browse_requests(request, *, moderation=False):
    serializer_class = AdminRequestQuerySerializer if moderation else RequestQuerySerializer
    query = serializer_class(data=request.query_params)
    query.is_valid(raise_exception=True)
    filters = query.validated_data
    if filters.get("page", 1) < 1:
        raise NotFound("Page does not exist.", code="page_not_found")
    queryset = requests_with_quantities()
    if moderation:
        if "owner_id" in filters:
            queryset = queryset.filter(owner_id=filters["owner_id"])
        if "archived" in filters:
            queryset = queryset.filter(archived_at__isnull=filters["archived"] == "false")
    elif filters["scope"] == "mine":
        if not request.user.is_authenticated or not request.user.is_active:
            raise NotAuthenticated("Login with an active account is required.", code="authentication_required")
        queryset = queryset.filter(owner=request.user)
    else:
        queryset = public_requests(queryset)

    for field in ("resource_type", "category", "intended_use", "status"):
        if field in filters:
            queryset = queryset.filter(**{field: filters[field]})
    if "donate_only" in filters:
        queryset = queryset.filter(donate_only=filters["donate_only"] == "true")
    if "area" in filters:
        queryset = queryset.filter(location__area__iexact=filters["area"])
    if "q" in filters:
        term = filters["q"]
        queryset = queryset.filter(Q(title__icontains=term) | Q(description__icontains=term)
                                   | Q(category__icontains=term) | Q(location__label__icontains=term)
                                   | Q(location__area__icontains=term))
    ordering = filters["ordering"]
    queryset = queryset.order_by(ordering, "-pk")
    pagination = RequestPagination()
    page = pagination.paginate_queryset(queryset, request)
    return pagination.get_paginated_response(ResourceRequestSerializer(page, many=True).data)


def request_detail(request, pk):
    pk = str(pk)
    if not pk.isascii() or not pk.isdecimal() or len(pk) > 19 or not 0 < int(pk) <= 9223372036854775807:
        raise ValidationError({"id": "Must be a positive integer ID."})
    queryset = requests_with_quantities()
    visible = Q(pk__in=public_requests(queryset).values("pk"))
    user = request.user
    if user.is_authenticated and user.is_active:
        if user.is_staff:
            visible = Q()
        else:
            participation = Reservation.objects.filter(request_id=OuterRef("pk")).filter(
                Q(provider_id=user.pk) | Q(receiver_id=user.pk))
            queryset = queryset.annotate(is_participant=Exists(participation))
            visible |= Q(owner_id=user.pk) | Q(is_participant=True)
    instance = queryset.filter(visible, pk=pk).first()
    if instance is None:
        raise NotFound("Request not found.")
    return success(ResourceRequestSerializer(instance).data)



def active_user(request):
    if not request.user.is_authenticated or not request.user.is_active:
        raise NotAuthenticated("Login with an active account is required.", code="authentication_required")
    return request.user


def create_request(request, *, moderation=False):
    user = active_user(request)
    serializer_class = AdminRequestCreateSerializer if moderation else RequestWriteSerializer
    serializer = serializer_class(data=request.data)
    serializer.is_valid(raise_exception=True)
    with transaction.atomic():
        changes = dict(serializer.validated_data)
        reason = changes.pop("reason", "")
        owner = user
        if moderation:
            owner = get_user_model().objects.select_for_update().filter(pk=changes.pop("owner_id")).first()
            if owner is None:
                raise NotFound("Owner not found.")
            if not owner.is_active:
                raise BusinessConflict("Owner account is inactive.", code="owner_inactive")
        instance = ResourceRequest.objects.create(owner=owner, created_by=user, **changes)
        instance = requests_with_quantities().get(pk=instance.pk)
        if moderation:
            _audit(user, "ResourceRequest", instance.pk, "create", reason, None, _moderation_state(instance))
    return success(ResourceRequestSerializer(instance).data, message="Request created.", status=201)


def update_request(request, pk, *, moderation=False):
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

        serializer_class = AdminRequestWriteSerializer if moderation else RequestWriteSerializer
        serializer = serializer_class(instance=instance, data=request.data, partial=True,
                                             context={"has_reservations": instance.reservations.exists()})
        serializer.is_valid(raise_exception=True)
        changes = dict(serializer.validated_data)
        reason = changes.pop("reason", "")
        quantities = requests_with_quantities().get(pk=pk)
        before = _moderation_state(quantities) if moderation else None
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
        if moderation:
            _audit(user, "ResourceRequest", instance.pk, "update", reason, before, _moderation_state(instance))
    return success(ResourceRequestSerializer(instance).data, message="Request updated.")


# Lifecycle operations lock the source before reservations in primary-key order.
# Keep the audit writer aligned with SF-BE-05 when its shared helper is available.
def _locked_request(user, pk):
    pk = str(pk)
    if not pk.isascii() or not pk.isdecimal() or len(pk) > 19 or not 0 < int(pk) <= 9223372036854775807:
        raise ValidationError({"id": "Must be a positive integer ID."})
    source = ResourceRequest.objects.select_for_update().filter(pk=pk).first()
    if source is None:
        raise NotFound("Request not found.")
    if source.owner_id != user.pk and not user.is_staff:
        visible = public_requests(requests_with_quantities()).filter(pk=pk).exists()
        participant = source.reservations.filter(Q(provider=user) | Q(receiver=user)).exists()
        if not visible and not participant:
            raise NotFound("Request not found.")
        raise PermissionDenied("Only the owner or admin may manage this request.")
    return source


def _state(source):
    return {"status": source.status, "version": source.version,
            "archived_at": source.archived_at.isoformat() if source.archived_at else None}


def _moderation_state(source):
    state = dict(ResourceRequestSerializer(source).data)
    state["created_by_id"] = source.created_by_id
    return state


def _audit(user, entity_type, entity_id, action, reason, before, after):
    AuditEvent.objects.create(actor=user, entity_type=entity_type, entity_id=str(entity_id),
                              action=action, summary={"reason": reason, "before": before, "after": after})


def _cancel_pending(user, reservations, reason, now):
    # ACCEPTED rows are deliberately untouched: no allocation release and no
    # change to scheduled/completed pickups or accepted snapshots.
    for reservation in reservations:
        if reservation.status != ReservationStatus.PENDING:
            continue
        before = {"status": reservation.status, "version": reservation.version,
                  "cancellation_code": reservation.cancellation_code}
        reservation.status = ReservationStatus.CANCELLED
        reservation.cancellation_code = "SOURCE_CLOSED"
        reservation.reason = reason
        reservation.version += 1
        reservation.updated_at = now
        reservation.save(update_fields=["status", "cancellation_code", "reason", "version", "updated_at"])
        _audit(user, "Reservation", reservation.pk, "cancel", reason, before,
               {"status": reservation.status, "version": reservation.version,
                "cancellation_code": reservation.cancellation_code})


def close_request(request, pk, *, moderation=False):
    user = active_user(request)
    with transaction.atomic():
        source = _locked_request(user, pk)
        body = RequestCloseSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        now = timezone.now()
        if (source.status not in (RequestStatus.OPEN, RequestStatus.ALLOCATED)
                or source.archived_at is not None or source.valid_until <= now):
            raise BusinessConflict("Request is already terminal or expired.", code="invalid_transition")
        reservations = list(source.reservations.select_for_update().order_by("pk"))
        quantities = requests_with_quantities().get(pk=source.pk)
        if quantities.quantity_fulfilled >= source.quantity_needed:
            raise BusinessConflict("Request has already been fully fulfilled.", code="invalid_transition")
        before = _moderation_state(quantities) if moderation else _state(source)
        source.status = RequestStatus.CLOSED
        source.version += 1
        source.save(update_fields=["status", "version", "updated_at"])
        reason = body.validated_data["reason"]
        _cancel_pending(user, reservations, reason, now)
        updated = requests_with_quantities().get(pk=source.pk)
        after = _moderation_state(updated) if moderation else _state(source)
        _audit(user, "ResourceRequest", source.pk, "close", reason, before, after)
        result = ResourceRequestSerializer(updated).data
    return success(result, message="Request closed.")


def delete_request(request, pk, *, moderation=False):
    user = active_user(request)
    with transaction.atomic():
        source = _locked_request(user, pk)
        serializer_class = RequestCloseSerializer if moderation else RequestDeleteSerializer
        body = serializer_class(data=request.data)
        body.is_valid(raise_exception=True)
        reservations = list(source.reservations.select_for_update().order_by("pk"))
        before = _moderation_state(requests_with_quantities().get(pk=source.pk)) if moderation else _state(source)
        reason = body.validated_data.get("reason", "")
        source_id = source.pk
        if not reservations:
            source.delete()
            _audit(user, "ResourceRequest", source_id, "delete", reason, before, None)
        elif source.archived_at is None:
            now = timezone.now()
            source.archived_at = now
            if source.status in (RequestStatus.OPEN, RequestStatus.ALLOCATED):
                source.status = RequestStatus.EXPIRED if source.valid_until <= now else RequestStatus.CLOSED
            source.version += 1
            source.save(update_fields=["archived_at", "status", "version", "updated_at"])
            _cancel_pending(user, reservations, reason if moderation else "Request archived by owner or admin.", now)
            after = _moderation_state(requests_with_quantities().get(pk=source.pk)) if moderation else _state(source)
            _audit(user, "ResourceRequest", source_id, "archive", reason, before, after)
    return success(status=204)


# GET, POST /api/v1/requests/
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticatedOrReadOnly])
@parser_classes([JSONParser])
def requests(request):
    if request.method == "GET":
        return browse_requests(request)
    if request.method == "POST":
        return create_request(request)


# GET, PATCH, DELETE /api/v1/requests/<int:pk>/
@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticatedOrReadOnly])
@parser_classes([JSONParser])
def requests_detail(request, pk):
    if request.method == "GET":
        return request_detail(request, pk)
    if request.method == "PATCH":
        return update_request(request, pk)
    if request.method == "DELETE":
        return delete_request(request, pk)


# POST /api/v1/requests/<int:pk>/close/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
def requests_detail_close(request, pk):
    return close_request(request, pk)


class RequestAdminPermission(IsAdminUser):
    def has_permission(self, request, view):
        active_user(request)
        return super().has_permission(request, view)


# GET, POST /api/v1/admin/requests/
@api_view(['GET', 'POST'])
@permission_classes([RequestAdminPermission])
@parser_classes([JSONParser])
def admin_requests(request):
    active_user(request)
    if request.method == "GET":
        return browse_requests(request, moderation=True)
    return create_request(request, moderation=True)


# GET, PATCH, DELETE /api/v1/admin/requests/<str:pk>/
@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([RequestAdminPermission])
@parser_classes([JSONParser])
def admin_requests_detail(request, pk):
    active_user(request)
    if request.method == "GET":
        return request_detail(request, pk)
    if request.method == "PATCH":
        return update_request(request, pk, moderation=True)
    return delete_request(request, pk, moderation=True)


# POST /api/v1/admin/requests/<str:pk>/close/
@api_view(['POST'])
@permission_classes([RequestAdminPermission])
@parser_classes([JSONParser])
def admin_requests_detail_close(request, pk):
    active_user(request)
    return close_request(request, pk, moderation=True)
