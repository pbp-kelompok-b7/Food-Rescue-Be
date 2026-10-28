from types import SimpleNamespace
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient
from rest_framework.exceptions import ValidationError
from common.pagination import ContractPagination
from common.exceptions import exception_handler


class ContractScaffoldTests(SimpleTestCase):
    def setUp(self):
        self.client = APIClient()

    def test_private_and_admin_operations_are_protected(self):
        self.assertEqual(self.client.get("/api/v1/reservations/").status_code, 401)
        self.client.force_authenticate(SimpleNamespace(pk=2, is_authenticated=True, is_staff=False))
        self.assertEqual(self.client.get("/api/v1/admin/users/").status_code, 403)

    def test_anonymous_auth_writes_require_csrf(self):
        self.client = APIClient(enforce_csrf_checks=True)
        for operation in ("register", "login"):
            response = self.client.post(f"/api/v1/auth/{operation}/", {}, format="json")
            self.assertEqual(response.status_code, 403)

    def test_pagination_limits_and_validation_error_envelope(self):
        pagination = ContractPagination()
        self.assertEqual(pagination.get_page_size(SimpleNamespace(query_params={})), 20)
        self.assertEqual(pagination.get_page_size(SimpleNamespace(query_params={"page_size": "100"})), 100)
        for raw in ("0", "101", "abc"):
            with self.assertRaises(ValidationError):
                pagination.get_page_size(SimpleNamespace(query_params={"page_size": raw}))
        response = exception_handler(ValidationError({"quantity": ["Invalid quantity."]}), {})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["errors"][0]["field"], "quantity")


class PublicBrowseTests(TestCase):
    def test_request_browse_returns_empty_results(self):
        response = APIClient().get("/api/v1/requests/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["success"])
        self.assertEqual(response.data["data"]["results"], [])