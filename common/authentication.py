from rest_framework.authentication import SessionAuthentication


class SessionCookieAuthentication(SessionAuthentication):
    def authenticate_header(self, request):
        # Anonymous requests receive 401; permission and CSRF errors remain 403.
        return "Session"
