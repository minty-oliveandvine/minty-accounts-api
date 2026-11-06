"""The WSGI entry point. Django answers every path; the fall-through to Flask was removed when
Flask stopped being used (Phase 7, 2026-11-06): a path it does not route is a 404."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()
