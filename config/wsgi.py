"""The WSGI entry point: Django for the paths it routes, the fall-through to Flask for the rest."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

django_application = get_wsgi_application()

from django.conf import settings  # noqa: E402  (after Django is set up)

from core.fallthrough import make_application  # noqa: E402

application = make_application(django_application, settings.FLASK_FALLBACK_URL)
