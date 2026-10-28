"""SL-BE-03/04: PATCH, close and delete/archive API contracts."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from pickups.models import Pickup, PickupStatus
from reservations.models import Reservation, ReservationStatus
from shared.models import AuditEvent
from .models import ListingStatus, ResourceListing


class ListingWriteFixtures:
    now = datetime(2026, 10, 27, 5, tzinfo=timezone.utc)

    def setUp(self):
        super().setUp()
        timer = patch('django.utils.timezone.now', return_value=self.now)
        timer.start()
        self.addCleanup(timer.stop)
        user = get_user_model()
        self.owner = user.objects.create_user(email='writer@example.com', display_name='Owner')
        self.other = user.objects.create_user(email='other-writer@example.com', display_name='Other')
        self.receiver = user.objects.create_user(email='write-receiver@example.com', display_name='Receiver')
        self.admin = user.objects.create_user(email='write-admin@example.com', display_name='Admin', is_staff=True)
        self.client, self.csrf = self.client_for(self.owner)
        self.listing = ResourceListing.objects.create(
            owner=self.owner, created_by=self.owner, resource_type='PACKAGED_FOOD_SURPLUS',
            title='Rice box', category='prepared_meal', description='Nasi ayam',
            quantity_total='20.000', unit='box', mode='DONATE', unit_price='0.00',
            condition='Segar', suitability=['HUMAN_CONSUMPTION'],
            location={'label': 'Kampus UI', 'area': 'Depok', 'latitude': -6.36, 'longitude': 106.82},
            available_until=self.now + timedelta(days=1), storage_notes='Simpan dingin',
        )
        self.url = f'/api/v1/listings/{self.listing.pk}/'
        self.close_url = self.url + 'close/'

    def client_for(self, actor):
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(actor)
        token = client.get('/api/v1/auth/csrf/').json()['data']['csrf_token']
        return client, token

    def reservation(self, status=ReservationStatus.ACCEPTED, quantity='5.000', pickup=None, **extra):
        reservation = Reservation.objects.create(
            source_type='LISTING_CLAIM', listing=self.listing, provider=self.owner,
            receiver=self.receiver, submitted_by=self.receiver, quantity=quantity,
            unit=self.listing.unit, mode=self.listing.mode, unit_price=self.listing.unit_price,
            resource_deadline=self.listing.available_until, status=status,
            accepted_snapshot={'title': 'Original', 'location': self.listing.location},
            **extra,
        )
        if pickup:
            Pickup.objects.create(
                reservation=reservation, status=pickup,
                scheduled_at=self.now + timedelta(hours=1), location=self.listing.location,
            )
        return reservation

    def write(self, method='patch', body=None, actor=None, url=None):
        client, token = (self.client, self.csrf) if actor is None else self.client_for(actor)
        return getattr(client, method)(
            url or (self.close_url if method == 'post' else self.url),
            body if body is not None else {}, format='json', HTTP_X_CSRFTOKEN=token,
        )

    def assert_error(self, response, status, code=None, field=None):
        self.assertEqual(response.status_code, status, response.content)
        payload = response.json()
        self.assertIs(payload['success'], False)
        self.assertTrue(payload['errors'])
        if code or field:
            self.assertTrue(any(
                (code is None or error['code'] == code)
                and (field is None or (error['field'] or '').startswith(field))
                for error in payload['errors']
            ), payload)


class ListingWriteTests(ListingWriteFixtures, TestCase):
    def test_partial_patch_preserves_omitted_fields_and_increments_version(self):
        previous_deadline = self.listing.available_until
        response = self.write(body={'description': 'Ambil di lobi'})
        self.assertEqual(response.status_code, 200, response.content)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.description, 'Ambil di lobi')
        self.assertEqual(self.listing.storage_notes, 'Simpan dingin')
        self.assertEqual(self.listing.available_until, previous_deadline)
        self.assertEqual(self.listing.version, 2)
        self.assertEqual(response.json()['data']['version'], 2)
        event = AuditEvent.objects.get(entity_type='ResourceListing', action='update')
        self.assertEqual(event.actor_id, self.owner.pk)
        self.assertEqual(event.summary['before']['version'], 1)
        self.assertEqual(event.summary['after']['version'], 2)
        self.assertEqual(event.summary['changed_fields'], ['description'])

    def test_patch_requires_at_least_one_mutable_field(self):
        for body in ({}, [], {'location': {}}):
            with self.subTest(body=body):
                self.assert_error(self.write(body=body), 400)
        self.assertFalse(AuditEvent.objects.exists())

    def test_patch_rejects_all_readonly_and_unknown_fields(self):
        for field in ('owner', 'owner_id', 'created_by', 'status', 'archived_at', 'version', 'id', 'created_at', 'updated_at', 'quantity_available', 'quantity_allocated', 'quantity_completed', 'expected_version', 'reason', 'unknown'):
            with self.subTest(field=field):
                self.assert_error(self.write(body={field: 'x'}), 400, 'unknown_field', field)

    def test_patch_validates_resulting_mode_and_price(self):
        self.assert_error(self.write(body={'mode': 'SELL'}), 400, field='unit_price')
        response = self.write(body={'mode': 'SELL', 'unit_price': '12000.00'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assert_error(self.write(body={'mode': 'DONATE'}), 400, field='unit_price')
        response = self.write(body={'mode': 'DONATE', 'unit_price': '0.00'})
        self.assertEqual(response.status_code, 200, response.content)

    def test_patch_reuses_create_field_validation(self):
        for body in (
            {'title': ''}, {'description': 'x' * 5001}, {'condition': None},
            {'quantity_total': '0'}, {'quantity_total': '1.500'}, {'quantity_total': '1.0001'},
            {'unit_price': '-1'}, {'suitability': []}, {'suitability': ['COMPOSTING']},
            {'available_until': self.now.isoformat()}, {'available_until': '2026-10-28T12:00:00'},
            {'location': {'latitude': 91, 'longitude': 100}}, {'storage_notes': 'x' * 2001},
        ):
            with self.subTest(body=body):
                self.assert_error(self.write(body=body), 400)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.version, 1)

    def test_partial_location_merges_and_coordinates_require_a_pair(self):
        original = deepcopy(self.listing.location)
        response = self.write(body={'location': {'label': 'Lobi UI'}})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['data']['location'], {**original, 'label': 'Lobi UI'})
        self.assert_error(self.write(body={'location': {'latitude': -6.4}}), 400, field='location.longitude')
        self.assert_error(self.write(body={'location': {'longitude': 107}}), 400, field='location.latitude')
        self.assert_error(self.write(body={'location': {'unknown': 1}}), 400)
        response = self.write(body={'location': {'latitude': -6.4, 'longitude': 107}})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['data']['location']['area'], original['area'])

    def test_quantity_floor_counts_allocated_and_completed_but_not_pending(self):
        self.reservation(quantity='5.000', pickup=PickupStatus.SCHEDULED)
        self.reservation(quantity='4.000', pickup=PickupStatus.COMPLETED)
        self.reservation(status=ReservationStatus.PENDING, quantity='12.000')
        self.assert_error(self.write(body={'quantity_total': '8', 'title': 'Must roll back'}), 409, 'quantity_floor', 'quantity_total')
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.title, 'Rice box')
        self.assertEqual(self.listing.version, 1)
        response = self.write(body={'quantity_total': '9'})
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()['data']
        self.assertEqual(row['quantity_available'], '0.000')
        self.assertEqual(row['quantity_allocated'], '5.000')
        self.assertEqual(row['quantity_completed'], '4.000')
        self.assertEqual(row['status'], 'RESERVED')
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, ListingStatus.RESERVED)

    def test_unit_type_category_locked_for_every_reservation_status(self):
        for status in ReservationStatus.values:
            reservation = self.reservation(status=status)
            for field, value in (('unit', 'kg'), ('resource_type', 'COMPOSTABLE_FOOD_WASTE'), ('category', 'bakery')):
                with self.subTest(status=status, field=field):
                    self.assert_error(self.write(body={field: value}), 409, 'unit_locked', field)
            reservation.delete()

    def test_same_unit_type_category_are_allowed_with_history(self):
        self.reservation()
        response = self.write(body={'unit': 'box', 'resource_type': 'PACKAGED_FOOD_SURPLUS', 'category': 'prepared_meal'})
        self.assertEqual(response.status_code, 200, response.content)

    def test_type_category_unit_can_change_without_history(self):
        response = self.write(body={
            'resource_type': 'COMPOSTABLE_FOOD_WASTE', 'category': 'coffee_grounds',
            'unit': 'kg', 'suitability': ['COMPOSTING', 'BSF'], 'quantity_total': '1.555',
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['data']['quantity_available'], '1.555')

    def test_patch_never_changes_existing_agreement_price_deadline_or_snapshot(self):
        accepted = self.reservation(pickup=PickupStatus.SCHEDULED)
        pending = self.reservation(status=ReservationStatus.PENDING)
        before = list(Reservation.objects.values())
        pickups_before = list(Pickup.objects.values())
        response = self.write(body={
            'title': 'New title', 'mode': 'SELL', 'unit_price': '5000.00',
            'available_until': (self.now + timedelta(hours=12)).isoformat(),
            'location': {'label': 'New lobby'},
        })
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(list(Reservation.objects.values()), before)
        self.assertEqual(list(Pickup.objects.values()), pickups_before)
        self.assertEqual(accepted.pk, before[1]['id'])
        self.assertEqual(pending.pk, before[0]['id'])

    def test_patch_does_not_reopen_terminal_or_effectively_expired_source(self):
        for status in ('CLOSED', 'EXPIRED', 'COMPLETED', 'AVAILABLE'):
            ResourceListing.objects.filter(pk=self.listing.pk).update(
                status=status, available_until=self.now - timedelta(seconds=1),
            )
            with self.subTest(status=status):
                response = self.write(body={'available_until': (self.now + timedelta(days=2)).isoformat()})
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()['data']['status'], 'EXPIRED' if status == 'AVAILABLE' else status)
                self.assertEqual(self.client.get('/api/v1/listings/').json()['data']['count'], 0)

    def test_patch_allows_nullable_notes(self):
        response = self.write(body={'storage_notes': None, 'allergen_notes': 'Kacang', 'packaging_status': None})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.json()['data']['storage_notes'])
        self.assertEqual(response.json()['data']['allergen_notes'], 'Kacang')

    def test_extending_expired_listing_deadline_does_not_restore_released_allocation(self):
        # Accepted terms can retain a later resource deadline after a source edit.
        self.reservation(quantity='5.000')
        ResourceListing.objects.filter(pk=self.listing.pk).update(available_until=self.now)
        response = self.write(body={'available_until': (self.now + timedelta(days=2)).isoformat()})
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()['data']
        self.assertEqual(row['status'], 'EXPIRED')
        self.assertEqual(row['quantity_allocated'], '0.000')
        self.assertEqual(row['quantity_available'], '20.000')

    def test_active_nonowner_forbidden_private_nonowner_hidden(self):
        for method, body in (('patch', {'title': 'Hijacked'}), ('post', {'reason': 'Close'}), ('delete', {})):
            with self.subTest(method=method):
                self.assert_error(self.write(method, body, actor=self.other), 403, 'permission_denied')
        ResourceListing.objects.filter(pk=self.listing.pk).update(status=ListingStatus.CLOSED)
        for method, body in (('patch', {'title': 'Hijacked'}), ('post', {'reason': 'Close'}), ('delete', {})):
            with self.subTest(method=method):
                self.assert_error(self.write(method, body, actor=self.other), 404, 'not_found')

    def test_participant_can_read_but_not_modify_private_source(self):
        self.reservation()
        ResourceListing.objects.filter(pk=self.listing.pk).update(status=ListingStatus.CLOSED)
        self.assert_error(self.write(body={'title': 'Hijacked'}, actor=self.receiver), 403, 'permission_denied')

    def test_admin_can_edit_close_and_archive_another_owners_listing(self):
        self.reservation(status=ReservationStatus.PENDING)
        response = self.write(body={'title': 'Moderated'}, actor=self.admin)
        self.assertEqual(response.status_code, 200, response.content)
        response = self.write('post', {'reason': 'Moderation'}, actor=self.admin)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.write('delete', actor=self.admin).status_code, 204)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.owner_id, self.owner.pk)
        self.assertEqual(self.listing.created_by_id, self.owner.pk)
        self.assertEqual(set(AuditEvent.objects.values_list('actor_id', flat=True)), {self.admin.pk})

    def test_guest_and_inactive_sessions_cannot_write(self):
        guest = APIClient(enforce_csrf_checks=True)
        for method, url, body in (('patch', self.url, {'title': 'x'}), ('post', self.close_url, {'reason': 'x'}), ('delete', self.url, {})):
            with self.subTest(method=method):
                self.assert_error(getattr(guest, method)(url, body, format='json'), 401, 'authentication_required')
        get_user_model().objects.filter(pk=self.owner.pk).update(is_active=False)
        self.assert_error(self.write(body={'title': 'x'}), 401, 'authentication_required')

    def test_all_writes_require_csrf(self):
        for method, url, body in (('patch', self.url, {'title': 'x'}), ('post', self.close_url, {'reason': 'x'}), ('delete', self.url, {})):
            for headers in ({}, {'HTTP_X_CSRFTOKEN': 'invalid'}):
                with self.subTest(method=method, headers=headers):
                    self.assert_error(getattr(self.client, method)(url, body, format='json', **headers), 403, 'csrf_failed')

    def test_malformed_ids_unknown_queries_and_missing_objects(self):
        for method, body in (('patch', {'title': 'x'}), ('post', {'reason': 'x'}), ('delete', {})):
            for pk in ('abc', '0', '-1', '1.5', '9223372036854775808'):
                url = f'/api/v1/listings/{pk}/' + ('close/' if method == 'post' else '')
                with self.subTest(method=method, pk=pk):
                    self.assert_error(self.write(method, body, url=url), 400, field='id')
            url = '/api/v1/listings/999999/' + ('close/' if method == 'post' else '')
            self.assert_error(self.write(method, body, url=url), 404, 'not_found')
            url = (self.close_url if method == 'post' else self.url) + '?unexpected=1'
            self.assert_error(self.write(method, body, url=url), 400, 'unknown_field')

    def test_close_requires_valid_reason_and_rejects_extra_fields(self):
        for body in ({}, {'reason': ''}, {'reason': ' '}, {'reason': None}, {'reason': 'x' * 1001}, {'reason': 'x', 'status': 'CLOSED'}, []):
            with self.subTest(body=body):
                self.assert_error(self.write('post', body), 400)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, ListingStatus.AVAILABLE)
        self.assertFalse(AuditEvent.objects.exists())

    def test_close_cancels_pending_only_and_keeps_accepted_quantity_pickup_and_terms(self):
        pending = self.reservation(status=ReservationStatus.PENDING)
        accepted = self.reservation(quantity='6.000', pickup=PickupStatus.SCHEDULED)
        completed = self.reservation(quantity='4.000', pickup=PickupStatus.COMPLETED)
        rejected = self.reservation(status=ReservationStatus.REJECTED)
        cancelled = self.reservation(status=ReservationStatus.CANCELLED)
        unchanged = list(Reservation.objects.exclude(pk=pending.pk).values())
        pickup_before = list(Pickup.objects.values())
        response = self.write('post', {'reason': 'Tidak menerima claim baru'})
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()['data']
        self.assertEqual((row['status'], row['quantity_available'], row['quantity_allocated'], row['quantity_completed']), ('CLOSED', '10.000', '6.000', '4.000'))
        pending.refresh_from_db()
        self.assertEqual(pending.status, ReservationStatus.CANCELLED)
        self.assertEqual(pending.cancellation_code, 'SOURCE_CLOSED')
        self.assertEqual(pending.reason, 'Tidak menerima claim baru')
        self.assertEqual(pending.version, 2)
        self.assertEqual(list(Reservation.objects.exclude(pk=pending.pk).values()), unchanged)
        self.assertEqual(list(Pickup.objects.values()), pickup_before)
        event = AuditEvent.objects.get(entity_type='ResourceListing', action='close')
        self.assertEqual(event.summary['reason'], pending.reason)
        self.assertEqual(event.summary['cancelled_reservation_ids'], [pending.pk])

    def test_close_terminal_and_repeated_close_conflict_without_more_changes(self):
        for status in ('CLOSED', 'EXPIRED', 'COMPLETED'):
            ResourceListing.objects.filter(pk=self.listing.pk).update(status=status)
            with self.subTest(status=status):
                self.assert_error(self.write('post', {'reason': 'Again'}), 409, 'invalid_transition')
        ResourceListing.objects.filter(pk=self.listing.pk).update(status='AVAILABLE')
        self.assertEqual(self.write('post', {'reason': 'First'}).status_code, 200)
        before = list(AuditEvent.objects.values())
        self.assert_error(self.write('post', {'reason': 'Again'}), 409, 'invalid_transition')
        self.assertEqual(list(AuditEvent.objects.values()), before)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.version, 2)

    def test_close_effectively_expired_or_fully_completed_is_conflict(self):
        ResourceListing.objects.filter(pk=self.listing.pk).update(available_until=self.now)
        self.assert_error(self.write('post', {'reason': 'Late'}), 409, 'invalid_transition')
        ResourceListing.objects.filter(pk=self.listing.pk).update(available_until=self.now + timedelta(days=1))
        self.reservation(quantity='20.000', pickup=PickupStatus.COMPLETED)
        self.assert_error(self.write('post', {'reason': 'Already done'}), 409, 'invalid_transition')

    def test_fully_reserved_source_can_be_closed(self):
        self.reservation(quantity='20.000', pickup=PickupStatus.SCHEDULED)
        response = self.write('post', {'reason': 'Stop new claims'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['data']['quantity_allocated'], '20.000')

    def test_closed_source_stays_closed_after_last_pickup_completes(self):
        reservation = self.reservation(quantity='20.000', pickup=PickupStatus.SCHEDULED)
        self.assertEqual(self.write('post', {'reason': 'Stop'}).status_code, 200)
        Pickup.objects.filter(reservation=reservation).update(status=PickupStatus.COMPLETED)
        row = self.client.get(self.url).json()['data']
        self.assertEqual(row['status'], 'CLOSED')
        self.assertEqual(row['quantity_completed'], '20.000')

    def test_delete_without_transactions_is_hard_delete_with_empty_response_and_audit(self):
        response = self.write('delete')
        self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual(response.content, b'')
        self.assertFalse(ResourceListing.objects.filter(pk=self.listing.pk).exists())
        event = AuditEvent.objects.get(action='delete')
        self.assertEqual(event.entity_id, str(self.listing.pk))
        self.assertIsNone(event.summary['after'])
        self.assert_error(self.write('delete'), 404, 'not_found')

    def test_delete_rejects_any_body_field(self):
        for body in ({'reason': 'Delete'}, {'owner': self.owner.pk}, []):
            with self.subTest(body=body):
                self.assert_error(self.write('delete', body), 400)
        self.assertTrue(ResourceListing.objects.filter(pk=self.listing.pk).exists())

    def test_delete_without_request_body_is_supported(self):
        response = self.client.delete(self.url, HTTP_X_CSRFTOKEN=self.csrf)
        self.assertEqual(response.status_code, 204, response.content)
        self.assertEqual(response.content, b'')

    def test_any_reservation_history_forces_archive_including_rejected_cancelled(self):
        for status in ReservationStatus.values:
            with self.subTest(status=status):
                reservation = self.reservation(status=status)
                response = self.write('delete')
                self.assertEqual(response.status_code, 204, response.content)
                self.assertEqual(response.content, b'')
                self.listing.refresh_from_db()
                self.assertIsNotNone(self.listing.archived_at)
                self.assertEqual(self.listing.status, ListingStatus.CLOSED)
                reservation.refresh_from_db()
                self.assertEqual(reservation.listing_id, self.listing.pk)
                expected = ReservationStatus.CANCELLED if status == ReservationStatus.PENDING else status
                self.assertEqual(reservation.status, expected)
                reservation.delete()
                ResourceListing.objects.filter(pk=self.listing.pk).update(status='AVAILABLE', archived_at=None)

    def test_archive_keeps_accepted_pickup_and_snapshot_and_hides_from_public(self):
        pending = self.reservation(status=ReservationStatus.PENDING)
        accepted = self.reservation(pickup=PickupStatus.SCHEDULED)
        snapshot = deepcopy(accepted.accepted_snapshot)
        self.assertEqual(self.write('delete').status_code, 204)
        pending.refresh_from_db()
        accepted.refresh_from_db()
        self.assertEqual(pending.cancellation_code, 'SOURCE_CLOSED')
        self.assertEqual(accepted.status, ReservationStatus.ACCEPTED)
        self.assertEqual(accepted.accepted_snapshot, snapshot)
        self.assertEqual(accepted.pickup.status, PickupStatus.SCHEDULED)
        guest = APIClient()
        self.assertEqual(guest.get('/api/v1/listings/').json()['data']['count'], 0)
        self.assert_error(guest.get(self.url), 404, 'not_found')
        for actor in (self.owner, self.receiver, self.admin):
            client, _ = self.client_for(actor)
            response = client.get(self.url)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertIsNotNone(response.json()['data']['archived_at'])

    def test_archiving_terminal_sources_preserves_terminal_status(self):
        self.reservation(status=ReservationStatus.REJECTED)
        for status in ('CLOSED', 'EXPIRED', 'COMPLETED'):
            ResourceListing.objects.filter(pk=self.listing.pk).update(status=status, archived_at=None)
            with self.subTest(status=status):
                self.assertEqual(self.write('delete').status_code, 204)
                self.listing.refresh_from_db()
                self.assertEqual(self.listing.status, status)
                self.assertIsNotNone(self.listing.archived_at)

    def test_repeated_archive_is_idempotent(self):
        pending = self.reservation(status=ReservationStatus.PENDING)
        self.assertEqual(self.write('delete').status_code, 204)
        self.listing.refresh_from_db()
        initial = (self.listing.archived_at, self.listing.version, AuditEvent.objects.count())
        self.assertEqual(self.write('delete').status_code, 204)
        self.listing.refresh_from_db()
        self.assertEqual((self.listing.archived_at, self.listing.version, AuditEvent.objects.count()), initial)
        pending.refresh_from_db()
        self.assertEqual(pending.version, 2)

    def test_audit_failure_rolls_back_patch_close_archive_and_hard_delete(self):
        for method, body in (('patch', {'title': 'Changed'}), ('post', {'reason': 'Close'}), ('delete', {})):
            pending = self.reservation(status=ReservationStatus.PENDING)
            before = list(ResourceListing.objects.values())
            reservation_before = list(Reservation.objects.values())
            with self.subTest(method=method), patch('listings.services._audit', side_effect=RuntimeError('Audit unavailable')):
                with self.assertLogs('common.exceptions', level='ERROR'):
                    self.assert_error(self.write(method, body), 500, 'server_error')
                self.assertEqual(list(ResourceListing.objects.values()), before)
                self.assertEqual(list(Reservation.objects.values()), reservation_before)
                self.assertFalse(AuditEvent.objects.exists())
            pending.delete()
        with patch('listings.services._audit', side_effect=RuntimeError('Audit unavailable')):
            with self.assertLogs('common.exceptions', level='ERROR'):
                self.assert_error(self.write('delete'), 500, 'server_error')
        self.assertTrue(ResourceListing.objects.filter(pk=self.listing.pk).exists())
