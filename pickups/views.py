from rest_framework.decorators import api_view, permission_classes
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError, ErrorDetail
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone
from common.exceptions import *
from common.responses import *
from reservations.models import Reservation, ReservationStatus, SourceType
from listings.models import ListingStatus, ResourceListing
from requests_app.models import RequestStatus, ResourceRequest
from shared.models import AuditEvent, record_audit, audit_state
from .models import Pickup, PickupStatus
from shared.services import recalculate_source, expiry_code, with_effective_status, utc_now
from .serializers import *


def _recalculate_source(reservation, source, now):
    """Use the same history/deadline calculation as the expiry command."""
    if source is not None:
        recalculate_source(source, now)


def _lock_source_then_reservation(reservation_id):
    ref = Reservation.objects.filter(pk=reservation_id).values(
        "source_type", "listing_id", "request_id"
    ).first()
    if ref is None:
        return None, None
    if ref["source_type"] == SourceType.LISTING_CLAIM:
        source = ResourceListing.objects.select_for_update(of=("self",)).filter(
            pk=ref["listing_id"]
        ).first()
    else:
        source = ResourceRequest.objects.select_for_update(of=("self",)).filter(
            pk=ref["request_id"]
        ).first()
    reservation = Reservation.objects.select_for_update(of=("self",)).filter(
        pk=reservation_id
    ).first()
    return reservation, source

def _record_admin_pickup_audit(*, actor, action, pickup, reason, before, after):
    """Record moderator changes in the same transaction as the domain mutation."""
    record_audit(
        actor=actor,
        entity_type="Pickup",
        entity_id=str(pickup.pk),
        action=action,
        summary={"reason": reason, "before": before, "after": after},
    )
     
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
        if expiry_code(reservation) is not None or scheduled_at >= reservation.resource_deadline:
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

        record_audit(actor=user, entity_type="Pickup", entity_id=pickup.pk, action="schedule",
                     summary={"before": None, "after": audit_state(pickup)})
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
    now = utc_now()
    queryset = with_effective_status(queryset, now, pickup=True)
    if "status" in params:
        queryset = queryset.filter(effective_status=params["status"])
    queryset = queryset.select_related("reservation__listing", "reservation__request").order_by("-scheduled_at", "-id")
 
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
            "results": PickupSerializer(page, many=True, context={"_expiry_now": now}).data,
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
        pickup = Pickup.objects.select_related("reservation__listing", "reservation__request").filter(pk=pk).first()
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
            if expiry_code(reservation) is not None:
                raise BusinessConflict("Deadline resource telah terlewati.", code="deadline_passed")
 
            data = serializer.validated_data
            if "instructions" in data and data["instructions"] != pickup.instructions:
                before = audit_state(pickup)
                pickup.instructions = data["instructions"]
                pickup.version += 1
                pickup.updated_at = timezone.now()
                pickup.save()
                record_audit(actor=user, entity_type="Pickup", entity_id=pickup.pk, action="update",
                             summary={"before": before, "after": audit_state(pickup),
                                      "changed_fields": ["instructions"]})
        
        return success(PickupSerializer(pickup).data, "Instruksi pickup diperbarui", status=200)

# POST /api/v1/pickups/<int:pk>/complete/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pickups_detail_complete(request, pk):
    if pk <= 0:
        raise ValidationError(
            {"id": [ErrorDetail("ID harus bilangan bulat positif.", code="invalid")]}
        )

    user = request.user
    with transaction.atomic():
        reservation_id = Pickup.objects.filter(pk=pk).values_list("reservation_id", flat=True).first()
        if reservation_id is None:
            raise NotFound()
        reservation, source = _lock_source_then_reservation(reservation_id)
        pickup = Pickup.objects.select_for_update(of=("self",)).filter(pk=pk).first()
        if (
            reservation is None
            or pickup is None
            or not (user.is_admin or user.pk in (reservation.provider_id, reservation.receiver_id))
        ):
            raise NotFound()
        if not user.is_admin and user.pk != reservation.receiver_id:
            raise PermissionDenied()
        if pickup.status != PickupStatus.SCHEDULED or reservation.status != ReservationStatus.ACCEPTED:
            raise BusinessConflict(
                "Pickup harus SCHEDULED dan reservation harus berstatus ACCEPTED.", code="invalid_transition"
        )
            
        serializer = PickupCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        now = timezone.now()
        if expiry_code(reservation, now) is not None:
            raise BusinessConflict("Deadline resource telah terlewati.", code="deadline_passed")
        
        before = audit_state(pickup)
        pickup.status = PickupStatus.COMPLETED
        pickup.completed_at = now
        pickup.version += 1
        pickup.updated_at = now
        pickup.save()
        _recalculate_source(reservation, source, now)
        record_audit(actor=user, entity_type="Pickup", entity_id=pickup.pk, action="complete",
                     summary={"before": before, "after": audit_state(pickup)})

    return success(PickupSerializer(pickup).data, "Pickup diselesaikan", status=200)


# POST /api/v1/pickups/<int:pk>/no-show/
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def pickups_detail_no_show(request, pk):
    if pk <= 0:
        raise ValidationError(
            {"id": [ErrorDetail("ID harus bilangan bulat positif.", code="invalid")]}
        )

    user = request.user
    with transaction.atomic():
        reservation_id = Pickup.objects.filter(pk=pk).values_list("reservation_id", flat=True).first()
        if reservation_id is None:
            raise NotFound()
        reservation, source = _lock_source_then_reservation(reservation_id)
        pickup = Pickup.objects.select_for_update(of=("self",)).filter(pk=pk).first()
        if (
            reservation is None
            or pickup is None
            or not (user.is_admin or user.pk in (reservation.provider_id, reservation.receiver_id))
        ):
            raise NotFound()
        if not user.is_admin and user.pk != reservation.provider_id:
            raise PermissionDenied()

        serializer = PickupNoShowSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        if pickup.status != PickupStatus.SCHEDULED or reservation.status != ReservationStatus.ACCEPTED:
            raise BusinessConflict(
                "Pickup harus SCHEDULED dan reservation harus berstatus ACCEPTED.", code="invalid_transition"
            )

        now = timezone.now()
        if now <= pickup.scheduled_at:
            raise BusinessConflict("No-show hanya dapat dicatat setelah jadwal pickup terlewati.", code="pickup_time_not_passed")
        if expiry_code(reservation, now) is not None:
            raise BusinessConflict("Deadline resource telah terlewati.", code="deadline_passed")

        before = audit_state(pickup)
        reservation_before = audit_state(reservation)
        pickup.status = PickupStatus.NO_SHOW
        pickup.no_show_at = now
        pickup.version += 1
        pickup.updated_at = now
        pickup.save()
        reservation.status = ReservationStatus.CANCELLED
        reservation.reason = serializer.validated_data["reason"]
        reservation.cancellation_code = "pickup_no_show"
        reservation.version += 1
        reservation.updated_at = now
        reservation.save()
        _recalculate_source(reservation, source, now)
        record_audit(actor=user, entity_type="Pickup", entity_id=pickup.pk, action="no_show",
                     summary={"reason": reservation.reason, "before": before, "after": audit_state(pickup)})
        record_audit(actor=user, entity_type="Reservation", entity_id=reservation.pk, action="cancel",
                     summary={"reason": reservation.reason, "before": reservation_before,
                              "after": audit_state(reservation)})

    return success(PickupSerializer(pickup).data, "Pickup ditandai no-show", status=200)


# GET /api/v1/admin/pickups/
@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_pickups(request):
    query = AdminPickupListQuerySerializer(data=request.query_params)
    query.is_valid(raise_exception=True)
    params = query.validated_data

    queryset = Pickup.objects.select_related("reservation__listing", "reservation__request").all()
    now = utc_now()
    queryset = with_effective_status(queryset, now, pickup=True)
    if "status" in params:
        queryset = queryset.filter(effective_status=params["status"])
    if "reservation_id" in params:
        queryset = queryset.filter(reservation_id=params["reservation_id"])
    queryset = queryset.select_related("reservation__listing", "reservation__request").order_by("-scheduled_at", "-id")

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
            "results": PickupSerializer(page, many=True, context={"_expiry_now": now}).data,
        },
        "Daftar Pickup",
        status=200,
    )


# GET, PATCH /api/v1/admin/pickups/<int:pk>/
@api_view(['GET', 'PATCH'])
@permission_classes([IsAdminUser])
def admin_pickups_detail(request, pk):
    if pk <= 0:
        raise ValidationError(
            {"id": [ErrorDetail("ID harus bilangan bulat positif.", code="invalid")]}
        )
 
    if request.method == "GET":
        pickup = Pickup.objects.select_related("reservation__listing", "reservation__request").filter(pk=pk).first()
        if pickup is None:
            raise NotFound()
 
        return success(PickupSerializer(pickup).data, "Data berhasil diambil", status=200)
 
    if request.method == "PATCH":
        with transaction.atomic():
            reservation_id = Pickup.objects.filter(pk=pk).values_list("reservation_id", flat=True).first()
            if reservation_id is None:
                raise NotFound()
            reservation = Reservation.objects.select_for_update(of=("self",)).filter(pk=reservation_id).first()
            pickup = Pickup.objects.select_for_update(of=("self",)).filter(pk=pk).first()
            if reservation is None or pickup is None:
                raise NotFound()

            serializer = AdminPickupUpdateSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            if pickup.status != PickupStatus.SCHEDULED or reservation.status != ReservationStatus.ACCEPTED:
                raise BusinessConflict(
                    "Pickup harus SCHEDULED dan reservation harus berstatus ACCEPTED.",
                    code="invalid_transition",
                )
            now = timezone.now()
            if expiry_code(reservation, now) is not None or pickup.scheduled_at >= reservation.resource_deadline:
                raise BusinessConflict("Deadline resource telah terlewati.", code="deadline_passed")

            data = serializer.validated_data
            before = {"instructions": pickup.instructions, "status": pickup.status}
            if "instructions" in data and data["instructions"] != pickup.instructions:
                pickup.instructions = data["instructions"]
                pickup.version += 1
                pickup.updated_at = now
                pickup.save(update_fields=["instructions", "version", "updated_at"])
            _record_admin_pickup_audit(
                actor=request.user,
                action="admin_instructions_updated",
                pickup=pickup,
                reason=data["reason"],
                before=before,
                after={"instructions": pickup.instructions, "status": pickup.status},
            )
        
        return success(PickupSerializer(pickup).data, "Instruksi pickup diperbarui", status=200)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_reservations_detail_pickup(request, pk):
    if pk <= 0:
        raise ValidationError(
            {"id": [ErrorDetail("ID harus bilangan bulat positif.", code="invalid")]}
        )

    with transaction.atomic():
        reservation = (
            Reservation.objects.select_for_update(of=("self",))
            .select_related("provider", "receiver")
            .filter(pk=pk)
            .first()
        )
        if reservation is None:
            raise NotFound()

        serializer = AdminPickupScheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        scheduled_at = serializer.validated_data["scheduled_at"]

        if reservation.status != ReservationStatus.ACCEPTED:
            raise BusinessConflict("Reservation harus berstatus ACCEPTED.", code="invalid_transition")
        if Pickup.objects.filter(reservation_id=reservation.pk).exists():
            raise BusinessConflict("Reservation sudah memiliki pickup.", code="pickup_exists")
        if not (reservation.provider.whatsapp_number and reservation.receiver.whatsapp_number):
            raise BusinessConflict(
                "Nomor WhatsApp provider dan receiver wajib diisi.", code="contact_required"
            )
        now = timezone.now()
        if expiry_code(reservation, now) is not None or scheduled_at >= reservation.resource_deadline:
            raise BusinessConflict(
                {"scheduled_at": "Jadwal harus lebih awal dari deadline resource."},
                code="deadline_passed",
            )

        location = (reservation.accepted_snapshot or {}).get("location")
        if not location:
            raise BusinessConflict(
                "Snapshot kesepakatan tidak memiliki lokasi pickup.", code="invalid_transition"
            )

        try:
            # Savepoint keeps the outer transaction usable if the UNIQUE reservation
            # constraint wins a concurrent scheduling race.
            with transaction.atomic():
                pickup = Pickup.objects.create(
                    reservation=reservation,
                    scheduled_at=scheduled_at,
                    location=dict(location),
                    instructions=serializer.validated_data["instructions"],
                )
        except IntegrityError:
            raise BusinessConflict("Reservation sudah memiliki pickup.", code="pickup_exists")

        _record_admin_pickup_audit(
            actor=request.user,
            action="admin_scheduled",
            pickup=pickup,
            reason=serializer.validated_data["reason"],
            before=None,
            after={"status": pickup.status, "reservation_id": reservation.pk,
                   "scheduled_at": pickup.scheduled_at.isoformat(), "location": pickup.location},
        )

    return success(PickupSerializer(pickup).data, "Pickup dijadwalkan oleh admin", status=201)


# POST /api/v1/admin/pickups/<int:pk>/complete/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_pickups_detail_complete(request, pk):
    if pk <= 0:
        raise ValidationError(
            {"id": [ErrorDetail("ID harus bilangan bulat positif.", code="invalid")]}
        )

    with transaction.atomic():
        reservation_id = Pickup.objects.filter(pk=pk).values_list("reservation_id", flat=True).first()
        if reservation_id is None:
            raise NotFound()
        reservation, source = _lock_source_then_reservation(reservation_id)
        pickup = Pickup.objects.select_for_update(of=("self",)).filter(pk=pk).first()
        if reservation is None or pickup is None:
            raise NotFound()

        serializer = AdminPickupCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if pickup.status != PickupStatus.SCHEDULED or reservation.status != ReservationStatus.ACCEPTED:
            raise BusinessConflict(
                "Pickup harus SCHEDULED dan reservation harus berstatus ACCEPTED.",
                code="invalid_transition",
            )

        now = timezone.now()
        if expiry_code(reservation, now) is not None:
            raise BusinessConflict("Deadline resource telah terlewati.", code="deadline_passed")

        before = {"status": pickup.status, "reservation_status": reservation.status}
        pickup.status = PickupStatus.COMPLETED
        pickup.completed_at = now
        pickup.version += 1
        pickup.updated_at = now
        pickup.save(update_fields=["status", "completed_at", "version", "updated_at"])
        _recalculate_source(reservation, source, now)
        _record_admin_pickup_audit(
            actor=request.user,
            action="admin_completed",
            pickup=pickup,
            reason=serializer.validated_data["reason"],
            before=before,
            after={"status": pickup.status, "reservation_status": reservation.status},
        )

    return success(PickupSerializer(pickup).data, "Pickup diselesaikan oleh admin", status=200)


# POST /api/v1/admin/pickups/<int:pk>/no-show/
@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_pickups_detail_no_show(request, pk):
    if pk <= 0:
        raise ValidationError(
            {"id": [ErrorDetail("ID harus bilangan bulat positif.", code="invalid")]}
        )

    with transaction.atomic():
        reservation_id = Pickup.objects.filter(pk=pk).values_list("reservation_id", flat=True).first()
        if reservation_id is None:
            raise NotFound()
        reservation, source = _lock_source_then_reservation(reservation_id)
        pickup = Pickup.objects.select_for_update(of=("self",)).filter(pk=pk).first()
        if reservation is None or pickup is None:
            raise NotFound()

        serializer = AdminPickupNoShowSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if pickup.status != PickupStatus.SCHEDULED or reservation.status != ReservationStatus.ACCEPTED:
            raise BusinessConflict(
                "Pickup harus SCHEDULED dan reservation harus berstatus ACCEPTED.",
                code="invalid_transition",
            )

        now = timezone.now()
        if now <= pickup.scheduled_at:
            raise BusinessConflict(
                "No-show hanya dapat dicatat setelah jadwal pickup terlewati.",
                code="pickup_time_not_passed",
            )
        if expiry_code(reservation, now) is not None:
            raise BusinessConflict("Deadline resource telah terlewati.", code="deadline_passed")

        reason = serializer.validated_data["reason"]
        before = {"pickup_status": pickup.status, "reservation_status": reservation.status}
        pickup.status = PickupStatus.NO_SHOW
        pickup.no_show_at = now
        pickup.version += 1
        pickup.updated_at = now
        pickup.save(update_fields=["status", "no_show_at", "version", "updated_at"])

        reservation.status = ReservationStatus.CANCELLED
        reservation.reason = reason
        reservation.cancellation_code = "pickup_no_show"
        reservation.version += 1
        reservation.updated_at = now
        reservation.save(update_fields=["status", "reason", "cancellation_code", "version", "updated_at"])
        _recalculate_source(reservation, source, now)
        _record_admin_pickup_audit(
            actor=request.user,
            action="admin_no_show",
            pickup=pickup,
            reason=reason,
            before=before,
            after={"pickup_status": pickup.status, "reservation_status": reservation.status,
                   "cancellation_code": reservation.cancellation_code},
        )

    return success(PickupSerializer(pickup).data, "Pickup ditandai no-show oleh admin", status=200)