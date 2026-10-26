from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from google.auth.exceptions import TransportError
from rest_framework.test import APIClient

from .models import User


class SessionAuthenticationContractTests(TestCase):
    """Drive Technical Design Revisi 2, sections 7.1.1 and 7.1.3-7.1.5.

    Login message follows the user's approved "Login berhasil" wording.
    Login throttling is also required by the SF-BE-01 acceptance criteria.
    Authentication and CSRF are real; no force_authenticate/force_login shortcuts.
    """

    user_fields = {
        "id", "email", "display_name", "whatsapp_number", "is_active",
        "is_admin", "created_at", "updated_at", "version",
    }
    password = "Password with spaces!234 "

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            "rina@example.com", password=cls.password, display_name="Rina",
            whatsapp_number="+628123456789",
        )

    def setUp(self):
        cache.clear()
        self.client = APIClient(enforce_csrf_checks=True)

    def csrf(self, client=None):
        client = client or self.client
        response = client.get("/api/v1/auth/csrf/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertIsInstance(response.json()["message"], str)
        self.assertEqual(set(response.json()["data"]), {"csrf_token"})
        token = response.json()["data"]["csrf_token"]
        self.assertIsInstance(token, str)
        self.assertTrue(token)
        self.assertTrue(client.cookies["csrftoken"].value)
        self.assertEqual(response["Cache-Control"], "no-store")
        return token

    def login(self, client=None, body=None):
        client = client or self.client
        token = self.csrf(client)
        if body is None:
            body = {"email": self.user.email, "password": self.password}
        return client.post(
            "/api/v1/auth/login/", body, format="json", HTTP_X_CSRFTOKEN=token,
        )

    def assert_error(self, response, status, code=None, field=None):
        self.assertEqual(response.status_code, status)
        self.assertEqual(response["Content-Type"].split(";")[0], "application/json")
        payload = response.json()
        self.assertFalse(payload["success"])
        self.assertIsInstance(payload["message"], str)
        self.assertTrue(payload["errors"])
        for error in payload["errors"]:
            self.assertEqual(set(error), {"field", "code", "message"})
        if code is not None:
            self.assertTrue(any(
                error["code"] == code and error["field"] == field
                for error in payload["errors"]
            ), payload)
        return payload

    def test_csrf_bootstrap_does_not_log_in_or_create_user(self):
        self.csrf()
        self.assertNotIn("sessionid", self.client.cookies)
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(
            self.client.get("/api/v1/auth/session/").json()["data"],
            {"authenticated": False, "user": None},
        )

    def test_login_returns_user_self_and_persists_session(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["message"], "Login berhasil")
        self.assertEqual(set(payload["data"]), self.user_fields)
        self.assertEqual(payload["data"]["id"], self.user.pk)
        self.assertEqual(payload["data"]["email"], self.user.email)
        self.assertEqual(payload["data"]["whatsapp_number"], self.user.whatsapp_number)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertTrue(self.client.cookies["sessionid"].value)
        self.assertTrue(self.client.cookies["sessionid"]["httponly"])
        session = self.client.get("/api/v1/auth/session/")
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session["Cache-Control"], "no-store")
        self.assertEqual(session.json()["data"], {"authenticated": True, "user": payload["data"]})

    def test_login_accepts_email_capitalization_and_preserves_password_spaces(self):
        response = self.login(body={"email": "RINA@EXAMPLE.COM", "password": self.password})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["id"], self.user.pk)

    def test_missing_fields_return_structured_400(self):
        for field in ("email", "password"):
            with self.subTest(field=field):
                body = {"email": self.user.email, "password": self.password}
                del body[field]
                self.assert_error(self.login(body=body), 400, "required", field)
        self.assertNotIn("sessionid", self.client.cookies)

    def test_invalid_fields_return_structured_400(self):
        cases = (
            ("email", "", "blank"),
            ("email", "not-an-email", "invalid"),
            ("password", "", "blank"),
            ("password", "x" * 129, "max_length"),
        )
        for field, value, code in cases:
            with self.subTest(field=field, code=code):
                body = {"email": self.user.email, "password": self.password, field: value}
                self.assert_error(self.login(body=body), 400, code, field)
        self.assertNotIn("sessionid", self.client.cookies)

    def test_login_rejects_unknown_fields(self):
        # Section 7.1.3: unknown/read-only request fields must be rejected.
        response = self.login(body={
            "email": self.user.email, "password": self.password, "is_admin": True,
        })
        payload = self.assert_error(response, 400)
        self.assertTrue(any(error["field"] == "is_admin" for error in payload["errors"]))
        self.assertNotIn("sessionid", self.client.cookies)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_admin)

    def test_login_rejects_non_object_json_without_creating_session(self):
        for body in ([], ["email", "password"], "not-an-object"):
            with self.subTest(body=body):
                response = self.client.post(
                    "/api/v1/auth/login/", body, format="json", HTTP_X_CSRFTOKEN=self.csrf(),
                )
                self.assert_error(response, 400)
        self.assertNotIn("sessionid", self.client.cookies)

    def test_wrong_password_missing_account_and_inactive_account_are_indistinguishable(self):
        wrong = self.assert_error(self.login(body={
            "email": self.user.email, "password": "incorrect-password",
        }), 401, "invalid_credentials")
        missing = self.assert_error(self.login(body={
            "email": "missing@example.com", "password": self.password,
        }), 401, "invalid_credentials")
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        inactive = self.assert_error(self.login(), 401, "invalid_credentials")
        self.assertEqual(wrong, missing)
        self.assertEqual(wrong, inactive)
        self.assertNotIn("sessionid", self.client.cookies)

    def test_google_only_account_cannot_use_password_login(self):
        user = User.objects.create_user(
            "google-only@gmail.com", display_name="Google user", google_sub="google-only-123",
        )
        self.assertFalse(user.has_usable_password())
        response = self.login(body={"email": user.email, "password": "anything"})
        self.assert_error(response, 401, "invalid_credentials")
        self.assertNotIn("sessionid", self.client.cookies)

    def test_missing_wrong_and_mismatched_csrf_cannot_log_in(self):
        other = APIClient(enforce_csrf_checks=True)
        other_token = self.csrf(other)
        self.csrf()
        for token in (None, "wrong-token", other_token):
            with self.subTest(token_kind="missing" if token is None else "invalid"):
                headers = {} if token is None else {"HTTP_X_CSRFTOKEN": token}
                response = self.client.post("/api/v1/auth/login/", {
                    "email": self.user.email, "password": self.password,
                }, format="json", **headers)
                self.assertEqual(response.status_code, 403)
        self.assertNotIn("sessionid", self.client.cookies)

    def test_public_auth_csrf_failures_use_contract_json(self):
        # csrf_protect rejects anonymous writes before the view's business logic.
        for endpoint in ("login", "register", "google"):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(f"/api/v1/auth/{endpoint}/", {}, format="json")
                self.assert_error(response, 403, "csrf_failed")

    def test_login_rotates_csrf_and_rejects_old_token_on_logout(self):
        old_token = self.csrf()
        old_cookie = self.client.cookies["csrftoken"].value
        response = self.client.post("/api/v1/auth/login/", {
            "email": self.user.email, "password": self.password,
        }, format="json", HTTP_X_CSRFTOKEN=old_token)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(self.client.cookies["csrftoken"].value, old_cookie)
        rejected = self.client.post("/api/v1/auth/logout/", {}, format="json", HTTP_X_CSRFTOKEN=old_token)
        self.assert_error(rejected, 403, "csrf_failed")
        self.assertTrue(self.client.get("/api/v1/auth/session/").json()["data"]["authenticated"])
        accepted = self.client.post("/api/v1/auth/logout/", {}, format="json", HTTP_X_CSRFTOKEN=self.csrf())
        self.assertEqual(accepted.status_code, 204)

    def test_expired_session_is_anonymous_and_cannot_logout(self):
        self.assertEqual(self.login().status_code, 200)
        session = self.client.session
        session.set_expiry(-1)
        session.save()
        response = self.client.get("/api/v1/auth/session/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], {"authenticated": False, "user": None})
        self.assert_error(self.client.post("/api/v1/auth/logout/", {}, format="json"), 401, "authentication_required")

    def test_deactivation_revokes_access_without_reactivating_user(self):
        self.assertEqual(self.login().status_code, 200)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.client.get("/api/v1/auth/session/").json()["data"], {"authenticated": False, "user": None})
        self.assert_error(self.client.post("/api/v1/auth/logout/", {}, format="json"), 401, "authentication_required")
        self.assert_error(self.login(), 401, "invalid_credentials")
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_logout_only_ends_current_browser_session_and_deletes_cookie(self):
        other = APIClient(enforce_csrf_checks=True)
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.login(other).status_code, 200)
        other_session_id = other.cookies["sessionid"].value
        response = self.client.post("/api/v1/auth/logout/", {}, format="json", HTTP_X_CSRFTOKEN=self.csrf())
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.cookies["sessionid"].value, "")
        self.assertEqual(int(response.cookies["sessionid"]["max-age"]), 0)
        self.assertEqual(self.client.get("/api/v1/auth/session/").json()["data"], {"authenticated": False, "user": None})
        self.assertTrue(other.get("/api/v1/auth/session/").json()["data"]["authenticated"])
        self.assertEqual(other.cookies["sessionid"].value, other_session_id)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_logout_rejects_unknown_body_fields_without_ending_session(self):
        self.assertEqual(self.login().status_code, 200)
        response = self.client.post(
            "/api/v1/auth/logout/", {"all_devices": True}, format="json", HTTP_X_CSRFTOKEN=self.csrf(),
        )
        self.assert_error(response, 400)
        self.assertTrue(self.client.get("/api/v1/auth/session/").json()["data"]["authenticated"])

    def test_logout_rejects_non_object_json_without_ending_session(self):
        self.assertEqual(self.login().status_code, 200)
        response = self.client.post(
            "/api/v1/auth/logout/", ["all_devices"], format="json", HTTP_X_CSRFTOKEN=self.csrf(),
        )
        self.assert_error(response, 400)
        self.assertTrue(self.client.get("/api/v1/auth/session/").json()["data"]["authenticated"])

    def test_anonymous_session_and_logout_contract(self):
        response = self.client.get("/api/v1/auth/session/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.json()["data"], {"authenticated": False, "user": None})
        self.assert_error(self.client.post("/api/v1/auth/logout/", {}, format="json"), 401, "authentication_required")

    def test_password_login_rate_limit_is_enforced(self):
        from .views import auth_login

        # Use the configured limit; the contract does not fix a numeric rate here.
        throttles = auth_login.cls().get_throttles()
        self.assertTrue(throttles, "SF-BE-01 requires a rate limit on password login.")
        limit = min(throttle.num_requests for throttle in throttles)
        for _ in range(limit):
            response = self.login(body={"email": self.user.email, "password": "wrong-password"})
            self.assertEqual(response.status_code, 401)
        self.assert_error(self.login(), 429, "throttled")
        self.assertNotIn("sessionid", self.client.cookies)

    @override_settings(
        CORS_ALLOWED_ORIGINS=["http://localhost:5173"],
        CSRF_TRUSTED_ORIGINS=["http://localhost:5173"],
    )
    def test_allowed_frontend_origin_can_login_with_cookie_credentials(self):
        token = self.csrf()
        response = self.client.post("/api/v1/auth/login/", {
            "email": self.user.email, "password": self.password,
        }, format="json", HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN="http://localhost:5173")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "http://localhost:5173")
        self.assertEqual(response["Access-Control-Allow-Credentials"], "true")

    @override_settings(CSRF_TRUSTED_ORIGINS=["http://localhost:5173"])
    def test_untrusted_origin_is_rejected_even_with_valid_csrf_token(self):
        token = self.csrf()
        response = self.client.post("/api/v1/auth/login/", {
            "email": self.user.email, "password": self.password,
        }, format="json", HTTP_X_CSRFTOKEN=token, HTTP_ORIGIN="https://untrusted.example")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", response)
        self.assertNotIn("sessionid", self.client.cookies)


@override_settings(GOOGLE_CLIENT_ID="test-client.apps.googleusercontent.com")
class GoogleSignInTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient(enforce_csrf_checks=True)
        self.claims = {
            "sub": "google-user-123", "email": "Someone@gmail.com",
            "email_verified": True, "name": "Someone",
        }
        self.verifier = patch("accounts.views.id_token.verify_oauth2_token", return_value=self.claims)
        self.verify = self.verifier.start()
        self.addCleanup(self.verifier.stop)

    def csrf(self, client=None):
        client = client or self.client
        response = client.get("/api/v1/auth/csrf/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertIn("csrftoken", client.cookies)
        return response.data["data"]["csrf_token"]

    def sign_in(self, client=None, body=None):
        client = client or self.client
        return client.post(
            "/api/v1/auth/google/", body if body is not None else {"credential": "google-id-token"},
            format="json", HTTP_X_CSRFTOKEN=self.csrf(client),
        )

    def test_first_sign_in_creates_account_session_and_rotates_csrf(self):
        token = self.csrf()
        old_cookie = self.client.cookies["csrftoken"].value
        response = self.client.post(
            "/api/v1/auth/google/", {"credential": "google-id-token"},
            format="json", HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["data"]["authenticated"])
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertNotIn("password", response.data["data"]["user"])
        self.assertNotIn("google_sub", response.data["data"]["user"])
        user = User.objects.get(google_sub=self.claims["sub"])
        self.assertEqual(user.email, "someone@gmail.com")
        self.assertFalse(user.has_usable_password())
        self.assertFalse(user.is_staff)
        self.assertIn("sessionid", self.client.cookies)
        self.assertNotEqual(self.client.cookies["csrftoken"].value, old_cookie)
        self.assertEqual(self.client.get("/api/v1/auth/session/").data["data"]["user"]["id"], user.pk)
        self.verify.assert_called_once()
        self.assertEqual(self.verify.call_args.kwargs["audience"], "test-client.apps.googleusercontent.com")

    def test_returning_user_uses_subject_even_if_google_email_changes(self):
        self.sign_in()
        user_id = User.objects.get().pk
        self.claims["email"] = "changed@gmail.com"
        response = self.sign_in()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["user"]["id"], user_id)
        self.assertEqual(User.objects.count(), 1)

    def test_missing_or_wrong_csrf_rejected_before_google_verification(self):
        for token in (None, "incorrect"):
            kwargs = {} if token is None else {"HTTP_X_CSRFTOKEN": token}
            response = self.client.post("/api/v1/auth/google/", {"credential": "token"}, format="json", **kwargs)
            self.assertEqual(response.status_code, 403)
        self.verify.assert_not_called()

    def test_invalid_credential_input(self):
        for body in ({}, {"credential": ""}, {"credential": "x" * 8193}):
            with self.subTest(body_length=len(str(body))):
                self.assertEqual(self.sign_in(body=body).status_code, 400)
        self.verify.assert_not_called()

    def test_unknown_google_fields_are_rejected_before_token_verification(self):
        response = self.sign_in(body={"credential": "google-id-token", "is_admin": True})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["errors"][0]["field"], "is_admin")
        self.assertEqual(response.json()["errors"][0]["code"], "unknown_field")
        self.verify.assert_not_called()
        self.assertFalse(User.objects.exists())
        self.assertNotIn("sessionid", self.client.cookies)

    def test_invalid_token_does_not_create_user_or_session(self):
        self.verify.side_effect = ValueError("Expired or invalid token")
        response = self.sign_in()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["errors"][0]["code"], "invalid_credentials")
        self.assertFalse(User.objects.exists())
        self.assertNotIn("sessionid", self.client.cookies)

    def test_unverified_email_or_invalid_subject_rejected(self):
        for claims in (
            {**self.claims, "email_verified": False},
            {**self.claims, "email_verified": "true"},
            {**self.claims, "sub": ""},
            {**self.claims, "sub": "x" * 256},
            {**self.claims, "email": "invalid-email"},
        ):
            with self.subTest(claims=claims):
                self.verify.return_value = claims
                self.assertEqual(self.sign_in().status_code, 401)
        self.assertFalse(User.objects.exists())

    def test_existing_email_is_not_automatically_linked(self):
        user = User.objects.create_user("someone@gmail.com", password="password", display_name="Existing")
        response = self.sign_in()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["errors"][0]["code"], "account_link_required")
        user.refresh_from_db()
        self.assertIsNone(user.google_sub)
        self.assertEqual(User.objects.count(), 1)
        self.assertNotIn("sessionid", self.client.cookies)

    def test_inactive_google_account_rejected(self):
        User.objects.create_user(
            "someone@gmail.com", google_sub=self.claims["sub"], display_name="Inactive", is_active=False,
        )
        response = self.sign_in()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.data["errors"][0]["code"], "invalid_credentials")
        self.assertNotIn("sessionid", self.client.cookies)

    def test_google_outage_returns_service_error(self):
        self.verify.side_effect = TransportError("Google unavailable")
        response = self.sign_in()
        self.assertEqual(response.status_code, 503)
        self.assertFalse(User.objects.exists())

    @override_settings(GOOGLE_CLIENT_ID="")
    def test_missing_client_id_does_not_skip_audience_validation(self):
        self.assertEqual(self.sign_in().status_code, 503)
        self.verify.assert_not_called()

    def test_rate_limit(self):
        self.verify.side_effect = ValueError("Invalid token")
        for _ in range(10):
            self.assertEqual(self.sign_in().status_code, 401)
        self.assertEqual(self.sign_in().status_code, 429)
        self.assertEqual(self.verify.call_count, 10)

    def test_password_login_limit_does_not_block_google_login(self):
        for _ in range(10):
            response = self.client.post(
                "/api/v1/auth/login/", {"email": "missing@example.com", "password": "wrong-password"},
                format="json", HTTP_X_CSRFTOKEN=self.csrf(),
            )
            self.assertEqual(response.status_code, 401)
        limited = self.client.post(
            "/api/v1/auth/login/", {"email": "missing@example.com", "password": "wrong-password"},
            format="json", HTTP_X_CSRFTOKEN=self.csrf(),
        )
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(self.sign_in().status_code, 200)

    def test_google_limit_is_separate_for_different_client_ips(self):
        self.verify.side_effect = ValueError("Invalid token")
        for _ in range(10):
            self.assertEqual(self.sign_in().status_code, 401)
        self.assertEqual(self.sign_in().status_code, 429)
        other = APIClient(enforce_csrf_checks=True, REMOTE_ADDR="192.0.2.1")
        self.assertEqual(self.sign_in(other).status_code, 401)

    def test_logout_requires_csrf_and_only_ends_current_session(self):
        self.sign_in()
        other_client = APIClient(enforce_csrf_checks=True)
        self.sign_in(other_client)
        self.assertEqual(self.client.post("/api/v1/auth/logout/").status_code, 403)
        response = self.client.post("/api/v1/auth/logout/", HTTP_X_CSRFTOKEN=self.csrf())
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertEqual(self.client.get("/api/v1/auth/session/").data["data"], {"authenticated": False, "user": None})
        self.assertTrue(other_client.get("/api/v1/auth/session/").data["data"]["authenticated"])

    def test_expired_and_inactive_sessions_are_anonymous(self):
        self.sign_in()
        session = self.client.session
        session.set_expiry(-1)
        session.save()
        self.assertFalse(self.client.get("/api/v1/auth/session/").data["data"]["authenticated"])
        self.sign_in()
        User.objects.update(is_active=False)
        self.assertFalse(self.client.get("/api/v1/auth/session/").data["data"]["authenticated"])

    def test_anonymous_session_and_logout(self):
        response = self.client.get("/api/v1/auth/session/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"], {"authenticated": False, "user": None})
        self.assertEqual(self.client.post("/api/v1/auth/logout/").status_code, 401)
