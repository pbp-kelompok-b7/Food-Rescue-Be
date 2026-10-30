import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as dt_timezone
from email.utils import format_datetime
from threading import Barrier
from unittest.mock import Mock, patch

import requests
from django.db import close_old_connections, connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from rest_framework.test import APIClient

from shared.enums import Mode, ResourceType, Suitability, Unit
from shared.models import LocationProviderState, LocationSearchCache
from shared.reference_data import CATEGORIES, CATEGORY_RESOURCE_TYPES
from shared.services import ATTRIBUTION, MAX_RESPONSE_BYTES


SEARCH = "/api/v1/locations/search/"
NOW = datetime(2026, 10, 29, 5, tzinfo=dt_timezone.utc)
PROVIDER_SETTINGS = dict(
    LOCATION_SEARCH_ENABLED=True, LOCATION_SEARCH_URL="https://nominatim.openstreetmap.org/search",
    LOCATION_SEARCH_USER_AGENT="FoodRescue/test (+https://example.com/food-rescue)",
    LOCATION_SEARCH_TIMEOUT=5.0, LOCATION_SEARCH_CACHE_TTL=86400, LOCATION_SEARCH_MIN_INTERVAL=1.0,
)
PLACE = {"display_name": "Universitas Indonesia, Depok, Jawa Barat, Indonesia",
         "lat": "-6.364", "lon": "106.828", "address": {"city": "Depok", "state": "Jawa Barat"}}


def provider_response(payload=None, *, status=200, headers=None, body=None):
    response = Mock()
    response.status_code = status
    response.headers = headers or {}
    response.iter_content.return_value = [body if body is not None else json.dumps(
        [PLACE] if payload is None else payload).encode()]
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    return response


class ReferenceDataTests(SimpleTestCase):
    def test_public_reference_data_matches_enum_and_resource_validation(self):
        response = APIClient().get("/api/v1/reference-data/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.data), {"success", "message", "data"})
        data = response.data["data"]
        self.assertEqual(set(data), {"categories", "units", "resource_types", "suitabilities", "modes"})
        self.assertEqual({item["code"]: item["resource_type"] for item in data["categories"]},
                         CATEGORY_RESOURCE_TYPES)
        for field, enum in (("resource_types", ResourceType), ("suitabilities", Suitability), ("modes", Mode)):
            self.assertEqual({item["code"] for item in data[field]}, set(enum.values))
            self.assertTrue(all(item["label"] for item in data[field]))
        self.assertEqual({item["code"] for item in data["units"]}, set(Unit.values))
        for unit in data["units"]:
            self.assertEqual(unit["allows_decimal"], unit["code"] == "kg")
            self.assertEqual(unit["max_decimal_places"], 3 if unit["code"] == "kg" else 0)
        from listings.serializers import CATEGORIES as listing_categories
        from requests_app.serializers import CATEGORY_RESOURCE_TYPES as request_categories
        self.assertIs(listing_categories, CATEGORIES)
        self.assertIs(request_categories, CATEGORY_RESOURCE_TYPES)

    def test_unknown_reference_query_is_rejected(self):
        response = APIClient().get("/api/v1/reference-data/?owner=1")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["errors"][0]["code"], "unknown_field")

    def test_reference_data_does_not_leak_personal_or_model_fields(self):
        response = APIClient().get("/api/v1/reference-data/")
        for field in ("email", "whatsapp_number", "password", "owner", "created_by"):
            self.assertNotIn(field, response.content.decode())


@override_settings(CONTRACT_THROTTLE_RATES={})
class LocationQueryTests(SimpleTestCase):
    def test_invalid_queries_do_not_reach_provider(self):
        cases = (
            "", "?q=ab", "?q=", "?q=%20%20%20", "?q=" + "x" * 201,
            "?q=Depok&area=", "?q=Depok&area=" + "x" * 101,
            "?q=Depok&limit=0", "?q=Depok&limit=11", "?q=Depok&limit=-1",
            "?q=Depok&limit=abc", "?q=Depok&limit=1.0", "?q=Depok&limit=",
            "?q=Depok&limit=%2B1", "?q=Depok&q=Jakarta", "?q=Depok&limit=1&limit=2",
            "?q=Depok&area=Depok&area=Bogor", "?q=Depok&unknown=yes",
        )
        with patch("shared.views.search_locations") as lookup:
            for query in cases:
                with self.subTest(query=query):
                    response = APIClient().get(SEARCH + query)
                    self.assertEqual(response.status_code, 400, response.content)
                    self.assertFalse(response.data["success"])
                    self.assertTrue(response.data["errors"])
                    self.assertIn("no-store", response["Cache-Control"])
            lookup.assert_not_called()

    def test_query_bounds_and_whitespace_normalization(self):
        with patch("shared.views.search_locations", return_value=[]) as lookup:
            for size in (1, 10):
                response = APIClient().get(SEARCH, {"q": "x" * 200, "area": "a" * 100, "limit": size})
                self.assertEqual(response.status_code, 200)
            response = APIClient().get(SEARCH, {"q": "  Kampus   UI ", "area": " Depok "})
            self.assertEqual(response.status_code, 200)
            lookup.assert_called_with(q="Kampus UI", area="Depok", limit=5)

    def test_writes_are_not_allowed(self):
        self.assertEqual(APIClient().post(SEARCH, {"q": "Depok"}, format="json").status_code, 405)


@override_settings(**PROVIDER_SETTINGS)
class LocationSearchTests(TestCase):
    @override_settings(CONTRACT_THROTTLE_RATES={"location": "2/min"})
    def test_shared_throttle_also_limits_cached_searches_without_extra_provider_calls(self):
        self.assertEqual(self.search().status_code, 200)
        self.assertEqual(self.search().status_code, 200)
        response = self.search()
        self.assert_error(response, 429, "throttled")
        self.assertGreater(int(response["Retry-After"]), 0)
        self.assertEqual(self.http.call_count, 1)

    def setUp(self):
        self.client = APIClient()
        clock = patch("shared.services.timezone.now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        http = patch("shared.services.requests.get", return_value=provider_response())
        self.http = http.start()
        self.addCleanup(http.stop)

    def search(self, q="Universitas Indonesia", **params):
        return self.client.get(SEARCH, {"q": q, **params})

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        self.assertFalse(response.data["success"])
        self.assertEqual(response.data["errors"][0]["code"], code)
        self.assertIn("no-store", response["Cache-Control"])

    def release_gate(self):
        LocationProviderState.objects.update(next_request_at=NOW - timedelta(seconds=1))

    def test_public_lookup_schema_provider_params_and_attribution(self):
        response = self.search(area="Depok")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(set(response.data["data"]), {"results"})
        self.assertEqual(response.data["data"]["results"], [{
            "label": PLACE["display_name"], "area": "Depok", "latitude": -6.364,
            "longitude": 106.828, "attribution": ATTRIBUTION,
        }])
        args, kwargs = self.http.call_args
        self.assertEqual(args, (PROVIDER_SETTINGS["LOCATION_SEARCH_URL"],))
        self.assertEqual(kwargs["params"], {"q": "Universitas Indonesia, Depok", "format": "jsonv2",
                         "addressdetails": 1, "limit": 5, "accept-language": "id,en"})
        self.assertEqual(kwargs["headers"]["User-Agent"], PROVIDER_SETTINGS["LOCATION_SEARCH_USER_AGENT"])
        self.assertEqual(kwargs["timeout"], 5)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])

    def test_empty_results_are_successful_and_cached(self):
        self.http.return_value = provider_response([])
        self.assertEqual(self.search().data["data"]["results"], [])
        self.assertEqual(self.search().status_code, 200)
        self.assertEqual(self.http.call_count, 1)

    def test_shared_cache_normalizes_case_and_whitespace_across_clients(self):
        first = self.search(area="Depok")
        second = APIClient().get(SEARCH, {"q": "  UNIVERSITAS   INDONESIA ", "area": "depok"})
        self.assertEqual(first.data, second.data)
        self.assertEqual(self.http.call_count, 1)
        cached = LocationSearchCache.objects.get()
        self.assertEqual(len(cached.key), 64)
        self.assertEqual(cached.expires_at, NOW + timedelta(days=1))

    def test_limit_area_and_provider_are_separate_cache_entries(self):
        self.search()
        for params in ({"limit": 1}, {"area": "Bogor"}):
            self.release_gate()
            self.assertEqual(self.search(**params).status_code, 200)
        with override_settings(LOCATION_SEARCH_URL="https://other.example/search"):
            self.assertEqual(self.search().status_code, 200)
        self.assertEqual(LocationSearchCache.objects.count(), 4)
        self.assertEqual(self.http.call_count, 4)

    def test_expired_cache_is_refreshed_and_pruned(self):
        self.search()
        LocationSearchCache.objects.update(expires_at=NOW)
        self.release_gate()
        self.assertEqual(self.search().status_code, 200)
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(LocationSearchCache.objects.count(), 1)

    def test_global_gate_blocks_other_clients_but_allows_cache_hits(self):
        self.assertEqual(self.search().status_code, 200)
        response = APIClient().get(SEARCH, {"q": "Bogor"}, REMOTE_ADDR="192.0.2.20")
        self.assert_error(response, 429, "location_rate_limited")
        self.assertEqual(response["Retry-After"], "1")
        self.assertEqual(self.search().status_code, 200)
        self.assertEqual(self.http.call_count, 1)

    def test_gate_allows_next_request_at_boundary(self):
        self.search()
        with patch("shared.services.timezone.now", return_value=NOW + timedelta(seconds=1)):
            self.assertEqual(self.search(q="Bogor").status_code, 200)

    def test_configuration_cannot_relax_public_provider_limit(self):
        with override_settings(LOCATION_SEARCH_MIN_INTERVAL=0):
            self.search()
            self.assert_error(self.search(q="Bogor"), 429, "location_rate_limited")

    def test_upstream_rate_limit_sets_global_cooldown_and_retry_after(self):
        self.http.return_value = provider_response(status=429, headers={"Retry-After": "37"})
        response = self.search()
        self.assert_error(response, 429, "location_rate_limited")
        self.assertEqual(response["Retry-After"], "37")
        self.assertEqual(LocationProviderState.objects.get().next_request_at, NOW + timedelta(seconds=37))
        self.assert_error(self.search(q="Bogor"), 429, "location_rate_limited")
        self.assertEqual(self.http.call_count, 1)
        self.assertFalse(LocationSearchCache.objects.exists())

    def test_upstream_retry_after_date_and_invalid_values(self):
        for raw, expected in ((format_datetime(NOW + timedelta(seconds=12), usegmt=True), 12),
                              (None, 60), ("invalid", 60), ("9" * 200, 60), ("0", 1)):
            with self.subTest(raw=raw):
                self.release_gate()
                self.http.return_value = provider_response(status=429, headers={"Retry-After": raw})
                response = self.search()
                self.assert_error(response, 429, "location_rate_limited")
                self.assertEqual(response["Retry-After"], str(expected))

    def test_unavailable_provider_and_timeout_preserve_rate_slot(self):
        for error in (requests.Timeout(), requests.ConnectionError()):
            with self.subTest(error=type(error).__name__):
                self.release_gate()
                self.http.side_effect = error
                self.assert_error(self.search(), 503, "location_unavailable")
                self.assert_error(self.search(q="Bogor"), 429, "location_rate_limited")
        self.assertFalse(LocationSearchCache.objects.exists())

    def test_upstream_status_mapping_and_private_errors(self):
        for status, expected, code in ((500, 503, "location_unavailable"), (503, 503, "location_unavailable"),
                                       (403, 502, "location_provider_error"), (302, 502, "location_provider_error")):
            with self.subTest(status=status):
                self.release_gate()
                self.http.return_value = provider_response(status=status, body=b"secret upstream debug")
                response = self.search()
                self.assert_error(response, expected, code)
                self.assertNotIn("secret upstream debug", response.content.decode())

    def test_slow_stream_cannot_hold_provider_gate_indefinitely(self):
        with patch("shared.services.monotonic", side_effect=[0, 6]):
            self.assert_error(self.search(), 503, "location_unavailable")
        self.assertFalse(LocationSearchCache.objects.exists())

    def test_malformed_or_oversized_json_is_provider_error(self):
        for body in (b"not json", b"{", b"\xff", b"x" * (MAX_RESPONSE_BYTES + 1)):
            with self.subTest(size=len(body)):
                self.release_gate()
                self.http.return_value = provider_response(body=body)
                self.assert_error(self.search(), 502, "location_provider_error")
        self.assertFalse(LocationSearchCache.objects.exists())

    def test_invalid_results_never_invent_coordinates_or_poison_cache(self):
        payloads = ({"error": "bad"}, [None], [{}], [{**PLACE, "lat": None}],
                    [{**PLACE, "lat": "NaN"}], [{**PLACE, "lon": "Infinity"}],
                    [{**PLACE, "lat": 91}], [{**PLACE, "lon": -181}], [{**PLACE, "lat": True}],
                    [{**PLACE, "display_name": ""}], [{**PLACE, "address": []}])
        for payload in payloads:
            with self.subTest(payload=payload):
                self.release_gate()
                self.http.return_value = provider_response(payload)
                self.assert_error(self.search(), 502, "location_provider_error")
        self.assertFalse(LocationSearchCache.objects.exists())

    def test_area_fallback_uses_provider_address_only(self):
        for address, expected in (({"town": "Town"}, "Town"), ({"county": "County"}, "County"), ({}, "")):
            self.release_gate()
            self.http.return_value = provider_response([{**PLACE, "address": address}])
            result = self.search(q=f"Search {expected or 'empty'}", area="Client area").data["data"]["results"][0]
            self.assertEqual(result["area"], expected)

    def test_result_count_is_bounded_by_requested_limit(self):
        self.http.return_value = provider_response([PLACE] * 20)
        self.assertEqual(len(self.search(limit=1).data["data"]["results"]), 1)

    def test_disabled_or_invalid_configuration_never_calls_upstream(self):
        for changes in ({"LOCATION_SEARCH_ENABLED": False}, {"LOCATION_SEARCH_URL": "http://example.com/search"},
                        {"LOCATION_SEARCH_URL": "https://[invalid/search"},
                        {"LOCATION_SEARCH_URL": "https://example.com:invalid/search"},
                        {"LOCATION_SEARCH_URL": "https://u:pass@example.com/search"},
                        {"LOCATION_SEARCH_USER_AGENT": ""}, {"LOCATION_SEARCH_TIMEOUT": 0},
                        {"LOCATION_SEARCH_CACHE_TTL": 0}):
            with self.subTest(changes=changes), override_settings(**changes):
                self.assert_error(self.search(), 503, "location_unavailable")
        self.http.assert_not_called()


@override_settings(**PROVIDER_SETTINGS)
@skipUnlessDBFeature("has_select_for_update")
class LocationConcurrencyTests(TransactionTestCase):
    def race(self, queries):
        barrier = Barrier(len(queries))

        def worker(q):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return APIClient().get(SEARCH, {"q": q}).status_code
            finally:
                connections.close_all()

        with patch("shared.services.timezone.now", return_value=NOW), patch(
            "shared.services.requests.get", side_effect=lambda *a, **kw: provider_response(),
        ) as http, ThreadPoolExecutor(max_workers=len(queries)) as pool:
            statuses = list(pool.map(worker, queries))
        self.assertEqual(http.call_count, 1)
        return statuses

    def test_concurrent_cache_misses_across_connections_send_one_provider_request(self):
        statuses = self.race([f"Public place {i}" for i in range(5)])
        self.assertEqual(sorted(statuses), [200, 429, 429, 429, 429])

    def test_concurrent_same_query_is_cached_once_for_all_clients(self):
        self.assertEqual(self.race(["Universitas Indonesia"] * 5), [200] * 5)
        self.assertEqual(LocationSearchCache.objects.count(), 1)
