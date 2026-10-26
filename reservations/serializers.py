from decimal import Decimal, ROUND_FLOOR

from django.db.models import Sum
from django.utils import timezone
from rest_framework import serializers

from common.exceptions import BusinessConflict
from listings.models import ListingStatus
from shared.enums import Unit
from .models import Reservation, ReservationStatus, SourceType


# Units that may only be claimed in whole-number quantities. ``kg`` allows
# decimals (up to the model's 3 decimal places); the rest are discrete items.
INTEGER_UNITS = {Unit.BOX, Unit.PCS, Unit.PAKET}

# Reservation statuses that hold a claim against listing availability. Per the
# business rules, PENDING claims do NOT allocate stock, so only ACCEPTED
# reservations count against the latest available quantity.
ACTIVE_ALLOCATION_STATUSES = (ReservationStatus.ACCEPTED,)


class ReservationSerializer(serializers.ModelSerializer):
    """Read representation of a reservation.

    NOTE: The exact field contract from "Technical Design & API Contract Food
    Rescue 2026 - Revisi 2" was not available at implementation time. This shape
    is intentionally close to the model and should be reconciled with the
    frozen contract when it becomes available (field names/nesting may change).
    """

    total_price = serializers.SerializerMethodField()

    class Meta:
        model = Reservation
        fields = [
            "id",
            "source_type",
            "listing",
            "request",
            "provider",
            "receiver",
            "submitted_by",
            "quantity",
            "unit",
            "mode",
            "unit_price",
            "total_price",
            "resource_deadline",
            "status",
            "reason",
            "created_at",
            "updated_at",
            "version",
        ]
        read_only_fields = fields

    def get_total_price(self, obj):
        return str((obj.quantity * obj.unit_price).quantize(Decimal("0.01")))


class ListingClaimCreateSerializer(serializers.Serializer):
    """Validate and create a PENDING ``LISTING_CLAIM`` reservation.

    The client may only supply ``quantity``. All transaction-defining fields
    (owner/provider, receiver, price, unit, mode, status, source) are derived
    server-side from the listing and the authenticated session user so a client
    cannot forge price/owner/status.
    """

    quantity = serializers.DecimalField(max_digits=15, decimal_places=3)

    def __init__(self, *args, listing=None, user=None, **kwargs):
        self.listing = listing
        self.user = user
        super().__init__(*args, **kwargs)

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than zero.")
        unit = self.listing.unit
        if unit in INTEGER_UNITS and value != value.to_integral_value(rounding=ROUND_FLOOR):
            raise serializers.ValidationError(
                f"Quantity for unit '{unit}' must be a whole number."
            )
        return value

    def validate(self, attrs):
        listing = self.listing
        user = self.user

        # Self-transaction ban: the claimant cannot be the listing owner.
        if listing.owner_id == user.id:
            raise BusinessConflict("You cannot claim your own listing.")

        # Source must be AVAILABLE and not past its deadline.
        if listing.status != ListingStatus.AVAILABLE:
            raise BusinessConflict("This listing is not available for claims.")
        if listing.available_until <= timezone.now():
            raise BusinessConflict("This listing has passed its deadline.")

        # Requested quantity must not exceed the latest available quantity.
        # PENDING claims do not allocate stock; only ACCEPTED reservations do.
        allocated = listing.reservations.filter(
            status__in=ACTIVE_ALLOCATION_STATUSES
        ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        available = listing.quantity_total - allocated
        if attrs["quantity"] > available:
            raise BusinessConflict(
                f"Requested quantity exceeds the available quantity ({available})."
            )

        return attrs

    def create(self, validated_data):
        listing = self.listing
        user = self.user
        return Reservation.objects.create(
            source_type=SourceType.LISTING_CLAIM,
            listing=listing,
            request=None,
            provider=listing.owner,
            receiver=user,
            submitted_by=user,
            quantity=validated_data["quantity"],
            # Unit/mode/unit_price follow the listing at claim time; the client
            # cannot override these.
            unit=listing.unit,
            mode=listing.mode,
            unit_price=listing.unit_price,
            resource_deadline=listing.available_until,
            status=ReservationStatus.PENDING,
        )
