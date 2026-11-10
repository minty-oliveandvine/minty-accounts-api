"""The onboarding wizard's routes that live here - Flask's ``entity/routes/create.py``,
``services/modules.py``, ``onboarding_invites.py`` and ``onboarding_xero.py`` (Phase 5 of the Flask
retirement, 2026-11-06). minty-onboarding-api forwards its caller's own token to them unchanged;
minty-subscription-api's "invite an admin" forwards to ``/invite``.

- **The door** is Flask's onboarding one: any valid Minty token (no scope check). A missing or bad
  token is 401 ``{"error": "Unauthorized"}``. CORS answers the onboarding app (``ONBOARDING_WEB_URL``,
  its ``EXTRA_FRONTEND_ORIGINS`` entry or its preview sets) with ``GET, POST, PUT, OPTIONS``.
  Each route then checks the company it is given, by permission.
- ``POST /api/onboarding/modules`` ``{entity_id, modules: [...]}`` (or the older ``module``):
  the chosen modules on and the others off, one ``entity_function_map`` row each. It answers
  ``{"modules": {code: bool}}``. Only an approved member may choose; Flask accepted any membership
  row, approved or not (tightened here on purpose).
- ``GET`` / ``POST /api/onboarding/invite``, ``POST /invite/resend`` and ``/invite/cancel``: the
  invitations (``hub/invitations.py``), with Flask's onboarding sentences.
- ``POST /api/onboarding/xero/disconnect`` ``{entity_id}``: onboarding's own disconnect (the
  company stays in ``onboarding``). ``/xero/release`` frees ANOTHER company's organisation with the
  shared disconnect, in development only.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from minty_db.models import Entity, EntityFunction, EntityFunctionMap, Invitation, User, UserEntity

from core.flask_json import flask_json
from hub import invitations
from hub.company import held_elsewhere_sentence
from hub.policy import Permission, can_manage_role_assignment_for_entity, has_permission
from hub.rules import invite_address_error
from hub.transport import bearer_user_id, cors_origin
from xero.services.disconnect import disconnect_entity_from_xero, disconnect_onboarding_entity

logger = logging.getLogger("minty-accounts-api")

METHODS = "GET, POST, PUT, OPTIONS"
MODULE_CODES = ("PETTY_CASH", "PAYMENT_REQUEST")
ASSIGNABLE = {"cashier", "shop_manager", "accountant", "admin"}


def _respond(request: HttpRequest, payload, status: int = 200) -> HttpResponse:
    response = flask_json(payload, status) if payload is not None else HttpResponse(status=status)
    response["Access-Control-Allow-Origin"] = cors_origin(request, (settings.ONBOARDING_WEB_URL,), ("onboarding",))
    response["Vary"] = "Origin"
    response["Access-Control-Allow-Methods"] = METHODS
    response["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    return response


def _door(request: HttpRequest, methods: tuple[str, ...]):
    """``(early, user_id)``: the preflight, the method, a valid token."""
    if request.method == "OPTIONS":
        return _respond(request, None, 204), None
    if request.method not in methods:
        return _respond(request, {"error": "Method not allowed"}, 405), None
    user_id = bearer_user_id(request)
    if not user_id:
        return _respond(request, {"error": "Unauthorized"}, 401), None
    return None, user_id


def _body(request: HttpRequest) -> dict:
    try:
        data = json.loads(request.body or b"null")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _uuid(value) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _user(user_id: str) -> User | None:
    return User.objects.filter(id=user_id).first() if _uuid(user_id) else None


def _may(user_id: str, permission: Permission, entity_id) -> bool:
    user = _user(user_id)
    return bool(user is not None and _uuid(entity_id) and has_permission(user, permission, str(entity_id)))


# ---- modules ----------------------------------------------------------------------------------


def _module_state(entity_id: str) -> dict[str, bool]:
    catalogue = {str(fn.id): fn.function_code for fn in EntityFunction.objects.filter(function_code__in=MODULE_CODES)}
    state = dict.fromkeys(MODULE_CODES, False)
    for row in EntityFunctionMap.objects.filter(entity_id=entity_id, entity_function_id__in=list(catalogue)):
        state[catalogue[str(row.entity_function_id)]] = bool(row.is_enabled)
    return state


def apply_module_selections(entity_id: str, selected: set[str], user_id: str) -> tuple[dict, int]:
    """Each selected module on, every other one off (Flask's ``apply_module_selections``)."""
    unknown = sorted(selected - set(MODULE_CODES))
    if unknown:
        return {"error": f"Unknown module code(s): {unknown}. Expected one of {list(MODULE_CODES)}."}, 400
    catalogue = {fn.function_code: fn for fn in EntityFunction.objects.filter(function_code__in=MODULE_CODES)}
    missing = [code for code in MODULE_CODES if code not in catalogue]
    if missing:
        return {"error": f"Module catalog missing rows for: {missing}. Run the seed migration."}, 500
    now = datetime.now(UTC)
    for code in MODULE_CODES:
        enabled = code in selected
        rows = EntityFunctionMap.objects.filter(entity_id=entity_id, entity_function_id=catalogue[code].id)
        row = rows.first()
        if row is None:
            EntityFunctionMap.objects.create(entity_id=entity_id, entity_function_id=catalogue[code].id,
                                             is_enabled=enabled, enabled_at=now if enabled else None,
                                             disabled_at=None if enabled else now, created_by=user_id)
        elif bool(row.is_enabled) != enabled:
            rows.update(is_enabled=enabled, **({"enabled_at": now} if enabled else {"disabled_at": now}))
    return {"modules": _module_state(entity_id)}, 200


def onboarding_modules(request: HttpRequest) -> HttpResponse:
    early, user_id = _door(request, ("POST",))
    if early is not None:
        return early
    payload = _body(request)
    entity_id = str(payload.get("entity_id") or "").strip()
    listed = payload.get("modules")
    if isinstance(listed, list):
        selected = [str(m).strip().upper() for m in listed if m is not None and str(m).strip()]
    else:
        legacy = str(payload.get("module") or "").strip().upper()
        selected = [legacy] if legacy else []
    if not entity_id:
        return _respond(request, {"error": "entity_id is required"}, 400)
    if not selected:
        return _respond(request, {"error": "at least one module is required"}, 400)
    if not (_uuid(entity_id) and _uuid(user_id)
            and UserEntity.objects.filter(user_id=user_id, entity_id=entity_id, approved=True).exists()):
        return _respond(request, {"error": "You don't have access to this entity"}, 403)
    data, status = apply_module_selections(entity_id, set(selected), user_id)
    logger.info("onboarding modules entity=%s actor=%s status=%s modules=%s", entity_id, user_id, status,
                data.get("modules"))
    return _respond(request, data, status)


# ---- invitations ------------------------------------------------------------------------------


def _invite_payload(inv: Invitation) -> dict:
    return {"id": str(inv.id), "email": inv.email, "role": str(inv.role), "status": str(inv.status),
            "first_name": inv.first_name or "", "last_name": inv.last_name or "",
            "created_at": inv.created_at.isoformat() if inv.created_at else None,
            "expires_at": inv.expires_at.isoformat() if inv.expires_at else None,
            "expired": inv.expires_at is not None and datetime.now(UTC) > inv.expires_at}


def onboarding_invite(request: HttpRequest) -> HttpResponse:
    early, user_id = _door(request, ("GET", "POST"))
    if early is not None:
        return early
    if request.method == "GET":
        entity_id = (request.GET.get("entity_id") or "").strip()
        if not entity_id:
            return _respond(request, {"error": "entity_id is required"}, 400)
        if not _may(user_id, Permission.USER_VIEW_ALL, entity_id):
            return _respond(request, {"error": "Access denied"}, 403)
        return _respond(request, {"invitations": [_invite_payload(i) for i in invitations.pending_invitations(entity_id)]})
    payload = _body(request)
    entity_id = str(payload.get("entity_id") or "").strip()
    if not entity_id:
        return _respond(request, {"error": "entity_id is required"}, 400)
    if not _may(user_id, Permission.USER_INVITE, entity_id):
        return _respond(request, {"error": "You do not have permission to invite users to this entity."}, 403)
    email = str(payload.get("email") or "").strip().lower()
    role = str(payload.get("role") or "").strip().lower().replace(" ", "_").replace("-", "_")
    first_name = str(payload.get("first_name") or "").strip()
    last_name = str(payload.get("last_name") or "").strip()
    if not email or not role:
        return _respond(request, {"error": "email and role are required."}, 400)
    if problem := invite_address_error(email):
        return _respond(request, {"error": problem}, 400)
    if role not in ASSIGNABLE:
        return _respond(request, {"error": "Invalid role."}, 400)
    user = _user(user_id)
    if not can_manage_role_assignment_for_entity(user, role, entity_id):
        return _respond(request, {"error": "You cannot assign a role higher than your own."}, 403)
    invitation, error = invitations.create_invitation(entity_id, email, role, user.id, first_name, last_name)
    if error:
        return _respond(request, {"error": error}, 409)
    sent = invitations.send_invitation_email(invitation, first_name=first_name, last_name=last_name)
    logger.info("onboarding invitation actor=%s entity=%s invitation=%s email_sent=%s", user_id, entity_id,
                invitation.id, sent)
    return _respond(request, {"status": "success", "email_sent": sent, "invitation": _invite_payload(invitation)}, 201)


def _invitation_to_manage(request, user_id: str, verb: str):
    """``(early, invitation)`` for resend and cancel: USER_INVITE on its company and its role."""
    invitation_id = str(_body(request).get("invitation_id") or "").strip()
    if not invitation_id:
        return _respond(request, {"error": "invitation_id is required"}, 400), None
    inv = Invitation.objects.filter(id=invitation_id).first() if _uuid(invitation_id) else None
    if inv is None:
        return _respond(request, {"error": "Invitation not found."}, 404), None
    if not _may(user_id, Permission.USER_INVITE, inv.entity_id):
        return _respond(request, {"error": "Not authorized."}, 403), None
    if not can_manage_role_assignment_for_entity(_user(user_id), str(inv.role), str(inv.entity_id)):
        return _respond(request, {"error": f"You cannot {verb} an invitation for a role equal to or higher than "
                                           "your own."}, 403), None
    return None, inv


def onboarding_invite_resend(request: HttpRequest) -> HttpResponse:
    early, user_id = _door(request, ("POST",))
    if early is not None:
        return early
    early, inv = _invitation_to_manage(request, user_id, "resend")
    if early is not None:
        return early
    inv, error, retry_after = invitations.resend_invitation(inv.id, actor_id=user_id)
    if error:
        return _respond(request, {"error": error, "retry_after": retry_after}, 429 if retry_after > 0 else 400)
    if not invitations.send_invitation_email(inv):
        # The token rotated already: the old link is dead and the new one did not go out.
        logger.error("onboarding invitation resend: email failed invitation=%s", inv.id)
        return _respond(request, {"error": "I couldn't send that email just now. Mind trying again in a moment?",
                                  "retry_after": invitations.resend_cooldown_remaining(inv)}, 502)
    return _respond(request, {"status": "success", "invitation": _invite_payload(inv),
                              "resend_cooldown": invitations.resend_cooldown_remaining(inv)})


def onboarding_invite_cancel(request: HttpRequest) -> HttpResponse:
    early, user_id = _door(request, ("POST",))
    if early is not None:
        return early
    early, inv = _invitation_to_manage(request, user_id, "cancel")
    if early is not None:
        return early
    ok, error = invitations.cancel_invitation(inv.id)
    if not ok:
        return _respond(request, {"error": error}, 400)
    return _respond(request, {"status": "success"})


# ---- Xero -------------------------------------------------------------------------------------


def _xero_company(request: HttpRequest):
    """``(early, user_id, entity)``: the door, ``{entity_id}``, XERO_SETTINGS_UPDATE on it."""
    early, user_id = _door(request, ("POST",))
    if early is not None:
        return early, None, None
    entity_id = str(_body(request).get("entity_id") or "").strip()
    if not entity_id:
        return _respond(request, {"error": "entity_id is required"}, 400), None, None
    if not _may(user_id, Permission.XERO_SETTINGS_UPDATE, entity_id):
        return _respond(request, {"error": "Access denied"}, 403), None, None
    org = Entity.objects.filter(id=entity_id, archived_at__isnull=True).first()
    if org is None:
        return _respond(request, {"error": "Entity not found"}, 404), None, None
    return None, user_id, org


def onboarding_xero_disconnect(request: HttpRequest) -> HttpResponse:
    early, user_id, org = _xero_company(request)
    if early is not None:
        return early
    try:
        disconnect_onboarding_entity(org.id)
    except Exception:
        logger.exception("onboarding disconnect failed entity=%s actor=%s", org.id, user_id)
        return _respond(request, {"error": "Failed to disconnect from Xero. Please try again."}, 500)
    logger.info("onboarding disconnect: entity %s disconnected actor=%s", org.id, user_id)
    return _respond(request, {"ok": True, "connected": False})


def onboarding_xero_release(request: HttpRequest) -> HttpResponse:
    """Free the organisation ANOTHER company holds (the body's ``entity_id``, authorized on itself)."""
    early, user_id, org = _xero_company(request)
    if early is not None:
        return early
    if not settings.DEVELOPMENT:
        return _respond(request, {"error": "Moving a Xero organisation between companies isn't available. "
                                           + held_elsewhere_sentence(org.name)}, 403)
    if not org.xero_org_id or org.status == "disconnected":  # a kept organisation is no claim
        logger.info("onboarding release: entity %s already free", org.id)
        return _respond(request, {"ok": True, "released": False, "entity_name": org.name})
    try:
        disconnect_entity_from_xero(org.id)
    except Exception:
        logger.exception("onboarding release failed entity=%s actor=%s", org.id, user_id)
        return _respond(request, {"error": f'I could not disconnect "{org.name}" from Xero, so the organisation is '
                                           "still in use there. Mind trying again?"}, 502)
    logger.info("onboarding release: entity %s disconnected actor=%s", org.id, user_id)
    return _respond(request, {"ok": True, "released": True, "entity_name": org.name})
