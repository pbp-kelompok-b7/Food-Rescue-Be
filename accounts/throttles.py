from rest_framework.throttling import SimpleRateThrottle


class GoogleLoginThrottle(SimpleRateThrottle):
    scope = "google_login"
    rate = "10/min"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class LoginThrottle(SimpleRateThrottle):
    scope = "login"
    rate = "10/min"

    def get_cache_key(self, request, view):
        return self.cache_format % {
            "scope": self.scope,
            "ident": self.get_ident(request),
        }