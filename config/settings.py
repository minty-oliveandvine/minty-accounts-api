"""minty-accounts-api - Minty's accounts service, on Flask's public address.

The Flask retirement (2026-11-05): Django owns the paths in ``config/urls.py`` (Phase 1
``/health``, Phase 2 the Xero token service); every other request falls through to Flask
unchanged (``core/fallthrough.py``, mounted in ``config/wsgi.py``). Each later phase moves another
group of Flask's routes in.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv
from minty_shared.logctx import logging_config

from config.dburl import database_url, parse_database_url

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"{name} is required and is not set")
    return value


APP_ENV = os.environ.get("APP_ENV", "production")
# The same key as Flask and the other three APIs (they verify each other's tokens); Phase 1
# signs nothing, but the service refuses to start without the key it will need.
SECRET_KEY = _required("SECRET_KEY")
# Never from APP_ENV: a development service runs on production's data until the launch, and
# Django's debug pages print tracebacks and settings.
DEBUG = os.environ.get("DJANGO_DEBUG") == "1"
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "*").split(",") if h.strip()]

#: Where every request Django does not route goes: Flask's own address (not the public one).
FLASK_FALLBACK_URL = _required("FLASK_FALLBACK_URL")

#: Xero's OAuth client (the same app Flask signs in with). The token service refreshes with it:
#: this service is the only caller of Xero's /connect/token for other services (Phase 2).
XERO_CLIENT_ID = _required("XERO_CLIENT_ID")
XERO_CLIENT_SECRET = _required("XERO_CLIENT_SECRET")

# minty_db's models are the schema (minty-db, generated from 01_schema_rebased.sql). Its
# migrations are run by hand (python -m minty_db migrate), never by a service.
INSTALLED_APPS = ["minty_db", "xero"]
MIGRATION_MODULES = {"minty_db": None}
# Only for the paths Django owns; the fall-through runs in front of all of it (config/wsgi.py).
MIDDLEWARE = ["minty_shared.django.middleware.RequestLoggingMiddleware"]
MINTY_HTTP_LOGGER = "minty-accounts-api.http"
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# The schema Minty owns; ``?schema=`` on DATABASE_URL names it (default pettycashv3,
# config/dburl.py) and every db_table resolves through search_path. Required: a missing URL must
# not quietly reach the local default from a host.
_required("DATABASE_URL")
_DEFAULT_DB, DB_SCHEMA = parse_database_url(database_url())
DATABASES = {"default": _DEFAULT_DB}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"

# The root logger only: "minty-accounts-api" and Django's own loggers reach it by propagation
# (naming one here as well writes each of its lines twice).
LOGGING = logging_config(
    {"": None},
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
