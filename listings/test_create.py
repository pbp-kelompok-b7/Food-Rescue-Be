"""POST /listings/ contract (Technical Design Revisi 2 section 7.2.2)."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import ResourceListing


class ListingCreateTests(TestCase):
    url = '/api/v1/listings/'
    now = datetime(2026, 10, 27, 5, tzinfo=timezone.utc)

    @classmethod
    def setUpTestData(cls):
        cls.owner = get_user_model().objects.create_user(
            email='create-owner@example.com', display_name='Provider',
            whatsapp_number='+628123456789',
        )

    def setUp(self):
        timer = patch('django.utils.timezone.now', return_value=self.now)
        timer.start()
        self.addCleanup(timer.stop)
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(self.owner)
        self.token = self.client.get('/api/v1/auth/csrf/').json()['data']['csrf_token']
        self.body = {
            'resource_type': 'PACKAGED_FOOD_SURPLUS', 'title': 'Rice box',
            'category': 'prepared_meal', 'description': 'Nasi ayam 20 box',
            'quantity_total': '20', 'unit': 'box', 'mode': 'DONATE',
            'unit_price': '0.00', 'condition': 'Baru dikemas',
            'suitability': ['HUMAN_CONSUMPTION'],
            'location': {'label': 'Kampus UI', 'area': 'Depok', 'latitude': -6.36, 'longitude': 106.82},
            'available_until': (self.now + timedelta(days=1)).isoformat(),
        }

    def post(self, body=None):
        return self.client.post(
            self.url, self.body if body is None else body,
            format='json', HTTP_X_CSRFTOKEN=self.token,
        )

    def assert_invalid(self, body, field=None):
        before = ResourceListing.objects.count()
        response = self.post(body)
        self.assertEqual(response.status_code, 400, response.content)
        payload = response.json()
        self.assertIs(payload['success'], False)
        self.assertTrue(payload['errors'])
        if field:
            self.assertTrue(any(
                error['field'] == field or (error['field'] or '').startswith(field + '.')
                for error in payload['errors']
            ), payload)
        self.assertEqual(ResourceListing.objects.count(), before)
        return payload

    def test_create_sets_session_owner_defaults_and_public_response(self):
        response = self.post()
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.json()
        self.assertIs(payload['success'], True)
        row = payload['data']
        listing = ResourceListing.objects.get(pk=row['id'])
        self.assertEqual(listing.owner_id, self.owner.pk)
        self.assertEqual(listing.created_by_id, self.owner.pk)
        self.assertEqual(row['owner'], {'id': self.owner.pk, 'display_name': 'Provider'})
        self.assertEqual(row['status'], 'AVAILABLE')
        self.assertEqual(row['quantity_total'], '20.000')
        self.assertEqual(row['quantity_available'], '20.000')
        self.assertEqual(row['quantity_allocated'], '0.000')
        self.assertEqual(row['quantity_completed'], '0.000')
        self.assertEqual(row['unit_price'], '0.00')
        self.assertEqual(row['version'], 1)
        for field in ('storage_notes', 'allergen_notes', 'packaging_status', 'archived_at'):
            self.assertIsNone(row[field])
        for private in ('created_by', 'whatsapp_number', self.owner.email, self.owner.whatsapp_number):
            self.assertNotIn(private, str(row))
        browse = self.client.get(self.url).json()['data']['results']
        self.assertEqual(browse, [row])

    def test_rejects_unknown_and_every_server_owned_field(self):
        for field in (
            'owner', 'owner_id', 'created_by', 'created_by_id', 'status', 'id',
            'created_at', 'updated_at', 'version', 'archived_at', 'effective_status',
            'quantity_available', 'quantity_allocated', 'quantity_completed', 'unknown',
        ):
            with self.subTest(field=field):
                errors = self.assert_invalid({**self.body, field: 999}, field)['errors']
                self.assertTrue(any(error['code'] == 'unknown_field' for error in errors))

    def test_required_fields_cannot_be_omitted(self):
        for field in self.body:
            body = deepcopy(self.body)
            del body[field]
            with self.subTest(field=field):
                self.assert_invalid(body, field)

    def test_blank_or_null_required_fields_are_invalid(self):
        for field in self.body:
            for value in ('', None):
                with self.subTest(field=field, value=value):
                    self.assert_invalid({**self.body, field: value}, field)

    def test_field_length_limits(self):
        for field, limit in (('title', 200), ('description', 5000), ('condition', 1000), ('storage_notes', 2000), ('allergen_notes', 2000), ('packaging_status', 200)):
            with self.subTest(field=field):
                self.assert_invalid({**self.body, field: 'a' * (limit + 1)}, field)

    def test_invalid_enums_and_unknown_category(self):
        for field in ('resource_type', 'category', 'unit', 'mode'):
            with self.subTest(field=field):
                self.assert_invalid({**self.body, field: 'UNKNOWN'}, field)

    def test_category_must_match_resource_type(self):
        self.assert_invalid({**self.body, 'category': 'coffee_grounds'}, 'category')
        self.assert_invalid({**self.body, 'resource_type': 'COMPOSTABLE_FOOD_WASTE', 'suitability': ['COMPOSTING']}, 'category')

    def test_suitability_rules(self):
        for values in ([], ['UNKNOWN'], ['COMPOSTING'], ['HUMAN_CONSUMPTION', 'COMPOSTING'], ['HUMAN_CONSUMPTION', 'HUMAN_CONSUMPTION']):
            with self.subTest(values=values):
                self.assert_invalid({**self.body, 'suitability': values})
        waste = {**self.body, 'resource_type': 'COMPOSTABLE_FOOD_WASTE', 'category': 'coffee_grounds'}
        self.assert_invalid(waste, 'suitability')
        self.assert_invalid({**waste, 'suitability': ['COMPOSTING', 'COMPOSTING']}, 'suitability')

    def test_waste_can_have_multiple_nonconsumption_uses_and_decimal_kg(self):
        body = {
            **self.body, 'resource_type': 'COMPOSTABLE_FOOD_WASTE', 'category': 'coffee_grounds',
            'suitability': ['COMPOSTING', 'BSF'], 'unit': 'kg', 'quantity_total': '1.001',
            'mode': 'SELL', 'unit_price': '1500.25',
        }
        response = self.post(body)
        self.assertEqual(response.status_code, 201, response.content)
        row = response.json()['data']
        self.assertEqual(row['quantity_total'], '1.001')
        self.assertEqual(row['quantity_available'], '1.001')
        self.assertEqual(row['unit_price'], '1500.25')

    def test_quantity_bounds_precision_and_string_transport(self):
        for value in ('0', '-1', '0.0001', '1000000000.000', 'NaN', 'Infinity', 'abc', 20, True):
            with self.subTest(value=value):
                self.assert_invalid({**self.body, 'quantity_total': value}, 'quantity_total')

    def test_non_kg_units_require_whole_quantities(self):
        for unit in ('box', 'pcs', 'paket'):
            with self.subTest(unit=unit):
                self.assert_invalid({**self.body, 'unit': unit, 'quantity_total': '1.500'}, 'quantity_total')

    def test_price_mode_precision_and_string_transport(self):
        for mode, price in (('DONATE', '1.00'), ('SELL', '0.00'), ('SELL', '-1.00'), ('SELL', '1.001'), ('SELL', 'NaN'), ('SELL', 'Infinity'), ('SELL', 1500)):
            with self.subTest(mode=mode, price=price):
                self.assert_invalid({**self.body, 'mode': mode, 'unit_price': price}, 'unit_price')

    def test_deadline_requires_future_rfc3339_with_timezone(self):
        for value in (self.now.isoformat(), (self.now - timedelta(seconds=1)).isoformat(), '2026-10-28T12:00:00', '2026-10-28', 'invalid'):
            with self.subTest(value=value):
                self.assert_invalid({**self.body, 'available_until': value}, 'available_until')

    def test_deadline_accepts_utc_or_explicit_offset(self):
        for value in ('2026-10-28T05:00:00Z', '2026-10-28T12:00:00+07:00'):
            with self.subTest(value=value):
                response = self.post({**self.body, 'available_until': value})
                self.assertEqual(response.status_code, 201, response.content)

    def test_location_required_keys_bounds_and_unknown_fields(self):
        cases = []
        for field in self.body['location']:
            value = deepcopy(self.body['location'])
            del value[field]
            cases.append((value, 'location.' + field))
        for field, value in (('label', ''), ('label', 'x' * 301), ('area', ''), ('area', 'x' * 101), ('latitude', -91), ('latitude', 91), ('longitude', -181), ('longitude', 181), ('latitude', True), ('latitude', '-6.36'), ('unknown', 1)):
            cases.append(({**self.body['location'], field: value}, 'location.' + field))
        for location, field in cases:
            with self.subTest(location=location):
                self.assert_invalid({**self.body, 'location': location}, field)

    def test_optional_notes_accept_null_or_text(self):
        for value in (None, '', 'Catatan'):
            body = {**self.body, 'storage_notes': value, 'allergen_notes': value, 'packaging_status': value}
            response = self.post(body)
            self.assertEqual(response.status_code, 201, response.content)
            for field in ('storage_notes', 'allergen_notes', 'packaging_status'):
                self.assertEqual(response.json()['data'][field], value)

    def test_guest_cannot_create(self):
        self.client.logout()
        response = self.post()
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(response.json()['errors'][0]['code'], 'authentication_required')
        self.assertEqual(ResourceListing.objects.count(), 0)

    def test_inactive_session_cannot_create(self):
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        response = self.post()
        self.assertEqual(response.status_code, 401, response.content)
        self.assertEqual(ResourceListing.objects.count(), 0)

    def test_missing_or_invalid_csrf_cannot_create(self):
        for headers in ({}, {'HTTP_X_CSRFTOKEN': 'invalid'}):
            with self.subTest(headers=headers):
                response = self.client.post(self.url, self.body, format='json', **headers)
                self.assertEqual(response.status_code, 403, response.content)
                self.assertEqual(response.json()['errors'][0]['code'], 'csrf_failed')
                self.assertEqual(ResourceListing.objects.count(), 0)

    def test_nonobject_json_is_rejected(self):
        self.assert_invalid([])

    def test_malformed_json_is_rejected_without_saving(self):
        response = self.client.post(
            self.url, '{', content_type='application/json', HTTP_X_CSRFTOKEN=self.token,
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(ResourceListing.objects.count(), 0)
