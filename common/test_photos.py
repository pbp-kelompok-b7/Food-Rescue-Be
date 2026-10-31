import json
import tempfile
from datetime import timedelta
from io import BytesIO

from PIL import Image
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient


def photo():
    data = BytesIO()
    Image.new('RGB', (16, 12), 'green').save(data, 'PNG')
    return SimpleUploadedFile('test.png', data.getvalue(), content_type='image/png')


class ResourcePhotoTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.directory.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.owner = get_user_model().objects.create_user(
            email='photo-owner@example.com', password='secret', display_name='Owner')
        self.stranger = get_user_model().objects.create_user(
            email='photo-stranger@example.com', password='secret', display_name='Stranger')
        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def payload(self, kind):
        data = dict(resource_type='PACKAGED_FOOD_SURPLUS', category='prepared_meal', unit='box',
                    location=dict(label='Kukusan, Depok', area='Depok', latitude=-6.366, longitude=106.818))
        deadline = (timezone.now() + timedelta(days=1)).isoformat()
        if kind == 'listings':
            data.update(title='Makanan', description='Layak konsumsi', quantity_total='2', mode='DONATE',
                        unit_price='0', condition='Baik', suitability=['HUMAN_CONSUMPTION'], available_until=deadline)
        else:
            data.update(intended_use='HUMAN_CONSUMPTION', quantity_needed='2', donate_only=True, valid_until=deadline)
        return data

    def test_photo_create_replace_remove_and_parent_visibility(self):
        for kind in ('listings', 'requests'):
            with self.subTest(kind=kind):
                self.client.force_authenticate(self.owner)
                response = self.client.post(f'/api/v1/{kind}/',
                    {'payload': json.dumps(self.payload(kind)), 'image': photo()}, format='multipart')
                self.assertEqual(response.status_code, 201, response.data)
                record = response.data['data']
                url = record['image']
                self.assertTrue(url.startswith('/api/v1/media/photos/'))
                detail = f'/api/v1/{kind}/{record["id"]}/'
                self.client.force_authenticate(None)
                image_response = self.client.get(url)
                self.assertEqual(image_response.status_code, 200)
                content = b''.join(image_response.streaming_content)
                self.assertEqual(Image.open(BytesIO(content)).format, 'JPEG')
                self.client.force_authenticate(self.stranger)
                self.assertEqual(self.client.patch(detail, {'payload': '{}', 'image': photo()}, format='multipart').status_code, 403)
                self.client.force_authenticate(self.owner)
                changed = self.client.patch(detail, {'payload': '{}', 'image': photo()}, format='multipart')
                self.assertEqual(changed.status_code, 200, changed.data)
                new_url = changed.data['data']['image']
                self.assertNotEqual(url, new_url)
                self.assertEqual(self.client.get(url).status_code, 404)
                closed = self.client.post(detail + 'close/', {'reason': 'Selesai'}, format='json')
                self.assertEqual(closed.status_code, 200)
                self.client.force_authenticate(None)
                self.assertEqual(self.client.get(new_url).status_code, 404)
                self.client.force_authenticate(self.owner)
                owner_photo = self.client.get(new_url)
                self.assertEqual(owner_photo.status_code, 200)
                self.assertTrue(b''.join(owner_photo.streaming_content))
                removed = self.client.patch(detail, {'image': None}, format='json')
                self.assertEqual(removed.status_code, 200, removed.data)
                self.assertIsNone(removed.data['data']['image'])
                self.assertEqual(self.client.get(new_url).status_code, 404)

    def test_invalid_image_and_malformed_multipart_are_rejected(self):
        for kind in ('listings', 'requests'):
            url = f'/api/v1/{kind}/'
            invalid = SimpleUploadedFile('fake.jpg', b'not an image', content_type='image/jpeg')
            response = self.client.post(url, {'payload': json.dumps(self.payload(kind)), 'image': invalid}, format='multipart')
            self.assertEqual(response.status_code, 400, response.data)
            self.assertTrue(any(error['field'] == 'image' for error in response.data['errors']))
            for payload in ('not-json', '[]'):
                response = self.client.post(url, {'payload': payload, 'image': photo()}, format='multipart')
                self.assertEqual(response.status_code, 400)
            huge = SimpleUploadedFile('huge.png', b'x' * (5 * 1024 * 1024 + 1), content_type='image/png')
            response = self.client.post(url, {'payload': json.dumps(self.payload(kind)), 'image': huge}, format='multipart')
            self.assertEqual(response.status_code, 400)
            response = self.client.post(url, self.payload(kind), format='json')
            self.assertEqual(response.status_code, 201, response.data)
            self.assertIsNone(response.data['data']['image'])
