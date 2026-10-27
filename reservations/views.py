from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from common.pagination import ContractPagination
from common.responses import failure, success
from listings.models import ResourceListing
from requests_app.models import ResourceRequest
from .models import Reservation, ReservationStatus, SourceType
from .serializers import (
    ListingClaimCreateSerializer,
    RequestOfferCreateSerializer,
    ReservationReviseSerializer,
    ReservationSerializer,
)


# Query params accepted by the reservation inbox list (section 7.4.3).
_ROLE_CHOICES = {"provider", "receiver"}
_RESERVATION_SELECT_RELATED = (
    "provider", "receiver", "listing", "request", "pickup",
)


# POST /api/v1/listings/<int:pk>/claims/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def listings_detail_claims(request, pk):
    """Create a PENDING LISTING_CLAIM reservation for the given listing.

    The receiver (claimant) is the authenticated session user; the provider is
    the listing owner. Unit, mode, and unit_price are copied from the listing;
    the body only accepts ``quantity``. A PENDING claim does not allocate stock.
    """
    with transaction.atomic():
        # Lock the listing row so the availability check and creation are
        # consistent under concurrent claims (effective on PostgreSQL).
        listing = get_object_or_404(
            ResourceListing.objects.select_for_update(), pk=pk
        )
        serializer = ListingClaimCreateSerializer(
            data=request.data, listing=listing, user=request.user
        )
        serializer.is_valid(raise_exception=True)
        reservation = serializer.save()
    return success(ReservationSerializer(reservation).data, status=201)


# POST /api/v1/requests/<int:pk>/offers/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def requests_detail_offers(request, pk):
    """Create a PENDING REQUEST_OFFER reservation for the given request.

    The provider (offering user) is the authenticated session user; the receiver
    is the request owner. The provider supplies the resource detail, quantity,
    mode, unit_price, and resource_deadline; no public listing is required. A
    PENDING offer does not reduce the request's remaining need.
    """
    with transaction.atomic():
        # Lock the request row so the remaining-need check and creation are
        # consistent under concurrent offers (effective on PostgreSQL).
        resource_request = get_object_or_404(
            ResourceRequest.objects.select_for_update(), pk=pk
        )
        serializer = RequestOfferCreateSerializer(
            data=request.data, request_obj=resource_request, user=request.user
        )
        serializer.is_valid(raise_exception=True)
        reservation = serializer.save()
    return success(
        ReservationSerializer(reservation).data,
        message="Offer berhasil diajukan",
        status=201,
    )


# GET /api/v1/reservations/
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def reservations(request):
    """List the reservations the authenticated user participates in (7.4.3).

    Only reservations where the user is provider or receiver are returned.
    Supports ``role`` (provider|receiver), ``status``, and ``source_type``
    filters, is paginated, and ordered by ``-created_at, -id``.
    """
    user = request.user
    qs = (
        Reservation.objects.select_related(*_RESERVATION_SELECT_RELATED)
        .filter(Q(provider=user) | Q(receiver=user))
        .order_by("-created_at", "-id")
    )

    role = request.query_params.get("role")
    if role is not None:
        if role not in _ROLE_CHOICES:
            raise ValidationError({"role": "Must be 'provider' or 'receiver'."})
        qs = qs.filter(provider=user) if role == "provider" else qs.filter(receiver=user)

    status_param = request.query_params.get("status")
    if status_param is not None:
        if status_param not in ReservationStatus.values:
            raise ValidationError({"status": "Unknown reservation status."})
        qs = qs.filter(status=status_param)

    source_type = request.query_params.get("source_type")
    if source_type is not None:
        if source_type not in SourceType.values:
            raise ValidationError({"source_type": "Unknown source type."})
        qs = qs.filter(source_type=source_type)

    paginator = ContractPagination()
    page = paginator.paginate_queryset(qs, request)
    data = ReservationSerializer(page, many=True, context={"viewer": user}).data
    return paginator.get_paginated_response(data)


def _get_participant_reservation(pk, user):
    """Fetch a reservation visible to the user, else raise 404.

    A reservation is visible to its provider, its receiver, or any admin. For
    everyone else it is hidden (404) so that guessing ids does not leak data
    (section 6.1 privacy rule).
    """
    reservation = get_object_or_404(
        Reservation.objects.select_related(*_RESERVATION_SELECT_RELATED), pk=pk
    )
    is_participant = user.id in (reservation.provider_id, reservation.receiver_id)
    if not (is_participant or user.is_staff):
        raise Http404
    return reservation


# GET, PATCH /api/v1/reservations/<int:pk>/
@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def reservations_detail(request, pk):
    if request.method == "GET":
        # Participant or admin may read; history is readable in every status.
        reservation = _get_participant_reservation(pk, request.user)
        data = ReservationSerializer(reservation, context={"viewer": request.user}).data
        return success(data)

    if request.method == "PATCH":
        # Revise the pending quantity. Only the submitter may do this.
        with transaction.atomic():
            reservation = get_object_or_404(
                Reservation.objects.select_for_update().select_related(
                    "listing", "request"
                ),
                pk=pk,
            )
            is_participant = request.user.id in (
                reservation.provider_id, reservation.receiver_id
            )
            if not is_participant:
                # Hide existence from non-participants (privacy 404).
                raise Http404
            if request.user.id != reservation.submitted_by_id:
                raise PermissionDenied("Only the submitter may revise this reservation.")
            serializer = ReservationReviseSerializer(
                data=request.data, reservation=reservation, user=request.user
            )
            serializer.is_valid(raise_exception=True)
            serializer.save()
        reservation.refresh_from_db()
        data = ReservationSerializer(reservation, context={"viewer": request.user}).data
        return success(data, message="Perubahan berhasil disimpan")


# POST /api/v1/reservations/<int:pk>/accept/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def reservations_detail_accept(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/reservations/<int:pk>/reject/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def reservations_detail_reject(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/reservations/<int:pk>/cancel/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def reservations_detail_cancel(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET /api/v1/admin/reservations/
@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_reservations(request):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET, PATCH /api/v1/admin/reservations/<int:pk>/
@api_view(['GET', 'PATCH'])
@permission_classes([IsAdminUser])
def admin_reservations_detail(request, pk):
    if request.method == "GET":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "PATCH":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/admin/reservations/<int:pk>/accept/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_reservations_detail_accept(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/admin/reservations/<int:pk>/reject/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_reservations_detail_reject(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/admin/reservations/<int:pk>/cancel/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_reservations_detail_cancel(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
