"""Shared expiry/quantity rules and manual geocoding.

Reads calculate effective state without writes. Expiry locks source ->
reservations -> pickups. Geocoding uses a persistent cache and provider gate;
a cache miss makes at most one bounded upstream request, without retries.
"""

import hashlib
import json
import logging
import math
from datetime import timezone as dt_timezone
from datetime import timedelta, timezone as datetime_timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from time import monotonic
from urllib.parse import urlsplit
import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Case, CharField, F, Q, Sum, Value, When
from django.utils import timezone
from rest_framework.exceptions import APIException, Throttled

from listings.models import ResourceListing, ListingStatus
from pickups.models import Pickup, PickupStatus
from requests_app.models import ResourceRequest, RequestStatus
from reservations.models import Reservation, ReservationStatus, SourceType
from .models import LocationProviderState, LocationSearchCache, audit_state, record_audit


RESOURCE_EXPIRED = "RESOURCE_EXPIRED"
REQUEST_EXPIRED = "REQUEST_EXPIRED"
ZERO = Decimal("0.000")
_UNSET = object()
logger = logging.getLogger(__name__)


def utc_now(now=None):
    value = now if now is not None else timezone.now()
    if timezone.is_naive(value):
        raise ValueError("Expiry requires an aware timestamp.")
    return value.astimezone(dt_timezone.utc)


def allocated_filter(now, prefix=""):
    """Accepted, unfinished, unexpired resource; request validity is independent."""
    def q(**fields):
        return Q(**{prefix+key: value for key, value in fields.items()})
    unfinished = q(pickup__isnull=True) | q(pickup__status=PickupStatus.SCHEDULED)
    live_claim = (q(source_type=SourceType.LISTING_CLAIM, listing__available_until__gt=now)
                  & ~q(listing__status=ListingStatus.EXPIRED))
    live_offer = q(source_type=SourceType.REQUEST_OFFER)
    return (q(status=ReservationStatus.ACCEPTED, resource_deadline__gt=now)
            & unfinished & (live_claim | live_offer))


def completed_filter(prefix=""):
    return Q(**{prefix+"status": ReservationStatus.ACCEPTED,
                prefix+"pickup__status": PickupStatus.COMPLETED})


def expired_filter(now, prefix=""):
    def q(**fields):
        return Q(**{prefix+key: value for key, value in fields.items()})
    claim = q(source_type=SourceType.LISTING_CLAIM) & (
        q(listing__status=ListingStatus.EXPIRED) | q(listing__available_until__lte=now)
        | q(status=ReservationStatus.ACCEPTED, resource_deadline__lte=now))
    offer = q(source_type=SourceType.REQUEST_OFFER) & (
        q(resource_deadline__lte=now) | (q(status=ReservationStatus.PENDING)
        & (q(request__status=RequestStatus.EXPIRED) | q(request__valid_until__lte=now))))
    return (q(status__in=(ReservationStatus.PENDING, ReservationStatus.ACCEPTED))
            & ~q(pickup__status=PickupStatus.COMPLETED) & (claim | offer))


def with_effective_status(queryset, now=None, *, pickup=False):
    now = utc_now(now)
    expired = expired_filter(now, "reservation__" if pickup else "")
    if pickup:
        expired &= Q(status=PickupStatus.SCHEDULED)
    return queryset.annotate(effective_status=Case(
        When(expired, then=Value("CANCELLED")), default=F("status"), output_field=CharField()))


def source_quantities(source, now=None):
    now = utc_now(now)
    totals = source.reservations.aggregate(
        allocated=Sum("quantity", filter=allocated_filter(now)),
        completed=Sum("quantity", filter=completed_filter()),
    )
    allocated = totals["allocated"] or ZERO
    completed = totals["completed"] or ZERO
    total = source.quantity_total if isinstance(source, ResourceListing) else source.quantity_needed
    return {"allocated": allocated, "completed": completed, "remaining": total-allocated-completed}


def expiry_code(reservation, now=None, *, source=None, pickup=_UNSET):
    now = utc_now(now)
    if reservation.status not in (ReservationStatus.PENDING, ReservationStatus.ACCEPTED):
        return None
    pickup = getattr(reservation, "pickup", None) if pickup is _UNSET else pickup
    if pickup is not None and pickup.status == PickupStatus.COMPLETED:
        return None
    if reservation.source_type == SourceType.LISTING_CLAIM:
        source = source if source is not None else reservation.listing
        if source.status == ListingStatus.EXPIRED or source.available_until <= now:
            return RESOURCE_EXPIRED
        # Pending claims follow the latest listing deadline at acceptance;
        # accepted reservations keep their snapshotted resource deadline.
        if reservation.status == ReservationStatus.ACCEPTED and reservation.resource_deadline <= now:
            return RESOURCE_EXPIRED
    else:
        source = source if source is not None else reservation.request
        if reservation.resource_deadline <= now:
            return RESOURCE_EXPIRED
        if reservation.status == ReservationStatus.PENDING and (
                source.status == RequestStatus.EXPIRED or source.valid_until <= now):
            return REQUEST_EXPIRED
    return None


def expiry_reason(code):
    return "Resource deadline passed." if code == RESOURCE_EXPIRED else "Request validity period passed."


def effective_reservation_status(reservation, now=None):
    return ReservationStatus.CANCELLED if expiry_code(reservation, now) else reservation.status


def effective_source_status(source, now=None, *, totals=None):
    """Pure status calculation shared by writes and expiry reconciliation."""
    now = utc_now(now)
    listing = isinstance(source, ResourceListing)
    if totals is None:
        totals = source_quantities(source, now)
    terminal = ((ListingStatus.CLOSED, ListingStatus.EXPIRED, ListingStatus.COMPLETED) if listing
                else (RequestStatus.CLOSED, RequestStatus.EXPIRED, RequestStatus.FULFILLED))
    total = source.quantity_total if listing else source.quantity_needed
    deadline = source.available_until if listing else source.valid_until
    if source.status in terminal:
        return source.status
    if totals["completed"] >= total:
        status = ListingStatus.COMPLETED if listing else RequestStatus.FULFILLED
    elif deadline <= now:
        status = ListingStatus.EXPIRED if listing else RequestStatus.EXPIRED
    elif source.archived_at is not None:
        status = ListingStatus.CLOSED if listing else RequestStatus.CLOSED
    elif totals["remaining"] <= 0:
        status = ListingStatus.RESERVED if listing else RequestStatus.ALLOCATED
    else:
        status = ListingStatus.AVAILABLE if listing else RequestStatus.OPEN
    return status


def recalculate_source(source, now=None):
    """Reconcile a locked source from history; terminal states never reopen."""
    now = utc_now(now)
    totals = source_quantities(source, now)
    status = effective_source_status(source, now, totals=totals)
    if source.status == status:
        return totals, False
    source.status = status
    source.version += 1
    source.updated_at = now
    source.save(update_fields=("status", "version", "updated_at"))
    return totals, True


@transaction.atomic
def expire_source(kind, pk, *, now=None):
    now = utc_now(now)
    model = {"listing": ResourceListing, "request": ResourceRequest}[kind]
    source = model.objects.select_for_update(of=("self",)).filter(pk=pk).first()
    counts = {"sources_changed": 0, "reservations_cancelled": 0, "pickups_cancelled": 0,
              "overallocated_sources": 0}
    if source is None:
        return counts
    reservations = list(source.reservations.select_for_update(of=("self",)).order_by("pk"))
    pickups = {row.reservation_id: row for row in Pickup.objects.select_for_update(of=("self",)).filter(
        reservation_id__in=[row.pk for row in reservations]).order_by("pk")}
    for reservation in reservations:
        pickup = pickups.get(reservation.pk)
        code = expiry_code(reservation, now, source=source, pickup=pickup)
        if code is None:
            continue
        reason = expiry_reason(code)
        before = audit_state(reservation)
        reservation.status = ReservationStatus.CANCELLED
        reservation.reason = reason
        reservation.cancellation_code = code
        reservation.version += 1
        reservation.save(update_fields=("status", "reason", "cancellation_code", "version", "updated_at"))
        record_audit(actor=None, entity_type="Reservation", entity_id=reservation.pk, action="expire",
                     summary={"reason": reason, "before": before, "after": audit_state(reservation)})
        counts["reservations_cancelled"] += 1
        if pickup is not None and pickup.status == PickupStatus.SCHEDULED:
            before_pickup = audit_state(pickup)
            pickup.status = PickupStatus.CANCELLED
            pickup.cancelled_at = now
            pickup.version += 1
            pickup.save(update_fields=("status", "cancelled_at", "version", "updated_at"))
            record_audit(actor=None, entity_type="Pickup", entity_id=pickup.pk, action="expire",
                         summary={"reason": reason, "before": before_pickup, "after": audit_state(pickup)})
            counts["pickups_cancelled"] += 1
    before_source = audit_state(source)
    totals, changed = recalculate_source(source, now)
    if changed:
        record_audit(actor=None, entity_type=model.__name__, entity_id=source.pk,
                     action="expire" if source.status == "EXPIRED" else "reconcile",
                     summary={"reason": "Expiry/quantity reconciliation.",
                              "before": before_source, "after": audit_state(source)})
        counts["sources_changed"] = 1
    counts["overallocated_sources"] = int(totals["remaining"] < 0)
    return counts


def run_expiry(*, now=None, batch_size=200):
    now = utc_now(now)
    if batch_size < 1:
        raise ValueError("batch_size must be positive.")
    result = {"checked_at": now.isoformat(), "sources_scanned": 0, "sources_changed": 0,
              "reservations_cancelled": 0, "pickups_cancelled": 0, "overallocated_sources": 0}
    for kind, model in (("listing", ResourceListing), ("request", ResourceRequest)):
        for pk in model.objects.order_by("pk").values_list("pk", flat=True).iterator(chunk_size=batch_size):
            try:
                counts = expire_source(kind, pk, now=now)
            except Exception:
                logger.exception("Expiry failed for %s %s", kind, pk)
                raise
            result["sources_scanned"] += 1
            for key, value in counts.items():
                result[key] += value
    return result


ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"
MAX_RESPONSE_BYTES = 1024 * 1024


class LocationProviderError(APIException):
    status_code = 502
    default_detail = "Response penyedia lokasi tidak valid."
    default_code = "location_provider_error"


class LocationUnavailable(APIException):
    status_code = 503
    default_detail = "Layanan pencarian lokasi sedang tidak tersedia."
    default_code = "location_unavailable"


class LocationRateLimited(Throttled):
    default_detail = "Batas pencarian lokasi terlampaui. Coba lagi nanti."
    default_code = "location_rate_limited"


def _retry_after(raw):
    try:
        seconds = int(raw)
    except (ValueError, TypeError):
        try:
            until = parsedate_to_datetime(raw)
            if until.tzinfo is None:
                until = until.replace(tzinfo=datetime_timezone.utc)
            seconds = math.ceil((until - timezone.now()).total_seconds())
        except (ValueError, TypeError, OverflowError):
            seconds = 60
    # Ignore absurd/unparseable upstream values instead of overflowing a datetime.
    return max(1, seconds) if seconds <= 31536000 else 60


def _normalize_results(payload, limit):
    if not isinstance(payload, list):
        raise LocationProviderError()
    results = []
    for item in payload[:limit]:
        if not isinstance(item, dict) or not isinstance(item.get("display_name"), str):
            raise LocationProviderError()
        label = item["display_name"].strip()
        address = item.get("address", {})
        if not label or not isinstance(address, dict):
            raise LocationProviderError()
        try:
            if isinstance(item["lat"], bool) or isinstance(item["lon"], bool):
                raise ValueError
            latitude, longitude = float(item["lat"]), float(item["lon"])
            if not (math.isfinite(latitude) and math.isfinite(longitude)
                    and -90 <= latitude <= 90 and -180 <= longitude <= 180):
                raise ValueError
        except (KeyError, ValueError, TypeError, OverflowError) as exc:
            raise LocationProviderError() from exc
        area = ""
        for field in ("city", "town", "village", "municipality", "county", "state"):
            if isinstance(address.get(field), str) and address[field].strip():
                area = address[field].strip()
                break
        results.append({"label": label, "area": area, "latitude": latitude,
                        "longitude": longitude, "attribution": ATTRIBUTION})
    return results


def _fetch_locations(url, user_agent, query, limit, timeout):
    started = monotonic()
    try:
        with requests.get(
            url, params={"q": query, "format": "jsonv2", "addressdetails": 1,
                         "limit": limit, "accept-language": "id,en"},
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=timeout, allow_redirects=False, stream=True,
        ) as response:
            if response.status_code == 429:
                raise LocationRateLimited(wait=_retry_after(response.headers.get("Retry-After")))
            if response.status_code >= 500:
                raise LocationUnavailable()
            if response.status_code != 200:
                raise LocationProviderError()
            body = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                if monotonic() - started > timeout:
                    raise LocationUnavailable()
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise LocationProviderError()
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError) as exc:
                raise LocationProviderError() from exc
        return _normalize_results(payload, limit)
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise LocationUnavailable() from exc
    except requests.RequestException as exc:
        raise LocationProviderError() from exc


def search_locations(*, q, area="", limit=5):
    url = settings.LOCATION_SEARCH_URL
    try:
        parsed = urlsplit(url)
        parsed.port  # Validate configured ports before making an HTTP request.
    except ValueError as exc:
        raise LocationUnavailable() from exc
    if (not settings.LOCATION_SEARCH_ENABLED or parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not settings.LOCATION_SEARCH_USER_AGENT.strip()):
        raise LocationUnavailable()
    interval = max(1.0, settings.LOCATION_SEARCH_MIN_INTERVAL)
    timeout = settings.LOCATION_SEARCH_TIMEOUT
    ttl = settings.LOCATION_SEARCH_CACHE_TTL
    if not math.isfinite(interval) or not math.isfinite(timeout) or timeout <= 0 or ttl <= 0:
        raise LocationUnavailable()
    identity = json.dumps([url, q.casefold(), area.casefold(), limit], ensure_ascii=False)
    key = hashlib.sha256(identity.encode()).hexdigest()
    now = timezone.now()
    cached = LocationSearchCache.objects.filter(key=key, expires_at__gt=now).first()
    if cached is not None:
        return cached.results

    provider_key = hashlib.sha256(parsed.hostname.lower().encode()).hexdigest()
    error = None
    with transaction.atomic():
        LocationProviderState.objects.get_or_create(key=provider_key, defaults={"next_request_at": now})
        state = LocationProviderState.objects.select_for_update().get(key=provider_key)
        now = timezone.now()
        # A worker may have populated the same search while this one waited.
        cached = LocationSearchCache.objects.filter(key=key, expires_at__gt=now).first()
        if cached is not None:
            return cached.results
        if state.next_request_at > now:
            raise LocationRateLimited(wait=math.ceil((state.next_request_at - now).total_seconds()))
        state.next_request_at = now + timedelta(seconds=interval)
        query = f"{q}, {area}" if area else q
        try:
            results = _fetch_locations(url, settings.LOCATION_SEARCH_USER_AGENT, query, limit, timeout)
        except (LocationProviderError, LocationUnavailable, LocationRateLimited) as exc:
            error = exc
            if isinstance(exc, LocationRateLimited):
                state.next_request_at = max(state.next_request_at,
                                            timezone.now() + timedelta(seconds=exc.wait))
        else:
            LocationSearchCache.objects.filter(expires_at__lte=now).delete()
            LocationSearchCache.objects.update_or_create(
                key=key, defaults={"results": results,
                                   "expires_at": timezone.now() + timedelta(seconds=ttl)},
            )
        # Commit the consumed slot/cooldown even when the provider failed.
        state.save(update_fields=["next_request_at"])
    if error is not None:
        raise error
    return results
