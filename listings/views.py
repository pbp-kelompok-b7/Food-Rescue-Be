from django.db.models import Q
from django.utils import timezone
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import NotAuthenticated, NotFound, ValidationError
from rest_framework.permissions import IsAdminUser, IsAuthenticated, IsAuthenticatedOrReadOnly
from common.pagination import ContractPagination
from common.responses import failure, success
from common.serializers import StrictSerializer
from .models import ListingStatus
from .queries import listings_with_state
from .serializers import ListingQuerySerializer, ListingSerializer
from . import services


def _listing_id(value):
    try:
        if not value.isascii() or not value.isdecimal():
            raise ValueError
        pk = int(value)
        if not 0 < pk <= 9223372036854775807:
            raise ValueError
        return pk
    except (ValueError, TypeError):
        raise ValidationError({'id': 'ID harus berupa integer positif yang valid.'})


# GET, POST /api/v1/listings/
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticatedOrReadOnly])
def listings(request):
    if request.method == "GET":
        # Treat an explicitly empty parameter as input, rather than an omitted HTML field.
        serializer = ListingQuerySerializer(data=request.query_params.dict())
        serializer.is_valid(raise_exception=True)

        filters = serializer.validated_data

        now = timezone.now()
        queryset = listings_with_state(now)

        if filters['scope'] == 'mine':
            if not request.user.is_authenticated:
                raise NotAuthenticated()
            queryset = queryset.filter(owner=request.user)
        else:
            queryset = queryset.filter(
                effective_status=ListingStatus.AVAILABLE,
                quantity_available__gt=0,
                available_until__gt=now,
                archived_at__isnull=True,
            )

        if 'q' in filters:
            term = filters['q']
            queryset = queryset.filter(
                Q(title__icontains=term) | Q(description__icontains=term)
                | Q(category__icontains=term)
            )

        for field in ('resource_type', 'category', 'mode'):
            if field in filters:
                queryset = queryset.filter(**{field: filters[field]})

        if 'suitability' in filters:
            queryset = queryset.filter(suitability__contains=[filters['suitability']])
        if 'area' in filters:
            queryset = queryset.filter(location__area__iexact=filters['area'])
        if 'status' in filters:
            queryset = queryset.filter(effective_status=filters['status'])

        queryset = queryset.order_by(filters.get('ordering', '-created_at'), '-id')
        pagination = ContractPagination()
        try:
            page = pagination.paginate_queryset(queryset, request)
        except NotFound as exc:
            raise NotFound('Halaman tidak ditemukan.', code='page_not_found') from exc

        data = ListingSerializer(page, many=True).data
        return pagination.get_paginated_response(data)

    if request.method == "POST":
        serializer = ListingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        listing = serializer.save(owner=request.user, created_by=request.user)

        listing = listings_with_state(timezone.now()).get(pk=listing.pk)

        return success(
            ListingSerializer(listing).data,
            message="Listing berhasil dibuat",
            status=201,
        )


# GET, PATCH, DELETE /api/v1/listings/{id}/
@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticatedOrReadOnly])
def listings_detail(request, pk):
    pk = _listing_id(pk)
    StrictSerializer(data=request.query_params.dict()).is_valid(raise_exception=True)
    if request.method == "GET":
        now = timezone.now()
        listing = get_object_or_404(listings_with_state(now), pk=pk)
        publik = (
            listing.effective_status == ListingStatus.AVAILABLE
            and listing.quantity_available > 0
            and listing.available_until > now
            and listing.archived_at is None
        )

        if not publik:
            user = request.user
            if not user.is_authenticated:
                raise NotFound()

            berhak = (
                listing.owner_id == user.pk
                or user.is_staff
                or listing.reservations.filter(
                    Q(provider=user) | Q(receiver=user),
                ).exists()
            )
            if not berhak:
                raise NotFound()

        data = ListingSerializer(listing).data

        return success(data=data, message='Data berhasil diambil')
    if request.method == "PATCH":
        listing = services.update_listing(pk, request.user, request.data)
        return success(ListingSerializer(listing).data, 'Perubahan berhasil disimpan')
    if request.method == "DELETE":
        services.delete_listing(pk, request.user, request.data)
        return success(status=204)


# POST /api/v1/listings/{id}/close/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def listings_detail_close(request, pk):
    pk = _listing_id(pk)
    StrictSerializer(data=request.query_params.dict()).is_valid(raise_exception=True)
    listing = services.close_listing(pk, request.user, request.data)
    return success(ListingSerializer(listing).data, 'Listing berhasil ditutup')


# GET, POST /api/v1/admin/listings/
@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def admin_listings(request):
    if request.method == "GET":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "POST":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET, PATCH, DELETE /api/v1/admin/listings/<int:pk>/
@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAdminUser])
def admin_listings_detail(request, pk):
    if request.method == "GET":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "PATCH":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "DELETE":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/admin/listings/<int:pk>/close/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_listings_detail_close(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
