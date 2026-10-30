from common.authentication import SharedRateThrottle


class GoogleLoginThrottle(SharedRateThrottle):
    scope = "google_login"


class LoginThrottle(SharedRateThrottle):
    scope = "login"
