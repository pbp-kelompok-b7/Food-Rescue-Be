from .settings import *  # noqa: F403,F401

# Explicit local smoke tests only; normal CI must still use PostgreSQL.
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
