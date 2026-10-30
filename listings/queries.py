from decimal import Decimal

from django.db.models import Case, CharField, DecimalField, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce

from pickups.models import PickupStatus
from reservations.models import ReservationStatus
from .models import ListingStatus, ResourceListing
from shared.services import allocated_filter, completed_filter


def listings_with_state(now):
    """Read effective quantities/status even before the expiry worker has run.

    Lifecycle writes and reservation/pickup cancellation belong to SF-BE-06.
    Completed quantities remain counted after the resource deadline.
    """
    quantity_field = DecimalField(max_digits=15, decimal_places=3)
    zero = Value(Decimal('0.000'), output_field=quantity_field)
    allocated = allocated_filter(now, "reservations__")
    completed = completed_filter("reservations__")

    return ResourceListing.objects.select_related('owner').annotate(
        quantity_completed=Coalesce(
            Sum('reservations__quantity', filter=completed),
            zero, output_field=quantity_field,
        ),
        quantity_allocated=Coalesce(
            Sum('reservations__quantity', filter=allocated),
            zero, output_field=quantity_field,
        ),
    ).annotate(
        quantity_available=F('quantity_total') - F('quantity_allocated') - F('quantity_completed'),
    ).annotate(
        effective_status=Case(
            # Releasing stock cannot reopen a terminal source.
            When(
                status__in=(ListingStatus.CLOSED, ListingStatus.EXPIRED, ListingStatus.COMPLETED),
                then=F('status'),
            ),
            When(quantity_completed__gte=F('quantity_total'), then=Value(ListingStatus.COMPLETED)),
            When(available_until__lte=now, then=Value(ListingStatus.EXPIRED)),
            When(quantity_available__lte=0, then=Value(ListingStatus.RESERVED)),
            default=Value(ListingStatus.AVAILABLE),
            output_field=CharField(),
        ),
    )
