"""The hub's door: how minty-web, minty-payment-request-web and minty-petty-cash-web call this
service across origins - Flask's ``blueprints/shared/hub_api.py`` and ``bearer_api.py`` (Phase 5 of
the Flask retirement, 2026-11-06).

- **The token:** a JWT in ``Authorization`` (``minty_shared.tokens.decode_bearer``: HS256, 60 s
  skew), with NO scope check. A module token and an onboarding token both pass, as in Flask. Its
  ``entity_id`` claim is ignored: a company travels as ``?entity=`` and is checked against
  membership by each route.
- **The person** must exist and be ``approved``; otherwise 401 ``{"error": "unauthorized"}``, which
  minty-web answers by fetching a fresh token.
- **CORS:** the request's ``Origin`` is echoed when it is one of the three front ends, an
  ``EXTRA_FRONTEND_ORIGINS`` entry for them, or a preview-set host for them; otherwise minty-web's
  (the browser then refuses the stranger). ``Vary: Origin``, no credentials. Every refusal carries
  the headers too: a bare 401 reads to the browser as a CORS failure, not a lapsed token.
- **A crash** in a hub route (``HubErrorMiddleware``) is logged with its traceback and answers 500
  ``{"error": HOUSE}`` with the headers: Django's bare 500 has none, so the browser couldn't read it
  and minty-web said "I couldn't reach the server" (2026-11-13).
"""

from __future__ import annotations

import logging
import re
import uuid

import jwt
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from minty_db.models import User
from minty_shared import logctx, tokens

from core.flask_json import flask_json

#: Everything the hub's routes answer between them (a preflight for any other is refused).
METHODS = "GET, POST, PATCH, DELETE, OPTIONS"
ALLOWED_HEADERS = "Authorization, Content-Type"
HUB_APPS = ("my", "payment-request", "pettycash")

#: A preview-set host: ``https://pr-<slug>-<app>.dailyminty.com`` (Flask's ``_SET_ORIGIN``).
_SET_ORIGIN = re.compile(
    r"https://pr-([a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?)-(my|payment-request|onboarding|pettycash)\.dailyminty\.com"
)


def preview_app(origin: str) -> str | None:
    """The app of a preview-set origin the configured regex allows, else None."""
    pattern = settings.PREVIEW_ORIGIN
    if pattern is None or not pattern.fullmatch(origin):
        return None
    match = _SET_ORIGIN.fullmatch(origin)
    return match.group(2) if match else None


def cors_origin(request: HttpRequest, configured: tuple[str, ...], apps: tuple[str, ...]) -> str:
    """Flask's ``preview_origin.cors_origin``."""
    origin = (request.headers.get("Origin") or "").rstrip("/")
    extra = settings.EXTRA_FRONTEND_ORIGINS
    if origin in configured or origin in {extra[a] for a in apps if a in extra}:
        return origin
    return origin if preview_app(origin) in apps else configured[0]


def cors(request: HttpRequest, response: HttpResponse) -> HttpResponse:
    configured = (settings.MINTY_WEB_URL, settings.PAYMENT_REQUEST_WEB_URL, settings.PETTY_CASH_WEB_URL)
    response["Access-Control-Allow-Origin"] = cors_origin(request, configured, HUB_APPS)
    response["Vary"] = "Origin"
    response["Access-Control-Allow-Methods"] = METHODS
    response["Access-Control-Allow-Headers"] = ALLOWED_HEADERS
    return response


def respond(request: HttpRequest, payload, status: int = 200) -> HttpResponse:
    return cors(request, flask_json(payload, status))


def refuse(request: HttpRequest, error: str, status: int) -> HttpResponse:
    """A sentence minty-web shows as it stands (``unauthorized`` makes it fetch a fresh token)."""
    return respond(request, {"error": error}, status)


#: The house sentence for a crash (minty-web's ``HOUSE_FALLBACK``).
HOUSE = "Something went wrong on my end. Mind trying again?"
#: Every hub route lives under this prefix (config/urls.py, Phase 5B-5C).
HUB_PREFIX = "/api/me/"

logger = logging.getLogger("minty-accounts-api")


class HubErrorMiddleware:
    """A hub route's unhandled exception: logged with its traceback, answered with CORS."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        return self.get_response(request)

    def process_exception(self, request: HttpRequest, exception: Exception) -> HttpResponse | None:
        if not request.path.startswith(HUB_PREFIX):
            return None
        logger.exception("hub route %s %s failed", request.method, request.path)
        return refuse(request, HOUSE, 500)


def bearer_claims(request: HttpRequest) -> dict | None:
    """Every claim of a valid JWT in ``Authorization``, or None (Flask's ``claims_from_bearer``).
    Every failure is None: telling them apart would tell a forger what to fix."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        claims = tokens.decode_bearer(header[len("Bearer "):].strip(), settings.SECRET_KEY)
    except jwt.PyJWTError:
        return None
    logctx.bind(user=claims.get("user_id"))
    return claims


def bearer_user_id(request: HttpRequest) -> str | None:
    claims = bearer_claims(request)
    user_id = claims.get("user_id") if claims else None
    return str(user_id) if user_id else None


def guard(request: HttpRequest) -> tuple[HttpResponse | None, User | None]:
    """``(early_response, user)``: the preflight, then a valid token naming an approved person."""
    if request.method == "OPTIONS":
        return cors(request, HttpResponse(status=204)), None
    user_id = bearer_user_id(request)
    user = User.objects.filter(id=user_id).first() if user_id and _is_uuid(user_id) else None
    if user is None or not user.approved:
        return refuse(request, "unauthorized", 401), None
    return None, user


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
