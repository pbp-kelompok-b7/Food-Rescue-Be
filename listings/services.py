"""Owner/admin listing writes. Always lock source -> reservations -> pickups.

Reservation approval/cancel/completion services must use the same lock order.
These operations preserve accepted terms and never edit accepted snapshots.
"""

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import NotFound, PermissionDenied

from common.exceptions import BusinessConflict
from common.serializers import StrictSerializer
from pickups.models import Pickup
from reservations.models import ReservationStatus
from shared.models import AuditEvent, record_audit, audit_state
from .models import ListingStatus, ResourceListing
from .queries import listings_with_state
from .serializers import ListingCloseSerializer, ListingSerializer


ACTIVE_STATUSES = (ListingStatus.AVAILABLE, ListingStatus.RESERVED)


def _lock_listing(pk, actor):
    listing = get_object_or_404(ResourceListing.objects.select_for_update(), pk=pk)
    now = timezone.now()
    state = listings_with_state(now).get(pk=listing.pk)
    if listing.owner_id != actor.pk and not actor.is_staff:
        public = (
            state.effective_status == ListingStatus.AVAILABLE
            and state.quantity_available > 0 and listing.available_until > now
            and listing.archived_at is None
        )
        participant = listing.reservations.filter(Q(provider=actor) | Q(receiver=actor)).exists()
        if public or participant:
            raise PermissionDenied()
        raise NotFound()
    return listing, state, now


def _lock_transactions(listing):
    reservations = list(listing.reservations.select_for_update().order_by('pk'))
    # No join here: PostgreSQL cannot lock the nullable side of an outer join.
    list(Pickup.objects.select_for_update().filter(
        reservation_id__in=[reservation.pk for reservation in reservations],
    ).order_by('pk'))
    return reservations


def _summary(listing):
    return {
        'status': listing.status, 'version': listing.version,
        'quantity_total': str(listing.quantity_total),
        'archived_at': listing.archived_at.isoformat() if listing.archived_at else None,
    }


def _audit(actor, listing_id, action, before, after, **extra):
    record_audit(
        actor=actor, entity_type='ResourceListing', entity_id=str(listing_id), action=action,
        summary={'before': before, 'after': after, **extra},
    )


def _cancel_pending(reservations, actor, reason, now):
    cancelled = []
    for reservation in reservations:
        if reservation.status != ReservationStatus.PENDING:
            continue
        reservation.status = ReservationStatus.CANCELLED
        reservation.cancellation_code = 'SOURCE_CLOSED'
        reservation.reason = reason
        reservation.version += 1
        reservation.updated_at = now
        reservation.save(update_fields=('status', 'cancellation_code', 'reason', 'version', 'updated_at'))
        record_audit(
            actor=actor, entity_type='Reservation', entity_id=str(reservation.pk),
            action='source_closed', summary={
                'listing_id': reservation.listing_id, 'before_status': ReservationStatus.PENDING,
                'after_status': ReservationStatus.CANCELLED,
                'cancellation_code': 'SOURCE_CLOSED', 'reason': reason,
            },
        )
        cancelled.append(reservation.pk)
    return cancelled


@transaction.atomic
def update_listing(pk, actor, data):
    listing, state, now = _lock_listing(pk, actor)
    reservations = _lock_transactions(listing)
    serializer = ListingSerializer(
        listing, data=data, partial=True, context={'has_reservations': bool(reservations)},
    )
    serializer.is_valid(raise_exception=True)
    total = serializer.validated_data.get('quantity_total', listing.quantity_total)
    if total < state.quantity_allocated + state.quantity_completed:
        raise BusinessConflict(
            {'quantity_total': 'Quantity tidak boleh di bawah jumlah allocated dan completed.'},
            code='quantity_floor',
        )
    before = _summary(listing)
    # Persist effective terminal state before any new deadline/quantity can reopen it.
    listing = serializer.save(status=state.effective_status, version=listing.version + 1)
    result = listings_with_state(now).get(pk=listing.pk)
    if listing.status != result.effective_status:
        listing.status = result.effective_status
        listing.save(update_fields=('status',))
    _audit(
        actor, listing.pk, 'update', before, _summary(listing),
        changed_fields=sorted(serializer.validated_data),
    )
    return result


@transaction.atomic
def close_listing(pk, actor, data):
    listing, state, now = _lock_listing(pk, actor)
    serializer = ListingCloseSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    reservations = _lock_transactions(listing)
    if state.effective_status not in ACTIVE_STATUSES or listing.archived_at is not None:
        raise BusinessConflict('Listing sudah ditutup, kedaluwarsa, atau selesai.')
    reason = serializer.validated_data['reason']
    before = _summary(listing)
    cancelled = _cancel_pending(reservations, actor, reason, now)
    listing.status = ListingStatus.CLOSED
    listing.version += 1
    listing.save(update_fields=('status', 'version', 'updated_at'))
    _audit(actor, listing.pk, 'close', before, _summary(listing), reason=reason, cancelled_reservation_ids=cancelled)
    return listings_with_state(now).get(pk=listing.pk)


@transaction.atomic
def delete_listing(pk, actor, data):
    listing, state, now = _lock_listing(pk, actor)
    StrictSerializer(data=data).is_valid(raise_exception=True)
    reservations = _lock_transactions(listing)
    before = _summary(listing)
    if not reservations:
        listing_id = listing.pk
        listing.delete()
        _audit(actor, listing_id, 'delete', before, None)
        return

    cancelled = _cancel_pending(reservations, actor, 'Listing diarsipkan.', now)
    status = ListingStatus.CLOSED if state.effective_status in ACTIVE_STATUSES else state.effective_status
    if listing.archived_at is not None and listing.status == status and not cancelled:
        return
    listing.archived_at = listing.archived_at or now
    listing.status = status
    listing.version += 1
    listing.save(update_fields=('archived_at', 'status', 'version', 'updated_at'))
    _audit(actor, listing.pk, 'archive', before, _summary(listing), cancelled_reservation_ids=cancelled)
