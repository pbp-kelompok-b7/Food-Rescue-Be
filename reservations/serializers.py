from decimal import Decimal, ROUND_FLOOR

import json

from django.db.models import Sum
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.renderers import JSONRenderer

from common.exceptions import BusinessConflict
from listings.models import ListingStatus
from pickups.models import PickupStatus
from pickups.serializers import PickupSerializer
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


class StrictFieldsMixin:
    """Reject unknown / read-only fields with 400 (contract section 7.0).

    The contract states requests must not send server-owned or unknown fields;
    doing so is a validation error rather than silently ignored.
    """

    def to_internal_value(self, data):
        if isinstance(data, dict):
            allowed = set(self.fields)
            unknown = [key for key in data if key not in allowed]
            if unknown:
                raise serializers.ValidationError(
                    {key: ["Unknown or read-only field."] for key in sorted(unknown)}
                )
        return super().to_internal_value(data)


class UserRefSerializer(serializers.Serializer):
    """Minimal user reference used in reservation responses (section 7.4)."""

    id = serializers.IntegerField()
    display_name = serializers.CharField()


class LocationSerializer(serializers.Serializer):
    """Resource location per the contract (sections 7.2.2 / 7.4.2).

    ``label`` 1-300, ``area`` 1-100, ``latitude`` -90..90, ``longitude``
    -180..180. Validated as a nested object rather than a free-form blob.
    """

    label = serializers.CharField(max_length=300, min_length=1)
    area = serializers.CharField(max_length=100, min_length=1)
    latitude = serializers.FloatField(min_value=-90, max_value=90)
    longitude = serializers.FloatField(min_value=-180, max_value=180)


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
        # Reuse the pickups app serializer so the nested pickup shape stays in
        # one place (section 7.5).
        return PickupSerializer(pickup).data

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


def _contact_of(user):
    """Admin-visible contact for a party (section 7.4.10). Always includes the
    user id; whatsapp_number is null when not filled."""
    return {
        "user_id": user.id,
        "whatsapp_number": user.whatsapp_number or None,
        "whatsapp_url": None,
    }


class AdminReservationSerializer(ReservationSerializer):
    """Reservation representation for admin moderation (section 7.4.10).

    Adds both parties' contacts, available in every status for moderation. The
    inherited ``counterparty_contact`` stays viewer-relative (null for a
    non-participant admin); the admin-only fields below carry the contacts.
    """

    provider_contact = serializers.SerializerMethodField()
    receiver_contact = serializers.SerializerMethodField()

    def get_provider_contact(self, obj):
        return _contact_of(obj.provider)

    def get_receiver_contact(self, obj):
        return _contact_of(obj.receiver)


class ListingClaimCreateSerializer(StrictFieldsMixin, serializers.Serializer):
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


class RequestOfferCreateSerializer(StrictFieldsMixin, serializers.Serializer):
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
    location = LocationSerializer()
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


class ReservationReviseSerializer(StrictFieldsMixin, serializers.Serializer):
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
    reason = serializers.CharField(min_length=1, max_length=1000, required=False)

    def __init__(self, *args, reservation=None, user=None, is_admin=False, **kwargs):
        self.reservation = reservation
        self.user = user
        self.is_admin = is_admin
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

        # Admin moderation requires a reason for the audit trail.
        if self.is_admin and not attrs.get("reason"):
            raise serializers.ValidationError({"reason": "This field is required."})

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
        update_fields = ["quantity", "version", "updated_at"]
        reason = self.validated_data.get("reason")
        if reason:
            reservation.reason = reason
            update_fields.append("reason")
        reservation.save(update_fields=update_fields)
        return reservation


def _offer_terms_mismatches(reservation, req):
    """Re-validate an offer's stored terms against the (possibly edited)
    request at acceptance time (section 5.3 step 3). Returns a list of
    mismatching field names (empty when the offer is still compatible).
    """
    data = reservation.resource_data or {}
    suitability = data.get("suitability") or []
    mismatches = []
    if data.get("resource_type") != req.resource_type:
        mismatches.append("resource_type")
    if data.get("category") != req.category:
        mismatches.append("category")
    if reservation.unit != req.unit:
        mismatches.append("unit")
    if req.intended_use not in suitability:
        mismatches.append("suitability")
    if data.get("resource_type") == ResourceType.PACKAGED_FOOD_SURPLUS:
        if suitability != [Suitability.HUMAN_CONSUMPTION]:
            mismatches.append("suitability")
    elif data.get("resource_type") == ResourceType.COMPOSTABLE_FOOD_WASTE:
        if Suitability.HUMAN_CONSUMPTION in suitability:
            mismatches.append("suitability")
    if reservation.mode == Mode.DONATE and reservation.unit_price != 0:
        mismatches.append("unit_price")
    if reservation.mode == Mode.SELL and reservation.unit_price <= 0:
        mismatches.append("unit_price")
    if req.donate_only and reservation.mode == Mode.SELL:
        mismatches.append("mode")
    if (
        reservation.mode == Mode.SELL
        and req.max_unit_price is not None
        and reservation.unit_price > req.max_unit_price
    ):
        mismatches.append("unit_price")
    return sorted(set(mismatches))


class ReservationAcceptSerializer(StrictFieldsMixin, serializers.Serializer):
    """Accept a PENDING reservation, atomically (section 7.4.6 / 5.1-5.3).

    Claim approver = provider; offer approver = receiver. The caller must send
    ``expected_version`` (optimistic concurrency); a mismatch yields 409
    ``stale_version``. On success the reservation becomes ACCEPTED with an
    ``accepted_at`` timestamp and an immutable ``accepted_snapshot``, the
    quantity is allocated (counted because the reservation is now ACCEPTED), and
    the source status is recomputed — all in the caller's transaction.

    The source row must already be locked by the view (lock order: source then
    reservation) so the availability re-check and the allocation commit together.
    """

    expected_version = serializers.IntegerField()
    reason = serializers.CharField(min_length=1, max_length=1000, required=False)

    def __init__(self, *args, reservation=None, user=None, source_obj=None, is_admin=False, **kwargs):
        # NOTE: ``source`` is reserved by DRF's Field/Serializer machinery, so
        # the locked source row is stored as ``source_obj``.
        self.reservation = reservation
        self.user = user
        self.source_obj = source_obj  # the locked ResourceListing or ResourceRequest
        self.is_admin = is_admin
        super().__init__(*args, **kwargs)

    def validate(self, attrs):
        reservation = self.reservation
        user = self.user
        source = self.source_obj

        # Admin moderation requires a reason for the audit trail.
        if self.is_admin and not attrs.get("reason"):
            raise serializers.ValidationError({"reason": "This field is required."})

        # Only PENDING reservations can be accepted.
        if reservation.status != ReservationStatus.PENDING:
            raise BusinessConflict(
                "Only a pending reservation can be accepted.",
                code="invalid_transition",
            )

        # Authorization: claim is approved by the provider, offer by the
        # receiver. An admin moderator may accept on anyone's behalf (override
        # is authorization only — every other invariant below still applies).
        # A participant who is not the designated approver is unauthorized
        # (403 permission_denied), per the section 7.4.6 access contract.
        if not self.is_admin:
            if reservation.source_type == SourceType.LISTING_CLAIM:
                approver_id = reservation.provider_id
            else:
                approver_id = reservation.receiver_id
            if user.id != approver_id:
                raise PermissionDenied(
                    "You are not allowed to accept this reservation."
                )

        # Optimistic concurrency: the approver must have seen the latest version.
        if attrs["expected_version"] != reservation.version:
            raise BusinessConflict(
                f"Reservation has changed; current version is {reservation.version}.",
                code="stale_version",
            )

        now = timezone.now()
        if reservation.source_type == SourceType.LISTING_CLAIM:
            # Source must still be AVAILABLE and before its deadline.
            if source.status != ListingStatus.AVAILABLE:
                raise BusinessConflict(
                    "The listing is no longer accepting claims.",
                    code="source_not_accepting",
                )
            if source.available_until <= now:
                raise BusinessConflict(
                    "The listing deadline has passed.", code="deadline_passed"
                )
            total = source.quantity_total
        else:
            if source.status != RequestStatus.OPEN:
                raise BusinessConflict(
                    "The request is no longer accepting offers.",
                    code="source_not_accepting",
                )
            if source.valid_until <= now:
                raise BusinessConflict(
                    "The request validity period has passed.", code="deadline_passed"
                )
            if reservation.resource_deadline <= now:
                raise BusinessConflict(
                    "The offered resource deadline has passed.", code="deadline_passed"
                )
            total = source.quantity_needed
            # Re-check terms against the (possibly edited) request.
            mismatches = _offer_terms_mismatches(reservation, source)
            if mismatches:
                raise BusinessConflict(
                    f"Reservation terms no longer match the source: {', '.join(mismatches)}.",
                    code="terms_mismatch",
                )

        # Availability re-check excluding this reservation. Only ACCEPTED
        # reservations hold allocation; this one is still PENDING.
        allocated = source.reservations.filter(
            status=ReservationStatus.ACCEPTED
        ).aggregate(total=Sum("quantity"))["total"] or Decimal("0")
        available = total - allocated
        if reservation.quantity > available:
            raise BusinessConflict(
                f"Not enough quantity remains to accept this reservation ({available}).",
                code="insufficient_quantity",
            )

        attrs["_total"] = total
        attrs["_allocated_after"] = allocated + reservation.quantity
        attrs["_now"] = now
        # For a claim the resource deadline is owned by the listing, which may
        # have been edited since the claim was submitted. Re-sync it from the
        # locked listing so the frozen snapshot uses the deadline that was
        # actually re-checked above (section 5.3 / 7.4.6). An offer carries its
        # own provider-set deadline, so it is left unchanged.
        if reservation.source_type == SourceType.LISTING_CLAIM:
            attrs["_resource_deadline"] = source.available_until
        else:
            attrs["_resource_deadline"] = reservation.resource_deadline
        return attrs

    def save(self):
        reservation = self.reservation
        source = self.source_obj
        now = self.validated_data["_now"]

        # Align the reservation's resource deadline with the locked source
        # before snapshotting (claims follow the listing's current deadline).
        reservation.resource_deadline = self.validated_data["_resource_deadline"]

        # Capture the immutable snapshot of the agreed terms (same shape as the
        # response ``terms``). The submitted price is preserved as-is. The terms
        # dict holds a raw datetime for ``resource_deadline``; round-trip through
        # the JSON renderer so the snapshot is JSON-safe for the JSONField.
        terms = ReservationSerializer(reservation).data["terms"]
        snapshot = json.loads(JSONRenderer().render(terms).decode())

        reservation.status = ReservationStatus.ACCEPTED
        reservation.accepted_at = now
        reservation.accepted_snapshot = snapshot
        reservation.version = reservation.version + 1
        update_fields = [
            "status", "accepted_at", "accepted_snapshot", "resource_deadline",
            "version", "updated_at",
        ]
        reason = self.validated_data.get("reason")
        if reason:
            reservation.reason = reason
            update_fields.append("reason")
        reservation.save(update_fields=update_fields)

        # Recompute source status now that this reservation is ACCEPTED and
        # therefore counts toward allocation. Full allocation flips the status.
        fully_allocated = self.validated_data["_allocated_after"] >= self.validated_data["_total"]
        if reservation.source_type == SourceType.LISTING_CLAIM:
            if fully_allocated and source.status == ListingStatus.AVAILABLE:
                source.status = ListingStatus.RESERVED
                source.save(update_fields=["status", "updated_at"])
        else:
            if fully_allocated and source.status == RequestStatus.OPEN:
                source.status = RequestStatus.ALLOCATED
                source.save(update_fields=["status", "updated_at"])

        return reservation


class ReservationRejectSerializer(StrictFieldsMixin, serializers.Serializer):
    """Reject a PENDING reservation (section 7.4.7).

    The approver decides: a claim is rejected by the provider, an offer by the
    receiver. Requires ``reason`` (1-1000) and ``expected_version`` (mismatch →
    409 ``stale_version``). REJECTED is terminal and does not change quantity.
    """

    reason = serializers.CharField(min_length=1, max_length=1000)
    expected_version = serializers.IntegerField()

    def __init__(self, *args, reservation=None, user=None, is_admin=False, **kwargs):
        self.reservation = reservation
        self.user = user
        self.is_admin = is_admin
        super().__init__(*args, **kwargs)

    def validate(self, attrs):
        reservation = self.reservation
        user = self.user

        if reservation.status != ReservationStatus.PENDING:
            raise BusinessConflict(
                "Only a pending reservation can be rejected.",
                code="invalid_transition",
            )

        # Approver = provider for a claim, receiver for an offer. An admin
        # moderator may reject on anyone's behalf (authorization override only).
        # A participant who is not the approver is unauthorized (403
        # permission_denied), per the section 7.4.7 access contract.
        if not self.is_admin:
            if reservation.source_type == SourceType.LISTING_CLAIM:
                approver_id = reservation.provider_id
            else:
                approver_id = reservation.receiver_id
            if user.id != approver_id:
                raise PermissionDenied(
                    "You are not allowed to reject this reservation."
                )

        if attrs["expected_version"] != reservation.version:
            raise BusinessConflict(
                f"Reservation has changed; current version is {reservation.version}.",
                code="stale_version",
            )
        return attrs

    def save(self):
        reservation = self.reservation
        reservation.status = ReservationStatus.REJECTED
        reservation.reason = self.validated_data["reason"]
        reservation.version = reservation.version + 1
        reservation.save(update_fields=["status", "reason", "version", "updated_at"])
        return reservation


class ReservationCancelSerializer(StrictFieldsMixin, serializers.Serializer):
    """Cancel a reservation and release any allocation (section 7.4.8 / 5.4).

    Any participant (provider or receiver) or an admin may cancel while the
    reservation is PENDING, or ACCEPTED with a pickup that is not yet COMPLETED.
    ``reason`` is required. The transaction parties, price, and accepted
    snapshot are preserved.

    Cancelling an ACCEPTED reservation releases its allocation exactly once
    (achieved by flipping the status to CANCELLED, since allocation is computed
    as the sum of ACCEPTED quantities), recomputes the source status (reopening
    it only if the source is still active and before its deadline), and flips a
    SCHEDULED pickup to CANCELLED. A NO_SHOW pickup is left untouched.

    The source row must already be locked by the view (lock order: source then
    reservation).
    """

    reason = serializers.CharField(min_length=1, max_length=1000)

    def __init__(self, *args, reservation=None, user=None, source_obj=None, **kwargs):
        self.reservation = reservation
        self.user = user
        self.source_obj = source_obj
        super().__init__(*args, **kwargs)

    def validate(self, attrs):
        reservation = self.reservation
        user = self.user

        # Participant or admin only.
        is_participant = user.id in (reservation.provider_id, reservation.receiver_id)
        if not (is_participant or user.is_staff):
            # Non-participants should not learn the reservation exists; the view
            # hides it with 404, but guard here too.
            raise BusinessConflict(
                "You are not allowed to cancel this reservation.",
                code="invalid_transition",
            )

        if reservation.status not in (
            ReservationStatus.PENDING, ReservationStatus.ACCEPTED
        ):
            raise BusinessConflict(
                "This reservation can no longer be cancelled.",
                code="invalid_transition",
            )

        # An ACCEPTED reservation whose pickup already COMPLETED is a finished
        # transaction and cannot be cancelled.
        pickup = getattr(reservation, "pickup", None)
        if (
            reservation.status == ReservationStatus.ACCEPTED
            and pickup is not None
            and pickup.status == PickupStatus.COMPLETED
        ):
            raise BusinessConflict(
                "A completed pickup cannot be cancelled.", code="invalid_transition"
            )

        attrs["_pickup"] = pickup
        attrs["_was_accepted"] = reservation.status == ReservationStatus.ACCEPTED
        return attrs

    def save(self):
        reservation = self.reservation
        source = self.source_obj
        pickup = self.validated_data["_pickup"]
        was_accepted = self.validated_data["_was_accepted"]
        now = timezone.now()

        reservation.status = ReservationStatus.CANCELLED
        reservation.reason = self.validated_data["reason"]
        reservation.version = reservation.version + 1
        reservation.save(update_fields=["status", "reason", "version", "updated_at"])

        # Flip a SCHEDULED pickup to CANCELLED; leave NO_SHOW/COMPLETED as-is.
        if pickup is not None and pickup.status == PickupStatus.SCHEDULED:
            pickup.status = PickupStatus.CANCELLED
            pickup.cancelled_at = now
            pickup.save(update_fields=["status", "cancelled_at", "updated_at"])

        # Releasing allocation happens implicitly: the reservation is no longer
        # ACCEPTED, so it stops counting toward the source's allocation. Recompute
        # and reopen the source only if it is still active and before its deadline.
        if was_accepted:
            if reservation.source_type == SourceType.LISTING_CLAIM:
                if source.status == ListingStatus.RESERVED and source.available_until > now:
                    source.status = ListingStatus.AVAILABLE
                    source.save(update_fields=["status", "updated_at"])
            else:
                if source.status == RequestStatus.ALLOCATED and source.valid_until > now:
                    source.status = RequestStatus.OPEN
                    source.save(update_fields=["status", "updated_at"])

        return reservation
