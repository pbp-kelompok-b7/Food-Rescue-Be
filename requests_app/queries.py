"""Read-only request queries; lifecycle writes belong to transaction/expiry flows."""

from decimal import Decimal

from django.db.models import DecimalField, Exists, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.exceptions import NotAuthenticated, NotFound, ValidationError

from common.pagination import ContractPagination
from common.responses import success
from pickups.models import PickupStatus
from reservations.models import Reservation, ReservationStatus
from .models import ResourceRequest, RequestStatus
from .serializers import RequestQuerySerializer, ResourceRequestSerializer


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
