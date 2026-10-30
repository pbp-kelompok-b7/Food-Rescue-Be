from rest_framework.authentication import SessionAuthentication


class SessionCookieAuthentication(SessionAuthentication):
    def authenticate_header(self, request):
        # Anonymous requests receive 401; permission and CSRF errors remain 403.
        return "Session"


class PrivateResponseMiddleware:
    """Prevent browser/shared caches from retaining user-specific API data."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.utils.cache import patch_cache_control, patch_vary_headers
        response = self.get_response(request)
        if request.path.startswith("/api/v1/"):
            if response.status_code == 404 and not response.get("Content-Type", "").startswith("application/json"):
                from django.http import JsonResponse
                response = JsonResponse({"success": False, "message": "Not found.",
                    "errors": [{"field": None, "code": "not_found", "message": "Not found."}]}, status=404)
            patch_vary_headers(response, ("Cookie",))
            if (getattr(request.user, "is_authenticated", False)
                    or request.method not in ("GET", "HEAD", "OPTIONS")
                    or response.status_code >= 400
                    or request.GET.get("scope") == "mine"
                    or request.path.startswith(("/api/v1/auth/", "/api/v1/users/",
                                               "/api/v1/admin/", "/api/v1/reservations/",
                                               "/api/v1/pickups/"))):
                if "no-store" not in response.get("Cache-Control", ""):
                    patch_cache_control(response, private=True, no_store=True)
        return response


from rest_framework.throttling import SimpleRateThrottle


class SharedRateThrottle(SimpleRateThrottle):
    """Database-backed sliding window serialized across PostgreSQL workers."""
    scope = None
    default_rates = {"login": "10/min", "google_login": "10/min", "location": "30/min",
                     "submission": "30/min", "admin_write": "30/min"}

    def get_rate(self):
        from django.conf import settings
        return getattr(settings, "CONTRACT_THROTTLE_RATES", self.default_rates).get(self.scope)

    def allow_request(self, request, view):
        from datetime import datetime, timedelta, timezone
        import hashlib
        from django.db import transaction
        from shared.models import ThrottleBucket
        if not self.rate:
            return True
        if self.scope in ("login", "google_login"):
            SessionAuthentication().enforce_csrf(request)
        if self.scope in ("login", "google_login") or not request.user.is_authenticated:
            identity = self.get_ident(request)
        else:
            identity = f"user:{request.user.pk}"
        key = hashlib.sha256(f"{self.scope}:{identity}".encode()).hexdigest()
        self.now = self.timer()
        with transaction.atomic():
            ThrottleBucket.objects.get_or_create(key=key, defaults={
                "expires_at": datetime.fromtimestamp(self.now, timezone.utc) + timedelta(seconds=self.duration),
            })
            bucket = ThrottleBucket.objects.select_for_update().get(key=key)
            self.history = [stamp for stamp in bucket.history if stamp > self.now - self.duration]
            if len(self.history) >= self.num_requests:
                return False
            self.history.insert(0, self.now)
            bucket.history = self.history
            bucket.expires_at = datetime.fromtimestamp(self.now, timezone.utc) + timedelta(seconds=self.duration)
            bucket.save(update_fields=("history", "expires_at"))
        return True


class ContractThrottle(SharedRateThrottle):
    def allow_request(self, request, view):
        path = request.path
        if path == "/api/v1/locations/search/":
            self.scope = "location"
        elif request.method == "POST" and path.endswith(("/claims/", "/offers/")):
            self.scope = "submission"
        elif request.method not in ("GET", "HEAD", "OPTIONS") and path.startswith("/api/v1/admin/"):
            self.scope = "admin_write"
        else:
            return True
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return super().allow_request(request, view)
