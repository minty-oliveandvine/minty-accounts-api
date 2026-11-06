"""minty-accounts-api - Minty's accounts service, on Flask's public address.

The Flask retirement (2026-11-05): Django owns the paths in ``config/urls.py`` (Phase 1
``/health``, Phase 2 the Xero token service); every other request falls through to Flask
unchanged (``core/fallthrough.py``, mounted in ``config/wsgi.py``). Each later phase moves another
group of Flask's routes in.
"""

import os
import re
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv
from minty_shared.config import django_email, is_development, preview_origin_regex
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
DEVELOPMENT = is_development()
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
#: Xero's accounting API (Flask's ``XERO_API_BASE_URL``; petty-cash-api's default).
XERO_API_BASE_URL = (os.environ.get("XERO_API_BASE_URL") or "https://api.xero.com/api.xro/2.0").rstrip("/")

# minty_db's models are the schema (minty-db, generated from 01_schema_rebased.sql). Its
# migrations are run by hand (python -m minty_db migrate), never by a service.
INSTALLED_APPS = ["minty_db", "xero", "hub"]
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

# ---- the hub and the public routes (Phase 5) -------------------------------------------------


def _url(name: str, default: str) -> str:
    return (os.environ.get(name) or default).strip().rstrip("/")


#: The front ends (Flask's ``bearer_api`` origins and defaults): the hub's CORS echoes these.
MINTY_WEB_URL = _url("MINTY_WEB_URL", "http://localhost:3000")
PAYMENT_REQUEST_WEB_URL = _url("PAYMENT_REQUEST_WEB_URL", "http://localhost:3020")
PETTY_CASH_WEB_URL = _url("PETTY_CASH_WEB_URL", "http://localhost:3010")
ONBOARDING_WEB_URL = _url("ONBOARDING_WEB_URL", "http://localhost:3030")
#: A branch's preview set (``pr-<slug>-<app>.dailyminty.com``) or None; a value that could open
#: CORS to any other host refuses to start (``minty_shared.config``, every backend's rule).
PREVIEW_ORIGIN = preview_origin_regex(os.environ.get("PREVIEW_ORIGIN_REGEX"))
FRONTEND_APPS = ("my", "payment-request", "onboarding", "pettycash")
_BARE_HTTPS_ORIGIN = re.compile(r"https://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")


def _extra_frontend_origins(raw: str | None) -> dict[str, str]:
    """``EXTRA_FRONTEND_ORIGINS``: ``app=https://host`` pairs, each app once (Flask's
    ``extra_frontend_origins``). Anything else refuses to start: these origins get CORS."""
    origins: dict[str, str] = {}
    for item in (raw or "").split(","):
        if not item.strip():
            continue
        app, sep, origin = (part.strip() for part in item.partition("="))
        if not sep or app not in FRONTEND_APPS or app in origins or not _BARE_HTTPS_ORIGIN.fullmatch(origin):
            raise ImproperlyConfigured(f"EXTRA_FRONTEND_ORIGINS: {item.strip()!r} is not <app>=https://<host> "
                                       f"with app one of {', '.join(FRONTEND_APPS)}, each at most once")
        origins[app] = origin
    return origins


EXTRA_FRONTEND_ORIGINS = _extra_frontend_origins(os.environ.get("EXTRA_FRONTEND_ORIGINS"))

#: This service's public address (the emailed links, the invitation's logo). Required outside
#: development: a guessed address in an email is worse than none (Flask's rule).
ACCOUNTS_API_URL = (os.environ.get("ACCOUNTS_API_URL") or os.environ.get("PETTY_CASH_URL") or "").strip().rstrip("/")
if not ACCOUNTS_API_URL and not DEVELOPMENT:
    raise ImproperlyConfigured("ACCOUNTS_API_URL is required and is not set")

#: minty-subscription-api, asked for the hub's subscription facts (core/subscription_client.py).
SUBSCRIPTION_API_URL = _url("SUBSCRIPTION_API_URL", "http://localhost:8001")

# ---- mail ------------------------------------------------------------------------------------
#: ``SMTP_URL`` (``minty_shared.config``: ``smtp://user:pass@host:587``, ``?tls=0``, ``?timeout=``).
#: Required outside development - unset is Django's console backend, which only prints, and a
#: production invitation must never quietly go nowhere.
_SMTP_URL = os.environ.get("SMTP_URL", "").strip()
if not _SMTP_URL and not DEVELOPMENT:
    raise ImproperlyConfigured("SMTP_URL is required and is not set")
globals().update(django_email(_SMTP_URL))
#: The invitation's From (Flask's ``INVITE_FROM``, required: a verified sender, never guessed).
INVITE_FROM = _required("INVITE_FROM")
#: Talk to Minty: where a message goes and who sends it. Optional at start, as in Flask: unset,
#: the contact route answers 500 and logs why.
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "").strip()
CONTACT_FROM = os.environ.get("CONTACT_FROM", "").strip()

# The root logger only: "minty-accounts-api" and Django's own loggers reach it by propagation
# (naming one here as well writes each of its lines twice).
LOGGING = logging_config(
    {"": None},
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
