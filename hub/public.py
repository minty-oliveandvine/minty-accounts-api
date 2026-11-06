"""The routes nobody signs in for - Flask's ``auth/routes/home.py`` ``auth_notices`` and
``legal/routes/consent_status.py`` (Phase 5 of the Flask retirement, 2026-11-06).

- ``GET /auth/notices?flash=``: the messages a redirect to the sign-in page carried.
- ``GET /legal/current``: which Terms and Privacy versions are live (minty-web shows their text).
- ``POST /legal/invite-terms-status`` ``{invite}``: does the person an invitation went to still owe
  a Terms acceptance? Keyed on the invitation's secret token, never an email (an email-keyed
  question would tell anyone which addresses have accounts), and it fails SAFE: anything unclear
  answers "still required".

CORS as Flask's app-wide flask-cors default gave them: the request's ``Origin`` echoed, any method
and header, no credentials.
"""

from __future__ import annotations

import json
import logging

from django.db.models import Q
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed
from minty_db.models import Invitation, TermsConsent, User

from core.flask_json import flask_json
from hub import registry
from hub.notices import read_notices

logger = logging.getLogger("minty-accounts-api")

FLASK_CORS_METHODS = "DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT"


def _open_cors(request: HttpRequest, response: HttpResponse) -> HttpResponse:
    origin = request.headers.get("Origin")
    if origin:
        response["Access-Control-Allow-Origin"] = origin
        response["Vary"] = "Origin"
    if request.method == "OPTIONS":
        response["Access-Control-Allow-Methods"] = FLASK_CORS_METHODS
        asked = request.headers.get("Access-Control-Request-Headers")
        if asked:
            response["Access-Control-Allow-Headers"] = asked
    return response


def _preflight(request: HttpRequest) -> HttpResponse | None:
    return _open_cors(request, HttpResponse(status=200)) if request.method == "OPTIONS" else None


def auth_notices(request: HttpRequest) -> HttpResponse:
    if (early := _preflight(request)) is not None:
        return early
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    return _open_cors(request, flask_json({"notices": read_notices(request.GET.get("flash"))}))


def legal_current(request: HttpRequest) -> HttpResponse:
    if (early := _preflight(request)) is not None:
        return early
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    entry = registry.get_current(registry.TERMS)
    return _open_cors(request, flask_json({
        "terms_version": registry.current_version(registry.TERMS),
        "privacy_version": registry.current_version(registry.PRIVACY),
        # The manifest's date; null for a version with none, never a default.
        "effective_date": entry.effective_date if entry else None,
    }))


def _user_by_email(address: str | None) -> User | None:
    """Flask's ``resolve_user_by_email``: either identity column, case-insensitively."""
    normalized = (address or "").strip().lower()
    if not normalized:
        return None
    return User.objects.filter(Q(email__iexact=normalized) | Q(xero_email__iexact=normalized)).first()


def invite_terms_status(request: HttpRequest) -> HttpResponse:
    if (early := _preflight(request)) is not None:
        return early
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    required = True
    try:
        body = json.loads(request.body or b"null")
    except (ValueError, UnicodeDecodeError):
        body = None
    token = str((body if isinstance(body, dict) else {}).get("invite") or "").strip()
    if token:
        try:
            invitation = Invitation.objects.filter(token=token, status="pending").first()
            user = _user_by_email(invitation.email) if invitation is not None else None
            if user is not None:
                version = registry.current_version(registry.TERMS)
                required = not TermsConsent.objects.filter(user_id=user.id, terms_version=version).exists()
        except Exception:  # a database blip keeps the tick box, never waves someone through
            logger.exception("invite-terms-status lookup failed; treating Terms as required")
    return _open_cors(request, flask_json({"terms_required": required,
                                           "terms_version": registry.current_version(registry.TERMS)}))
