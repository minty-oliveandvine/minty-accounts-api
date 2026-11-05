"""minty-accounts-api - Minty's accounts service, on Flask's public address.

Phase 1 of the Flask retirement (2026-11-05): Django owns ``/health`` only; every other request
falls through to Flask unchanged (``core/fallthrough.py``, mounted in ``config/wsgi.py``). Later
phases move Flask's routes in, one group at a time. No database yet: minty-db comes with the
first ported route.
"""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv
from minty_shared.logctx import logging_config

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

INSTALLED_APPS: list[str] = []
MIDDLEWARE: list[str] = []
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DATABASES: dict = {}
USE_TZ = True
TIME_ZONE = "UTC"

# The root logger only: "minty-accounts-api" and Django's own loggers reach it by propagation
# (naming one here as well writes each of its lines twice).
LOGGING = logging_config(
    {"": None},
    level=os.environ.get("LOG_LEVEL", "INFO"),
)
