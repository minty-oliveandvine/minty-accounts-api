"""What Django answers itself. Every other path falls through to Flask (core/fallthrough.py)."""

from django.http import JsonResponse
from django.urls import path

from hub import entities, public
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
    # Phase 5 (2026-11-06): the public routes, Flask's paths.
    path("auth/notices", public.auth_notices, name="auth_notices"),
    path("legal/current", public.legal_current, name="legal_current"),
    path("legal/invite-terms-status", public.invite_terms_status, name="invite_terms_status"),
    # Phase 5B: the hub's own routes (bearer, hub CORS).
    path("api/me/entities", entities.my_entities, name="my_entities"),
]
