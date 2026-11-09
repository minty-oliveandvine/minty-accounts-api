"""Tests: the required values set to placeholders, so no .env is needed (CI has none).

Two test databases, chosen by MINTY_TEST_PG_URI (the same knobs as the other Django APIs):

  unset  -> SQLite in memory, tables built from the models. The token service's tests need
            Postgres (advisory locks) and skip.
  set    -> PostgreSQL, a database built from minty-db's packaged 01_schema_rebased.sql by
            minty_db.testing.pg (the root conftest.py's plugin). MINTY_TEST_PG_DBNAME
            (minty_test - always give a unique one), MINTY_TEST_PG_KEEP=1.
"""

import os

# Fixed values, never the developer's .env: no test may reach a real Flask, Xero or database.
os.environ["SECRET_KEY"] = "test-secret-key-shared-with-flask"
os.environ["XERO_CLIENT_ID"] = "test-client-id"
os.environ["XERO_CLIENT_SECRET"] = "test-client-secret"
os.environ["INVITE_FROM"] = "Minty <invites@test.minty>"
os.environ["ACCOUNTS_API_URL"] = "https://accounts-api.test"
os.environ["SMTP_URL"] = "smtp://mail.test:587"
os.environ["SUBSCRIPTION_API_URL"] = "http://subscription.invalid"
for _name in ("PREVIEW_ORIGIN_REGEX", "EXTRA_FRONTEND_ORIGINS", "CONTACT_EMAIL", "CONTACT_FROM"):
    os.environ.pop(_name, None)
os.environ.setdefault("DATABASE_URL", "postgresql://postgres@localhost:5432/postgres")

from minty_shared.config import django_database  # noqa: E402

from config.settings import *  # noqa: E402,F403

# No test sends real mail: Django's in-memory outbox (mail.outbox).
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

_PG_URI = os.environ.get("MINTY_TEST_PG_URI")
if _PG_URI:
    from urllib.parse import parse_qs as _parse_qs
    from urllib.parse import urlsplit as _urlsplit

    _db, _schema = django_database(_PG_URI)
    if "schema" in _parse_qs(_urlsplit(_PG_URI).query):
        DB_SCHEMA = _schema
    _db["OPTIONS"]["options"] = f"-c search_path={DB_SCHEMA},public"  # noqa: F405
    _db["NAME"] = os.environ.get("MINTY_TEST_PG_DBNAME", "minty_test")
    _db["TEST"] = {"NAME": _db["NAME"]}
    DATABASES = {"default": _db}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
