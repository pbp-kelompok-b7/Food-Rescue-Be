from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError, ErrorDetail
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from common.exceptions import *
from common.responses import *
from reservations.models import Reservation, ReservationStatus
from .models import Pickup, PickupStatus
from .serializers import *


# POST /api/v1/reservations/<int:pk>/pickup/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def reservations_detail_pickup(request, pk):
    if pk <= 0:
        raise ValidationError(
            {"id": [ErrorDetail("ID harus bilangan bulat positif.", code="invalid")]}
        )
        
    user = request.user
    with transaction.atomic():
        # Lock reservation (schedule ganda / bersaing dengan cancel)
        reservation = (
            Reservation.objects.select_for_update(of=("self",))
            .select_related("provider", "receiver")
            .filter(pk=pk)
            .first()
        )
        if reservation is None or not (user.is_admin or user.pk in (reservation.provider_id, reservation.receiver_id)):
            raise NotFound()
        if not user.is_admin and user.pk != reservation.provider_id:
            raise PermissionDenied()

        serializer = PickupScheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        scheduled_at = serializer.validated_data["scheduled_at"]

        if reservation.status != ReservationStatus.ACCEPTED:
            raise BusinessConflict("Reservation harus berstatus ACCEPTED.", code="invalid_transition")
        if Pickup.objects.filter(reservation_id=reservation.pk).exists():
            raise BusinessConflict("Reservation sudah memiliki pickup.", code="pickup_exists")
        if not (reservation.provider.whatsapp_number and reservation.receiver.whatsapp_number):
            raise BusinessConflict("Nomor WhatsApp provider dan receiver wajib diisi.", code="contact_required")
        if timezone.now() >= reservation.resource_deadline or scheduled_at >= reservation.resource_deadline:
            raise BusinessConflict(
                {"scheduled_at": "Jadwal harus lebih awal dari deadline resource."}, code="deadline_passed"
            )

        location = (reservation.accepted_snapshot or {}).get("location")
        if not location:
            raise ValueError(f"accepted_snapshot reservation {reservation.pk} tidak memiliki location.")

        try:
            with transaction.atomic():
                pickup = Pickup.objects.create(
                    reservation=reservation,
                    scheduled_at=scheduled_at,
                    location=dict(location),
                    instructions=serializer.validated_data["instructions"],
                )
        except IntegrityError:
            raise BusinessConflict("Reservation sudah memiliki pickup.", code="pickup_exists")

    return success(PickupSerializer(pickup).data, "Pickup dijadwalkan", status=201)


# GET /api/v1/pickups/
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def pickups(request):
    user = request.user
 
    query = PickupListQuerySerializer(data=request.query_params)
    query.is_valid(raise_exception=True)
    params = query.validated_data
 
    role = params.get("role")
    if role == "provider":
        queryset = Pickup.objects.filter(reservation__provider_id=user.pk)
    elif role == "receiver":
        queryset = Pickup.objects.filter(reservation__receiver_id=user.pk)
    else:
        queryset = Pickup.objects.filter(
            Q(reservation__provider_id=user.pk) | Q(reservation__receiver_id=user.pk)
        )
    if "status" in params:
        queryset = queryset.filter(status=params["status"])
    queryset = queryset.order_by("-scheduled_at", "-id")
 
    paginator = PageNumberPagination()
    paginator.page_size = params.get("page_size", 20)
    try:
        page = paginator.paginate_queryset(queryset, request)
    except NotFound:
        raise NotFound(ErrorDetail("Halaman tidak ditemukan.", code="page_not_found"))
 
    return success(
        {
            "count": paginator.page.paginator.count,
            "next": paginator.get_next_link(),
            "previous": paginator.get_previous_link(),
            "results": PickupSerializer(page, many=True).data,
        },
        "Daftar Pickup",
        status=200,
    )

# GET, PATCH /api/v1/pickups/<int:pk>/
@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def pickups_detail(request, pk):
    if pk <= 0:
        raise ValidationError(
            {"id": [ErrorDetail("ID harus bilangan bulat positif.", code="invalid")]}
        )
 
    user = request.user
 
    if request.method == "GET":
        pickup = Pickup.objects.select_related("reservation").filter(pk=pk).first()
        if pickup is None or not (
            user.is_admin or user.pk in (pickup.reservation.provider_id, pickup.reservation.receiver_id)
            ):
            raise NotFound()
 
        return success(PickupSerializer(pickup).data, "Data berhasil diambil", status=200)
 
    if request.method == "PATCH":
        with transaction.atomic():
            reservation_id = Pickup.objects.filter(pk=pk).values_list("reservation_id", flat=True).first()
            if reservation_id is None:
                raise NotFound()
            reservation = Reservation.objects.select_for_update(of=("self",)).filter(pk=reservation_id).first()
            pickup = Pickup.objects.select_for_update(of=("self",)).filter(pk=pk).first()
            if (
                reservation is None
                or pickup is None
                or not (user.is_admin or user.pk in (reservation.provider_id, reservation.receiver_id))
            ):
                raise NotFound()
            if not user.is_admin and user.pk != reservation.provider_id:
                raise PermissionDenied()
 
            serializer = PickupUpdateSerializer(data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
 
            if pickup.status != PickupStatus.SCHEDULED or reservation.status != ReservationStatus.ACCEPTED:
                raise BusinessConflict(
                    "Pickup harus SCHEDULED dan reservation harus berstatus ACCEPTED.", code="invalid_transition"
                )
            if timezone.now() >= reservation.resource_deadline:
                raise BusinessConflict("Deadline resource telah terlewati.", code="deadline_passed")
 
            data = serializer.validated_data
            if "instructions" in data and data["instructions"] != pickup.instructions:
                pickup.instructions = data["instructions"]
                pickup.version += 1
                pickup.updated_at = timezone.now()
                pickup.save(update_fields=["instructions", "version", "updated_at"])
        
        return success(PickupSerializer(pickup).data, "Instruksi pickup diperbarui", status=200)

# POST /api/v1/pickups/<int:pk>/complete/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pickups_detail_complete(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/pickups/<int:pk>/no-show/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pickups_detail_no_show(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET /api/v1/admin/pickups/
@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_pickups(request):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# GET, PATCH /api/v1/admin/pickups/<int:pk>/
@api_view(['GET', 'PATCH'])
@permission_classes([IsAdminUser])
def admin_pickups_detail(request, pk):
    if request.method == "GET":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
    if request.method == "PATCH":
        # TODO: implement this operation.
        return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/admin/reservations/<int:pk>/pickup/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_reservations_detail_pickup(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/admin/pickups/<int:pk>/complete/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_pickups_detail_complete(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)


# POST /api/v1/admin/pickups/<int:pk>/no-show/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_pickups_detail_no_show(request, pk):
    # TODO: implement this operation.
    return failure('Endpoint belum diimplementasikan.', [{'field': None, 'code': 'not_implemented', 'message': 'Business logic pending.'}], status=501)
