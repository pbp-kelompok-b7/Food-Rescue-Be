from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from google.auth.exceptions import TransportError
from rest_framework.test import APIClient

from .models import User


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
