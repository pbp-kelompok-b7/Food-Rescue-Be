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
# business rules and Technical Design Rev 2 section 5.1, PENDING claims do NOT
# allocate stock, so only ACCEPTED reservations count against availability.
ACTIVE_ALLOCATION_STATUSES = (ReservationStatus.ACCEPTED,)


def _decimal_str(value, places):
    quantum = Decimal(1).scaleb(-places)
    return str(Decimal(value).quantize(quantum))


class UserRefSerializer(serializers.Serializer):
    """Minimal user reference used in reservation responses (section 7.4)."""

    id = serializers.IntegerField()
    display_name = serializers.CharField()


class ReservationSerializer(serializers.Serializer):
    """Read representation of a reservation, matching Technical Design & API
    Contract Rev 2 section 7.4.

    Fields: {id, created_at, updated_at, version, source_type, listing_id,
    request_id, provider, receiver, submitted_by_id, status, terms,
    accepted_snapshot, accepted_at, reason, cancellation_code, pickup,
    counterparty_contact}.
    """

    id = serializers.IntegerField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    version = serializers.IntegerField()
    source_type = serializers.CharField()
    listing_id = serializers.IntegerField(allow_null=True)
    request_id = serializers.IntegerField(allow_null=True)
    provider = UserRefSerializer()
    receiver = UserRefSerializer()
    submitted_by_id = serializers.IntegerField()
    status = serializers.CharField()
    terms = serializers.SerializerMethodField()
    accepted_snapshot = serializers.JSONField()
    accepted_at = serializers.DateTimeField(allow_null=True)
    reason = serializers.SerializerMethodField()
    cancellation_code = serializers.SerializerMethodField()
    pickup = serializers.SerializerMethodField()
    counterparty_contact = serializers.SerializerMethodField()

    def get_terms(self, obj):
        # For a listing claim the resource detail is taken from the listing at
        # claim time; quantity/price come from the reservation. For an offer the
        # resource detail lives in ``resource_data``.
        if obj.source_type == SourceType.LISTING_CLAIM and obj.listing_id:
            listing = obj.listing
            terms = {
                "resource_type": listing.resource_type,
                "title": listing.title,
                "category": listing.category,
                "description": listing.description,
                "condition": listing.condition,
                "suitability": listing.suitability,
                "location": listing.location,
                "storage_notes": listing.storage_notes,
                "allergen_notes": listing.allergen_notes,
                "packaging_status": listing.packaging_status,
            }
        else:
            terms = dict(obj.resource_data or {})
        terms.update(
            {
                "quantity": _decimal_str(obj.quantity, 3),
                "unit": obj.unit,
                "mode": obj.mode,
                "unit_price": _decimal_str(obj.unit_price, 2),
                "total_price": _decimal_str(obj.quantity * obj.unit_price, 2),
                "resource_deadline": obj.resource_deadline,
            }
        )
        return terms

    def get_reason(self, obj):
        return obj.reason or None

    def get_cancellation_code(self, obj):
        return obj.cancellation_code or None

    def get_pickup(self, obj):
        # Pickup scheduling (section 7.5) is out of scope for this endpoint; a
        # freshly created PENDING claim never has a pickup.
        return None

    def get_counterparty_contact(self, obj):
        # Contact is only exposed to the transaction partner once ACCEPTED
        # (section 7.4.4). A PENDING claim exposes no contact.
        return None


class ListingClaimCreateSerializer(serializers.Serializer):
    """Validate and create a PENDING ``LISTING_CLAIM`` reservation
    (Technical Design & API Contract Rev 2 section 7.4.1).

    The client may only supply ``quantity``. All transaction-defining fields
    (provider, receiver, price, unit, mode, status, source) are derived
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
            raise BusinessConflict(
                "You cannot claim your own listing.", code="self_transaction"
            )

        # Source must be AVAILABLE and not past its deadline. The contract uses
        # a single ``source_not_accepting`` code for both conditions.
        if (
            listing.status != ListingStatus.AVAILABLE
            or listing.available_until <= timezone.now()
        ):
            raise BusinessConflict(
                "This listing is not accepting claims.", code="source_not_accepting"
            )

        # Requested quantity must not exceed the available quantity at create
        # time. PENDING claims do not allocate stock; only ACCEPTED do.
        allocated = listing.reservations.filter(
            status__in=ACTIVE_ALLOCATION_STATUSES
        ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        available = listing.quantity_total - allocated
        if attrs["quantity"] > available:
            raise BusinessConflict(
                f"Requested quantity exceeds the available quantity ({available}).",
                code="insufficient_quantity",
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
