"""``POST /api/internal/xero/token`` - Flask's route of the same path, moved (Phase 2, 2026-11-05).

The caller is a Minty backend, not a browser: it presents a short-lived HS256 assertion signed
with the shared SECRET_KEY, scoped ``xero-access-token``. The company comes from the signed
claims, never the body, so a leaked assertion cannot be pointed at another company.

The answers are Flask's ``jsonify`` byte for byte (sorted keys, compact, a trailing newline): the
callers' ``main`` branches read them today.

SECURITY: this hands out live Xero access tokens; nothing but another Minty backend should reach it.
"""

from __future__ import annotations

import logging
import uuid

import jwt
from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, JsonResponse
from minty_shared import logctx, tokens

from core.flask_json import flask_json
from xero.services.sync import sync_company
from xero.services.tokens import resolve_entity_access_token

logger = logging.getLogger("minty-accounts-api")

SCOPE = "xero-access-token"
SYNC_SCOPE = "xero-sync"


def _authenticate(request: HttpRequest, scope: str = SCOPE) -> tuple[dict | None, str | None]:
    """``(claims, None)`` for a valid assertion for ``scope`` (each route has its own), else
    ``(None, reason)``."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None, "missing bearer token"
    try:
        claims = tokens.decode_bearer(header[7:].strip(), settings.SECRET_KEY,
                                      options={"require": ["exp", "scope", "entity_id"]})
    except jwt.ExpiredSignatureError:
        return None, "token expired"
    except jwt.InvalidTokenError as exc:
        return None, f"invalid token: {exc}"
    if claims.get("scope") != scope:
        return None, "wrong scope"
    # A Minty backend's own assertion: its company (and person, when it names one) go on the
    # log line, with the caller's trace.
    logctx.bind(user=claims.get("user_id"), entity=claims["entity_id"])
    return claims, None


def internal_xero_access_token(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    claims, error = _authenticate(request)
    if error:
        logger.warning("internal xero token: rejected caller (%s)", error)
        return flask_json({"status": "error", "message": "unauthorized"}, 401)

    raw_entity_id = claims["entity_id"]
    logger.info("internal xero token: request for entity %s", raw_entity_id)
    try:
        entity_id = uuid.UUID(str(raw_entity_id))
    except ValueError:
        # Signed by a Minty backend, so not an attack - a caller bug. No such company.
        logger.error("internal xero token: entity_id %r is not a uuid", raw_entity_id)
        result = None
    else:
        result = resolve_entity_access_token(entity_id)
    if not result:
        # Not a server error: the connection needs a person to reconnect, or a concurrent refresh
        # is still in flight. Callers show a reconnect prompt rather than send Xero a dead token.
        logger.info("internal xero token: no usable token for entity %s", raw_entity_id)
        return flask_json({"status": "reconnect_required",
                           "message": "No valid Xero token for this entity"}, 409)
    access_token, xero_org_id = result
    logger.info("internal xero token: issued for entity %s", raw_entity_id)
    return flask_json({"access_token": access_token, "xero_org_id": xero_org_id}, 200)


def internal_xero_sync(request: HttpRequest) -> HttpResponse:
    """``POST /api/internal/xero/sync`` (Phase 3, 2026-11-06): bring the company's Xero accounts and
    contacts into Minty's tables now (``xero/services/sync.py``) - the one sync, asked for by
    Petty Cash Settings (and, as their routes move, onboarding and Payment Requests).

    The caller's 60-second assertion is scoped ``xero-sync``; the company is its ``entity_id``. 200
    ``{"accounts": counts | null, "contacts": counts | null}`` - null when Xero could not be asked
    for that half (nothing changed); 409 ``{"error", "reconnect": true}`` when there is no usable
    token; 401 for a bad assertion.
    """
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    claims, error = _authenticate(request, SYNC_SCOPE)
    if error:
        logger.warning("internal xero sync: rejected caller (%s)", error)
        return JsonResponse({"error": "Unauthorized"}, status=401)
    try:
        entity_id = uuid.UUID(str(claims["entity_id"]))
    except ValueError:
        logger.error("internal xero sync: entity_id %r is not a uuid", claims["entity_id"])
        return JsonResponse({"error": "Xero needs to be connected again.", "reconnect": True}, status=409)
    token = resolve_entity_access_token(entity_id)
    if not token:
        return JsonResponse({"error": "Xero needs to be connected again.", "reconnect": True}, status=409)
    access_token, xero_org_id = token
    return JsonResponse(sync_company(entity_id, access_token, xero_org_id))
