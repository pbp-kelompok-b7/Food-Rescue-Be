from decimal import Decimal, ROUND_FLOOR

from django.db.models import Sum
from django.utils import timezone
from rest_framework import serializers

from common.exceptions import BusinessConflict
from listings.models import ListingStatus
from requests_app.models import RequestStatus
from shared.enums import Mode, ResourceType, Suitability, Unit
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
        # A reservation has at most one pickup (section 7.5). Render it when it
        # exists; otherwise null (e.g. a PENDING reservation never has one).
        pickup = getattr(obj, "pickup", None)
        if pickup is None:
            return None
        return PickupRefSerializer(pickup).data

    def get_counterparty_contact(self, obj):
        """Contact of the transaction partner, relative to the viewer.

        Only revealed to a participant once the reservation is ACCEPTED and the
        other party's WhatsApp number is filled (sections 7.4.4 / 10). The
        viewing user is passed through serializer context. A non-participant
        admin receives null here: this single field cannot carry both parties'
        contacts, so admin moderation uses the admin detail endpoint instead.
        """
        viewer = self.context.get("viewer")
        if viewer is None or obj.status != ReservationStatus.ACCEPTED:
            return None

        if viewer.id == obj.provider_id:
            other = obj.receiver
        elif viewer.id == obj.receiver_id:
            other = obj.provider
        else:
            # Non-participant (e.g. admin): no single "counterparty".
            return None

        if not other.whatsapp_number:
            return None
        return {
            "user_id": other.id,
            "whatsapp_number": other.whatsapp_number,
            "whatsapp_url": None,
        }


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


# Resource-detail keys stored verbatim in Reservation.resource_data for an
# offer. Quantity/unit/mode/unit_price/resource_deadline live in real columns.
OFFER_RESOURCE_KEYS = (
    "resource_type",
    "title",
    "category",
    "description",
    "condition",
    "suitability",
    "location",
    "storage_notes",
    "allergen_notes",
    "packaging_status",
)


class RequestOfferCreateSerializer(serializers.Serializer):
    """Validate and create a PENDING ``REQUEST_OFFER`` reservation
    (Technical Design & API Contract Rev 2 section 7.4.2).

    The provider supplies the resource detail, quantity, mode, unit_price, unit,
    location, and resource_deadline. Provider is the session user; receiver is
    the request owner. The offer does not require a public listing. A PENDING
    offer does not reduce the request's remaining quantity, and the price is set
    at submission time and cannot be revised.
    """

    resource_type = serializers.ChoiceField(choices=ResourceType.choices)
    title = serializers.CharField(max_length=200)
    category = serializers.CharField(max_length=120)
    description = serializers.CharField(max_length=5000)
    unit = serializers.ChoiceField(choices=Unit.choices)
    mode = serializers.ChoiceField(choices=Mode.choices)
    unit_price = serializers.DecimalField(max_digits=15, decimal_places=2, min_value=Decimal("0"))
    condition = serializers.CharField(max_length=1000)
    suitability = serializers.ListField(
        child=serializers.ChoiceField(choices=Suitability.choices), allow_empty=False
    )
    location = serializers.JSONField()
    storage_notes = serializers.CharField(max_length=2000, required=False, allow_null=True, default=None)
    allergen_notes = serializers.CharField(max_length=2000, required=False, allow_null=True, default=None)
    packaging_status = serializers.CharField(max_length=200, required=False, allow_null=True, default=None)
    quantity = serializers.DecimalField(max_digits=15, decimal_places=3)
    resource_deadline = serializers.DateTimeField()

    def __init__(self, *args, request_obj=None, user=None, **kwargs):
        self.request_obj = request_obj
        self.user = user
        super().__init__(*args, **kwargs)

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than zero.")
        unit = self.request_obj.unit
        if unit in INTEGER_UNITS and value != value.to_integral_value(rounding=ROUND_FLOOR):
            raise serializers.ValidationError(
                f"Quantity for unit '{unit}' must be a whole number."
            )
        return value

    def validate_resource_deadline(self, value):
        if value <= timezone.now():
            raise serializers.ValidationError("Resource deadline must be in the future.")
        return value

    def validate_suitability(self, value):
        # De-duplicate while preserving order.
        seen = list(dict.fromkeys(value))
        return seen

    def validate(self, attrs):
        req = self.request_obj
        user = self.user

        # Self-transaction ban: provider cannot offer to their own request.
        if req.owner_id == user.id:
            raise BusinessConflict(
                "You cannot offer to your own request.", code="self_transaction"
            )

        # Source must be OPEN and not past its valid_until.
        if req.status != RequestStatus.OPEN or req.valid_until <= timezone.now():
            raise BusinessConflict(
                "This request is not accepting offers.", code="source_not_accepting"
            )

        # Resource type/category/unit must match the request exactly.
        mismatches = []
        if attrs["resource_type"] != req.resource_type:
            mismatches.append("resource_type")
        if attrs["category"] != req.category:
            mismatches.append("category")
        if attrs["unit"] != req.unit:
            mismatches.append("unit")

        # Suitability must contain the request's intended_use.
        if req.intended_use not in attrs["suitability"]:
            mismatches.append("suitability")

        # Consumption vs waste suitability rule (section 7.2 schema): packaged
        # food surplus is human-consumption only; compostable waste must not be
        # human-consumption.
        suitability = attrs["suitability"]
        if attrs["resource_type"] == ResourceType.PACKAGED_FOOD_SURPLUS:
            if suitability != [Suitability.HUMAN_CONSUMPTION]:
                mismatches.append("suitability")
        elif attrs["resource_type"] == ResourceType.COMPOSTABLE_FOOD_WASTE:
            if Suitability.HUMAN_CONSUMPTION in suitability:
                mismatches.append("suitability")

        # Price/mode rules: DONATE must be zero-priced, SELL must be positive;
        # donate_only requests forbid SELL; SELL price cannot exceed max_unit_price.
        if attrs["mode"] == Mode.DONATE and attrs["unit_price"] != 0:
            mismatches.append("unit_price")
        if attrs["mode"] == Mode.SELL and attrs["unit_price"] <= 0:
            mismatches.append("unit_price")
        if req.donate_only and attrs["mode"] == Mode.SELL:
            mismatches.append("mode")
        if (
            attrs["mode"] == Mode.SELL
            and req.max_unit_price is not None
            and attrs["unit_price"] > req.max_unit_price
        ):
            mismatches.append("unit_price")

        if mismatches:
            raise BusinessConflict(
                f"Offer terms do not match the request: {', '.join(sorted(set(mismatches)))}.",
                code="terms_mismatch",
            )

        # Requested quantity must not exceed the remaining need at create time.
        # PENDING offers do not allocate; only ACCEPTED reservations do.
        allocated = req.reservations.filter(
            status__in=ACTIVE_ALLOCATION_STATUSES
        ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        remaining = req.quantity_needed - allocated
        if attrs["quantity"] > remaining:
            raise BusinessConflict(
                f"Offer quantity exceeds the remaining need ({remaining}).",
                code="insufficient_quantity",
            )

        return attrs

    def create(self, validated_data):
        req = self.request_obj
        user = self.user
        resource_data = {key: validated_data[key] for key in OFFER_RESOURCE_KEYS}
        return Reservation.objects.create(
            source_type=SourceType.REQUEST_OFFER,
            listing=None,
            request=req,
            provider=user,
            receiver=req.owner,
            submitted_by=user,
            quantity=validated_data["quantity"],
            unit=validated_data["unit"],
            mode=validated_data["mode"],
            unit_price=validated_data["unit_price"],
            resource_data=resource_data,
            resource_deadline=validated_data["resource_deadline"],
            status=ReservationStatus.PENDING,
        )


class ReservationReviseSerializer(serializers.Serializer):
    """Revise the quantity of a PENDING reservation (section 7.4.5).

    Only the submitter may revise, and only ``quantity`` is mutable. Price,
    mode, unit, source, and the transaction parties are immutable. The caller
    must send ``expected_version`` (optimistic concurrency); a mismatch yields
    409 ``stale_version``. Each successful revision bumps ``version``. A PENDING
    reservation holds no allocation, so availability is only re-checked, never
    reallocated.
    """

    quantity = serializers.DecimalField(max_digits=15, decimal_places=3, required=False)
    expected_version = serializers.IntegerField()

    def __init__(self, *args, reservation=None, user=None, **kwargs):
        self.reservation = reservation
        self.user = user
        super().__init__(*args, **kwargs)

    def validate_quantity(self, value):
        if value <= 0:
            raise serializers.ValidationError("Quantity must be greater than zero.")
        unit = self.reservation.unit
        if unit in INTEGER_UNITS and value != value.to_integral_value(rounding=ROUND_FLOOR):
            raise serializers.ValidationError(
                f"Quantity for unit '{unit}' must be a whole number."
            )
        return value

    def validate(self, attrs):
        reservation = self.reservation

        # Only PENDING submissions can be revised.
        if reservation.status != ReservationStatus.PENDING:
            raise BusinessConflict(
                "Only a pending reservation can be revised.", code="invalid_transition"
            )

        # Optimistic concurrency: the client must have seen the latest version.
        if attrs["expected_version"] != reservation.version:
            raise BusinessConflict(
                f"Reservation has changed; current version is {reservation.version}.",
                code="stale_version",
            )

        if "quantity" not in attrs:
            raise serializers.ValidationError(
                {"quantity": "At least one revisable field is required."}
            )

        # Re-check availability/remaining against the source. PENDING holds no
        # allocation, so only ACCEPTED reservations count.
        new_quantity = attrs["quantity"]
        if reservation.source_type == SourceType.LISTING_CLAIM:
            source = reservation.listing
            total = source.quantity_total
            label = "available"
        else:
            source = reservation.request
            total = source.quantity_needed
            label = "remaining"
        allocated = source.reservations.filter(
            status__in=ACTIVE_ALLOCATION_STATUSES
        ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        headroom = total - allocated
        if new_quantity > headroom:
            raise BusinessConflict(
                f"Revised quantity exceeds the {label} quantity ({headroom}).",
                code="insufficient_quantity",
            )

        return attrs

    def save(self):
        reservation = self.reservation
        reservation.quantity = self.validated_data["quantity"]
        reservation.version = reservation.version + 1
        reservation.save(update_fields=["quantity", "version", "updated_at"])
        return reservation
