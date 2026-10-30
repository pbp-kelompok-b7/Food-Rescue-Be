from decimal import Decimal

from django.db import transaction
from django.db.models import Case, CharField, When, DecimalField, Exists, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.views.decorators.cache import never_cache
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import JSONParser
from common.photos import ResourceMultipartParser
from rest_framework.exceptions import NotAuthenticated, NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated, IsAuthenticatedOrReadOnly

from common.exceptions import BusinessConflict
from common.pagination import ContractPagination
from common.responses import failure, success
from pickups.models import PickupStatus
from reservations.models import Reservation, ReservationStatus
from shared.models import AuditEvent, record_audit, audit_state
from shared.services import allocated_filter, completed_filter, utc_now
from .models import ResourceRequest, RequestStatus
from .serializers import (
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


def requests_with_quantities(now=None):
    now = utc_now(now)
    quantity_field = DecimalField(max_digits=15, decimal_places=3)
    accepted = Reservation.objects.filter(
        request_id=OuterRef("pk"), status=ReservationStatus.ACCEPTED,
    ).order_by()

    def total(query):
        subtotal = query.values("request_id").annotate(total=Sum("quantity")).values("total")
        return Coalesce(Subquery(subtotal, output_field=quantity_field),
                        Value(Decimal("0.000")), output_field=quantity_field)

    return ResourceRequest.objects.select_related("owner").annotate(
        quantity_allocated=total(accepted.filter(allocated_filter(now))),
        quantity_fulfilled=total(accepted.filter(completed_filter())),
    ).annotate(quantity_remaining=F("quantity_needed") - F("quantity_allocated") - F("quantity_fulfilled")).annotate(
        effective_status=Case(
            When(status__in=(RequestStatus.CLOSED, RequestStatus.EXPIRED, RequestStatus.FULFILLED), then=F("status")),
            When(quantity_fulfilled__gte=F("quantity_needed"), then=Value(RequestStatus.FULFILLED)),
            When(valid_until__lte=now, then=Value(RequestStatus.EXPIRED)),
            When(quantity_remaining__lte=0, then=Value(RequestStatus.ALLOCATED)),
            default=Value(RequestStatus.OPEN), output_field=CharField(),
        ),
    )


def public_requests(queryset):
    return queryset.filter(effective_status=RequestStatus.OPEN, valid_until__gt=timezone.now(),
                           quantity_remaining__gt=0, archived_at__isnull=True)


def browse_requests(request):
    query = RequestQuerySerializer(data=request.query_params)
    query.is_valid(raise_exception=True)
    filters = query.validated_data
    if filters.get("page", 1) < 1:
        raise NotFound("Page does not exist.", code="page_not_found")
    queryset = requests_with_quantities()
    if filters["scope"] == "mine":
        if not request.user.is_authenticated or not request.user.is_active:
            raise NotAuthenticated("Login with an active account is required.", code="authentication_required")
        queryset = queryset.filter(owner=request.user)
    else:
        queryset = public_requests(queryset)

    for field in ("resource_type", "category", "intended_use", "status"):
        if field in filters:
            queryset = queryset.filter(**{("effective_status" if field == "status" else field): filters[field]})
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


def create_request(request):
    user = active_user(request)
    serializer = RequestWriteSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    with transaction.atomic():
        instance = ResourceRequest.objects.create(owner=user, created_by=user, **serializer.validated_data)
        record_audit(actor=user, entity_type="ResourceRequest", entity_id=instance.pk,
                     action="create", summary={"before": None, "after": audit_state(instance)})
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

        serializer = RequestWriteSerializer(instance=instance, data=request.data, partial=True,
                                             context={"has_reservations": instance.reservations.exists()})
        serializer.is_valid(raise_exception=True)
        before = audit_state(instance)
        changes = serializer.validated_data
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
        record_audit(actor=user, entity_type="ResourceRequest", entity_id=instance.pk,
                     action="update", summary={"before": before, "after": audit_state(instance),
                                               "changed_fields": sorted(changes)})
        instance = requests_with_quantities().get(pk=pk)
    return success(ResourceRequestSerializer(instance).data, message="Request updated.")


# Lifecycle operations lock the source before reservations in primary-key order.
# Audit writes use the shared SF-BE-05 helper in the same transaction.
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


def _audit(user, entity_type, entity_id, action, reason, before, after):
    record_audit(actor=user, entity_type=entity_type, entity_id=str(entity_id),
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


def close_request(request, pk):
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
        before = _state(source)
        source.status = RequestStatus.CLOSED
        source.version += 1
        source.save(update_fields=["status", "version", "updated_at"])
        reason = body.validated_data["reason"]
        _cancel_pending(user, reservations, reason, now)
        _audit(user, "ResourceRequest", source.pk, "close", reason, before, _state(source))
        result = ResourceRequestSerializer(requests_with_quantities().get(pk=source.pk)).data
    return success(result, message="Request closed.")


def delete_request(request, pk):
    user = active_user(request)
    with transaction.atomic():
        source = _locked_request(user, pk)
        body = RequestDeleteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        reservations = list(source.reservations.select_for_update().order_by("pk"))
        before = _state(source)
        source_id = source.pk
        if not reservations:
            source.delete()
            _audit(user, "ResourceRequest", source_id, "delete", "", before, None)
        elif source.archived_at is None:
            now = timezone.now()
            source.archived_at = now
            if source.status in (RequestStatus.OPEN, RequestStatus.ALLOCATED):
                source.status = RequestStatus.EXPIRED if source.valid_until <= now else RequestStatus.CLOSED
            source.version += 1
            source.save(update_fields=["archived_at", "status", "version", "updated_at"])
            _cancel_pending(user, reservations, "Request archived by owner or admin.", now)
            _audit(user, "ResourceRequest", source_id, "archive", "", before, _state(source))
    return success(status=204)


# GET, POST /api/v1/requests/
@never_cache
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticatedOrReadOnly])
@parser_classes([JSONParser, ResourceMultipartParser])
def requests(request):
    if request.method == "GET":
        return browse_requests(request)
    if request.method == "POST":
        return create_request(request)


# GET, PATCH, DELETE /api/v1/requests/<int:pk>/
@never_cache
@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticatedOrReadOnly])
@parser_classes([JSONParser, ResourceMultipartParser])
def requests_detail(request, pk):
    if request.method == "GET":
        return request_detail(request, pk)
    if request.method == "PATCH":
        return update_request(request, pk)
    if request.method == "DELETE":
        return delete_request(request, pk)


# POST /api/v1/requests/<int:pk>/close/
@never_cache
@api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([JSONParser])
def requests_detail_close(request, pk):
    return close_request(request, pk)


# GET, POST /api/v1/admin/requests/
@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def admin_requests(request):
    if request.method == "GET":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "POST":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET, PATCH, DELETE /api/v1/admin/requests/<int:pk>/
@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAdminUser])
def admin_requests_detail(request, pk):
    if request.method == "GET":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "PATCH":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "DELETE":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/admin/requests/<int:pk>/close/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_requests_detail_close(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
