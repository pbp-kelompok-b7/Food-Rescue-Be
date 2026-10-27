"""Request close/archive operations, not participant cancellation or an expiry worker.

All lifecycle writers must lock source -> reservations in PK order. Future
MR-BE-05/SF-BE-06 integration must preserve this order and terminal source states.
Audit summaries use the existing AuditEvent storage, without copying contacts or
resource snapshots. Adapt this local writer to SF-BE-05's helper when available.
"""

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from common.exceptions import BusinessConflict
from common.responses import success
from reservations.models import ReservationStatus
from shared.models import AuditEvent
from .models import ResourceRequest, RequestStatus
from .queries import public_requests, requests_with_quantities
from .serializers import RequestCloseSerializer, RequestDeleteSerializer, ResourceRequestSerializer
from .writes import active_user


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
