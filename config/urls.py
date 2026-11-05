"""What Django answers itself. Every other path falls through to Flask (core/fallthrough.py)."""

from django.http import JsonResponse
from django.urls import path


def health(request):
    """Liveness only: no database, and never a call to Flask (its own /health is behind us)."""
    return JsonResponse({"status": "ok", "service": "minty-accounts-api"})


urlpatterns = [
    path("health", health, name="health"),
]
