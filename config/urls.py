"""What Django answers itself. Every other path falls through to Flask (core/fallthrough.py)."""

from django.http import JsonResponse
from django.urls import path

from xero.views import internal_xero_access_token, internal_xero_sync


def health(request):
    """Liveness only: never a call to Flask (its own /health is behind us) or the database."""
    return JsonResponse({"status": "ok", "service": "minty-accounts-api"})


urlpatterns = [
    path("health", health, name="health"),
    # Phase 2 (2026-11-05): Flask's path, moved. The other backends call it at the same address.
    path("api/internal/xero/token", internal_xero_access_token, name="internal_xero_access_token"),
    # Phase 3 (2026-11-06): the one Xero account/contact sync, new (Flask's ran in each caller).
    path("api/internal/xero/sync", internal_xero_sync, name="internal_xero_sync"),
]
