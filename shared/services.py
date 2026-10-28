"""Manual geocoding with a persistent cache and a global provider gate.

No retry loop: each cache miss can make at most one upstream request. The
provider row stays locked during the bounded HTTP call, serializing workers.
"""

import hashlib
import json
import math
from datetime import timedelta, timezone as datetime_timezone
from email.utils import parsedate_to_datetime
from time import monotonic
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import APIException, Throttled

from .models import LocationProviderState, LocationSearchCache

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
