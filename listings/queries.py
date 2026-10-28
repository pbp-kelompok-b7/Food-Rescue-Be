from decimal import Decimal

from django.db.models import Case, CharField, DecimalField, F, Q, Sum, Value, When
from django.db.models.functions import Coalesce

from pickups.models import PickupStatus
from reservations.models import ReservationStatus
from .models import ListingStatus, ResourceListing


def listings_with_state(now):
    """Read effective quantities/status even before the expiry worker has run.

    Lifecycle writes and reservation/pickup cancellation belong to SF-BE-04.
    Completed quantities remain counted after the resource deadline.
    """
    quantity_field = DecimalField(max_digits=15, decimal_places=3)
    zero = Value(Decimal('0.000'), output_field=quantity_field)
    accepted = Q(reservations__status=ReservationStatus.ACCEPTED)
    completed = Q(reservations__pickup__status=PickupStatus.COMPLETED)
    allocated = (
        accepted
        & ~Q(status=ListingStatus.EXPIRED)
        & Q(available_until__gt=now, reservations__resource_deadline__gt=now)
        & (Q(reservations__pickup__isnull=True)
           | Q(reservations__pickup__status=PickupStatus.SCHEDULED))
    )

    return ResourceListing.objects.select_related('owner').annotate(
        quantity_completed=Coalesce(
            Sum('reservations__quantity', filter=accepted & completed),
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
