"""PostgreSQL lock tests for SL-BE-03/04.

Reservation endpoints are still scaffolds. The competing transaction below
performs an approval/completion using the contract's source-first lock protocol;
these tests verify listing writes against that boundary, not the future MR API.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Barrier, Event
from unittest import skipUnless

from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase

from pickups.models import Pickup, PickupStatus
from reservations.models import Reservation, ReservationStatus
from shared.models import AuditEvent
from .models import ListingStatus, ResourceListing
from .test_mutations import ListingWriteFixtures


@skipUnless(connection.vendor == 'postgresql', 'Requires PostgreSQL row locks.')
class ListingConcurrencyTests(ListingWriteFixtures, TransactionTestCase):
    def run_behind_source_lock(self, transition, method, body):
        """Prove the API writer blocks, then let it observe the committed change."""
        source_locked = Event()
        release_source = Event()
        writer_pid = Queue()

        def hold_source():
            close_old_connections()
            try:
                with transaction.atomic():
                    listing = ResourceListing.objects.select_for_update().get(pk=self.listing.pk)
                    transition(listing)
                    source_locked.set()
                    if not release_source.wait(timeout=15):
                        raise AssertionError('Timed out releasing source lock.')
            finally:
                source_locked.set()
                connection.close()

        def write_after_lock():
            close_old_connections()
            try:
                client, token = self.client_for(self.owner)
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    writer_pid.put(cursor.fetchone()[0])
                return getattr(client, method)(
                    self.close_url if method == 'post' else self.url,
                    body, format='json', HTTP_X_CSRFTOKEN=token,
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            holder = pool.submit(hold_source)
            writer = None
            try:
                self.assertTrue(source_locked.wait(timeout=10))
                if holder.done():
                    holder.result()
                writer = pool.submit(write_after_lock)
                pid = writer_pid.get(timeout=10)
                deadline = time.monotonic() + 5
                blocked = False
                while time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT cardinality(pg_blocking_pids(%s)) > 0', [pid])
                        blocked = cursor.fetchone()[0]
                    if blocked or writer.done():
                        break
                    time.sleep(0.02)
                self.assertTrue(blocked, 'Listing writer did not wait for the source row lock.')
            finally:
                release_source.set()
                holder.result(timeout=10)
            return writer.result(timeout=10)

    def test_patch_rechecks_quantity_after_concurrent_approval(self):
        reservation = self.reservation(status=ReservationStatus.PENDING, quantity='15.000')

        def approve(listing):
            current = Reservation.objects.select_for_update().get(pk=reservation.pk)
            current.status = ReservationStatus.ACCEPTED
            current.save(update_fields=('status',))

        response = self.run_behind_source_lock(approve, 'patch', {'quantity_total': '10'})
        self.assert_error(response, 409, 'quantity_floor')
        self.listing.refresh_from_db()
        self.assertEqual(str(self.listing.quantity_total), '20.000')
        self.assertEqual(self.listing.version, 1)
        self.assertFalse(AuditEvent.objects.exists())

    def test_close_does_not_cancel_reservation_accepted_while_waiting(self):
        reservation = self.reservation(status=ReservationStatus.PENDING, quantity='15.000')

        def approve(listing):
            current = Reservation.objects.select_for_update().get(pk=reservation.pk)
            current.status = ReservationStatus.ACCEPTED
            current.save(update_fields=('status',))

        response = self.run_behind_source_lock(approve, 'post', {'reason': 'Stop new claims'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['data']['quantity_allocated'], '15.000')
        reservation.refresh_from_db()
        self.assertEqual(reservation.status, ReservationStatus.ACCEPTED)

    def test_delete_sees_reservation_inserted_while_waiting_and_archives(self):
        def insert_claim(listing):
            self.reservation(status=ReservationStatus.PENDING)

        response = self.run_behind_source_lock(insert_claim, 'delete', {})
        self.assertEqual(response.status_code, 204, response.content)
        self.listing.refresh_from_db()
        self.assertIsNotNone(self.listing.archived_at)
        self.assertEqual(self.listing.status, ListingStatus.CLOSED)
        self.assertEqual(self.listing.reservations.get().status, ReservationStatus.CANCELLED)

    def test_close_rechecks_full_completion_after_waiting(self):
        reservation = self.reservation(quantity='20.000', pickup=PickupStatus.SCHEDULED)

        def complete(listing):
            Reservation.objects.select_for_update().get(pk=reservation.pk)
            pickup = Pickup.objects.select_for_update().get(reservation=reservation)
            pickup.status = PickupStatus.COMPLETED
            pickup.save(update_fields=('status',))

        response = self.run_behind_source_lock(complete, 'post', {'reason': 'Too late'})
        self.assert_error(response, 409, 'invalid_transition')
        self.assertFalse(AuditEvent.objects.exists())

    def test_two_simultaneous_closes_only_one_succeeds(self):
        reservation = self.reservation(status=ReservationStatus.PENDING)
        start = Barrier(2)

        def close():
            close_old_connections()
            try:
                client, token = self.client_for(self.owner)
                start.wait(timeout=10)
                return client.post(self.close_url, {'reason': 'Close once'}, format='json', HTTP_X_CSRFTOKEN=token)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(close) for _ in range(2)]
            responses = [future.result(timeout=15) for future in futures]
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        conflict = next(response for response in responses if response.status_code == 409)
        self.assert_error(conflict, 409, 'invalid_transition')
        reservation.refresh_from_db()
        self.assertEqual(reservation.version, 2)
        self.assertEqual(reservation.cancellation_code, 'SOURCE_CLOSED')
        self.assertEqual(AuditEvent.objects.filter(entity_type='ResourceListing', action='close').count(), 1)
        self.assertEqual(AuditEvent.objects.filter(entity_type='Reservation').count(), 1)
