import json
import os
import subprocess
import sys

from django.test import SimpleTestCase


class ProductionConfigurationTests(SimpleTestCase):
    def test_public_runtime_works_even_with_stale_local_environment(self):
        environment = {**os.environ, 'DJANGO_DEBUG': 'True',
            'DJANGO_ALLOWED_HOSTS': 'localhost,127.0.0.1',
            'CORS_ALLOWED_ORIGINS': 'http://localhost:5173',
            'CSRF_TRUSTED_ORIGINS': 'http://localhost:5173'}
        environment.pop('DJANGO_SETTINGS_MODULE', None)
        script = '''
import json
from config import production as s
print(json.dumps(dict(debug=s.DEBUG, hosts=s.ALLOWED_HOSTS,
    cors=s.CORS_ALLOWED_ORIGINS, csrf=s.CSRF_TRUSTED_ORIGINS,
    secure=s.SESSION_COOKIE_SECURE, same_site=s.SESSION_COOKIE_SAMESITE)))
'''
        result = subprocess.run([sys.executable, '-c', script], env=environment,
                                text=True, capture_output=True, check=True)
        settings = json.loads(result.stdout)
        self.assertFalse(settings['debug'])
        self.assertIn('fiqhi-deski-food-rescue.pws.cs.ui.ac.id', settings['hosts'])
        self.assertIn('https://food-rescue-fe.vercel.app', settings['cors'])
        self.assertIn('https://food-rescue-fe.vercel.app', settings['csrf'])
        self.assertTrue(settings['secure'])
        self.assertEqual(settings['same_site'], 'Lax')

    def test_wsgi_defaults_to_public_runtime(self):
        environment = dict(os.environ)
        environment.pop('DJANGO_SETTINGS_MODULE', None)
        result = subprocess.run([sys.executable, '-c',
            'import config.wsgi; from django.conf import settings; print(settings.SETTINGS_MODULE)'],
            env=environment, text=True, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), 'config.production')
