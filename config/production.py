"""Public PWS runtime; manage.py/runserver keep using local settings."""
from .settings import *

# PWS can retain development env values across deployments. The public server
# must still recognize this application's canonical host and frontend origin.
DEBUG = False
PUBLIC_API_HOST = env('PUBLIC_API_HOST', default='fiqhi-deski-food-rescue.pws.cs.ui.ac.id')
PUBLIC_WEB_ORIGIN = env('PUBLIC_WEB_ORIGIN', default='https://food-rescue-fe.vercel.app')
ALLOWED_HOSTS = list(dict.fromkeys([*ALLOWED_HOSTS, PUBLIC_API_HOST]))
CORS_ALLOWED_ORIGINS = list(dict.fromkeys([*CORS_ALLOWED_ORIGINS, PUBLIC_WEB_ORIGIN]))
CSRF_TRUSTED_ORIGINS = list(dict.fromkeys([*CSRF_TRUSTED_ORIGINS, PUBLIC_WEB_ORIGIN]))
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# Vercel forwards /api to PWS, so these stay first-party, secure cookies.
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
