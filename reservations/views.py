from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from common.responses import failure, success
from listings.models import ResourceListing
from requests_app.models import ResourceRequest
from .serializers import (
    ListingClaimCreateSerializer,
    RequestOfferCreateSerializer,
    ReservationSerializer,
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
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET, PATCH /api/v1/reservations/<int:pk>/
@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def reservations_detail(request, pk):
    if request.method == "GET":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "PATCH":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


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
