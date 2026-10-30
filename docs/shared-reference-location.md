# SF-BE-04: reference data and manual location search

`GET /api/v1/reference-data/` is public. Categories contain `code`, `label`, and
`resource_type`; units contain `code`, `label`, `allows_decimal`, and
`max_decimal_places`. Other enum arrays contain `{code, label}` objects. Codes
match the existing listing/request vocabulary; both serializers import the same
catalog in `shared/reference_data.py`.

`GET /api/v1/locations/search/?q=Universitas%20Indonesia&area=Depok&limit=5`
is public and returns `{success, message, data: {results: [...]}}`. Each result has
`label`, `area`, numeric `latitude`/`longitude`, and `attribution`. An empty list
is a successful result. A missing provider area stays an empty string; it is not
inferred from the client's filter. Query validation rejects unknown/repeated
parameters, blank values, q outside 3–200 characters, area outside 1–100, and a
limit outside 1–10 (default 5).

## Provider policy and frontend integration

The default provider is Photon (`https://photon.komoot.io/api/`), which supports
partial place names and typo-tolerant searches. Its GeoJSON is normalized to the
same response shape. Searches remain explicitly submitted; no background or
per-keystroke requests are made. Results are restricted to a regional bounding
box (94,-12,142,7), which also intersects nearby countries.

Photon demo terms: https://github.com/komoot/photon#demo-server . Reasonable
project usage is permitted, with no availability guarantee; use a hosted/private
provider for larger traffic. Custom Photon deployments should use an `/api/` URL.

`LOCATION_SEARCH_URL` can still point to Nominatim. For its public server, the
following policy is mandatory:
https://operations.osmfoundation.org/policies/nominatim/

- Search only when the user submits a search. No autocomplete, periodic lookup,
  bulk/systematic searches, or scraping.
- The public server permits at most **one upstream request per second across
  the entire application**, with an identifying User-Agent. This applies to all
  deployments using the same application, not one limit per frontend user.
- Display the returned attribution with results; link to
  https://www.openstreetmap.org/copyright. Do not submit personal/confidential data.
- URL and User-Agent are configurable so the operator can switch providers.
  Review the policy before deployment; larger traffic needs another provider.

Only the server calls the configured provider. `area` is appended to the free
text query; it is not a radius or distance filter. No provider redirects or
automatic retries are followed. Responses larger than 1 MiB or containing
invalid coordinates fail with 502; raw provider errors are never returned.

## Deployment

Run `python manage.py migrate` before serving the endpoints. Migration
`shared/0002_locationproviderstate_locationsearchcache` adds two independent
support tables; it does not modify business data.

Configuration (also in `.env.example`):

| Variable | Default |
| --- | --- |
| `LOCATION_SEARCH_ENABLED` | `True` (set False to disable outgoing geocoding) |
| `LOCATION_SEARCH_URL` | `https://photon.komoot.io/api/` |
| `LOCATION_SEARCH_USER_AGENT` | FoodRescue project identity with repository URL |
| `LOCATION_SEARCH_TIMEOUT` | 10 seconds for socket operations |
| `LOCATION_SEARCH_CACHE_TTL` | 86400 seconds |
| `LOCATION_SEARCH_MIN_INTERVAL` | 1 second, never allowed below 1 |

All workers/deployments using the public server must share the same PostgreSQL
database/provider gate. A per-host row lock serializes provider requests. Cache
keys hash provider URL, normalized query/area and limit; raw query text is not
stored or logged by this service. Cached results, including empty results, are
served without consuming a provider slot. Expired entries are pruned on the next
successful miss. The API response uses `Cache-Control: no-store` because search
results can describe user-selected locations; the server cache is separate.

Errors retain the standard error envelope:

- 400: query validation.
- 429 `location_rate_limited`, with `Retry-After`: global gate or provider limit.
  Upstream cooldown also applies to other queries and workers.
- 502 `location_provider_error`: invalid data/response or other provider failure.
- 503 `location_unavailable`: timeout, connection failure, provider 5xx, disabled
  service, or invalid configuration.

Run `python manage.py test shared` and the full suite on PostgreSQL. Provider
tests are mocked; `LocationConcurrencyTests` require PostgreSQL and exercise
separate connections. A live provider check is a separate manual lookup, not
part of the automated suite.

## Map selection

Supply, Request and Offer forms show a visible search button, address results,
and a map pin after selection. Supply and Request detail pages also show the
full label and map. Map tiles use an OpenStreetMap embed; loading the map does
not issue another geocoding request. External provider outages remain possible.
