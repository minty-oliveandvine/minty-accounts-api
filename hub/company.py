"""A company's Users and Entity & Integration tabs on minty-web - Flask's
``entity/routes/hub_settings.py`` and ``user_management/services/roles.py`` (Phase 5 of the Flask
retirement, 2026-11-06).

Every route is the hub's door (``guard``) plus ``?entity=`` (the person must see the company:
``entity_context``) and the permission it asks for (403 with Flask's sentence).

- ``GET /api/me/company/users``: approved members, pending invitations, the roles this person may
  give, who pays (minty-subscription-api; not knowing reads as nobody, for display only).
- ``POST /api/me/company/invitations`` and ``.../<id>/cancel`` / ``/resend``: invitations
  (``hub/invitations.py``), never for a role above the person's own.
- ``PATCH`` / ``DELETE /api/me/company/users/<id>``: a role change or a removal, after Flask's
  guards: may you touch this person, then the payer (refused while they pay: removal, or a demotion
  out of admin), the last admin, and a handover offered to them. The payer guards fail CLOSED - when
  minty-subscription-api can't answer, the change is refused (503) - while the handover guard fails
  OPEN, as Flask documents it (accept re-checks everything).
- ``GET`` / ``PATCH /api/me/company/integration``: the name, country, currency and Xero state. The
  Xero state is checked live (Xero's /connections, the connector's token from this service's own
  token service) and the organisation name filled in when missing.
- ``POST /api/me/company/xero/disconnect`` and ``/release``: the shared disconnect
  (``xero/services/disconnect.py``) of this company, or of the one holding the organisation it
  was refused (development only).
"""

from __future__ import annotations

import json
import logging
import uuid

import requests
from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from minty_db.models import CountryInfo, CurrencyInfo, Entity, Invitation, User, UserEntity

from core import subscription_client
from hub import invitations
from hub.cash_settings import sync_cash_settings
from hub.entities import _modules
from hub.notices import read_conflict, read_notices
from hub.policy import (
    Permission,
    Role,
    can_manage_role_assignment_for_entity,
    has_permission,
    role_at_least,
)
from hub.profile import entity_context, initials
from hub.rules import invite_address_error, role_label
from hub.transport import guard, refuse, respond
from xero.services.disconnect import disconnect_entity_from_xero
from xero.services.tokens import resolve_entity_access_token

logger = logging.getLogger("minty-accounts-api")

SAVE_FAILED = "That didn't quite save. Mind trying again?"
NO_COMPANY = "You don't have access to that company."
#: The roles a person may be given here, in the dropdown's order.
ASSIGNABLE = ("admin", "accountant", "shop_manager", "cashier")
NO_SUBSCRIPTION_ANSWER = ("I couldn't check this company's subscription just now, so nothing was changed. "
                          "Mind trying again in a moment?")
CONNECTIONS_URL = "https://api.xero.com/connections"


def _open(request: HttpRequest, permission: Permission, denied: str):
    """``(early, user, entity_id)``: the door, the company from ``?entity=``, the permission."""
    early, user = guard(request)
    if early is not None:
        return early, None, None
    entity_id = (request.GET.get("entity") or "").strip()
    company = entity_context(user, entity_id) if entity_id else None
    if company is None:
        return refuse(request, NO_COMPANY, 403), None, None
    if not has_permission(user, permission, company["id"]):
        return refuse(request, denied, 403), None, None
    return None, user, company["id"]


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


# ---- Users ------------------------------------------------------------------------------------


def _users_page(user: User, entity_id: str) -> dict:
    org = Entity.objects.get(id=entity_id)
    rows = (UserEntity.objects.filter(entity_id=entity_id, approved=True).select_related("user")
            .order_by("user__first_name", "user__last_name", "user__email"))
    try:
        payer = subscription_client.entity_facts(entity_id).get("payer")
    except subscription_client.SubscriptionUnavailable:
        payer = None  # display only: the badge is missing, no change is allowed on it
    can_assign = has_permission(user, Permission.USER_ROLE_ASSIGN, entity_id)
    can_delete = has_permission(user, Permission.USER_ROLE_DELETE, entity_id)
    can_invite = has_permission(user, Permission.USER_INVITE, entity_id)

    def member(row) -> dict:
        person, role = row.user, str(row.role or "")
        may_touch = can_manage_role_assignment_for_entity(user, role, entity_id)
        return {"id": str(person.id), "first_name": person.first_name or "", "last_name": person.last_name or "",
                "email": person.email or person.username or "", "initials": initials(person), "role": role,
                "role_label": role_label(role), "subscriber": payer is not None and str(payer) == str(person.id),
                "is_you": person.id == user.id, "can_change_role": can_assign and may_touch,
                "can_remove": can_delete and may_touch}

    def invitation(inv: Invitation) -> dict:
        role = str(inv.role or "")
        return {"id": str(inv.id), "email": inv.email, "first_name": inv.first_name or "",
                "last_name": inv.last_name or "", "role": role, "role_label": role_label(role),
                "created_at": inv.created_at.isoformat() if inv.created_at else None,
                "resend_cooldown": invitations.resend_cooldown_remaining(inv),
                "can_manage": can_invite and can_manage_role_assignment_for_entity(user, role, entity_id)}

    return {
        "company": {"id": str(org.id), "name": org.name},
        "modules": sorted(_modules([org.id]).get(str(org.id), set())),
        "members": [member(row) for row in rows],
        "invitations": [invitation(inv) for inv in invitations.pending_invitations(entity_id)],
        "roles": [{"value": r, "label": role_label(r)} for r in ASSIGNABLE
                  if can_manage_role_assignment_for_entity(user, r, entity_id)],
        "can_invite": can_invite,
    }


def company_users(request: HttpRequest) -> HttpResponse:
    early, user, entity_id = _open(request, Permission.USER_VIEW_ALL,
                                   "You don't have permission to see this company's users.")
    if early is not None:
        return early
    return respond(request, {**_users_page(user, entity_id), "notices": read_notices(request.GET.get("flash"))})


def company_invite(request: HttpRequest) -> HttpResponse:
    early, user, entity_id = _open(request, Permission.USER_INVITE,
                                   "You don't have permission to invite people to this company.")
    if early is not None:
        return early
    data = _body(request)
    email = str(data.get("email") or "").strip().lower()
    role = str(data.get("role") or "").strip().lower()
    first_name = str(data.get("first_name") or "").strip()
    last_name = str(data.get("last_name") or "").strip()
    problem = invite_address_error(email) if email else "I need an email address to send the invitation to."
    if problem:
        return refuse(request, problem, 400)
    if role not in ASSIGNABLE:
        return refuse(request, "Pick one of the roles offered.", 400)
    if not first_name or not last_name:
        return refuse(request, "I need their first and last name.", 400)
    if len(first_name) > 100 or len(last_name) > 100:
        return refuse(request, "Please keep each name to 100 characters or fewer.", 400)
    if not can_manage_role_assignment_for_entity(user, role, entity_id):
        return refuse(request, "I can't let you give someone a role above your own.", 403)
    invitation, error = invitations.create_invitation(entity_id, email, role, user.id, first_name, last_name)
    if error:
        logger.info("hub invitation refused actor=%s entity=%s reason=%r", user.id, entity_id, error)
        return refuse(request, error, 409)
    sent = invitations.send_invitation_email(invitation, first_name=first_name, last_name=last_name)
    logger.info("hub invitation actor=%s entity=%s invitation=%s email_sent=%s", user.id, entity_id, invitation.id, sent)
    return respond(request, {
        "invitation_id": str(invitation.id), "email_sent": sent,
        "message": "Invitation sent." if sent else "The invitation is saved, but its email didn't go out - try Resend.",
    }, 201)


def _invitation_to_manage(request, user, entity_id, invitation_id, verb: str):
    inv = Invitation.objects.filter(id=invitation_id).first() if _uuid(invitation_id) else None
    if inv is None or str(inv.entity_id) != entity_id:
        return refuse(request, "Hmm, I couldn't find that invitation.", 404), None
    if not can_manage_role_assignment_for_entity(user, str(inv.role or ""), entity_id):
        return refuse(request, f"I can't let you {verb} an invitation for a role above your own.", 403), None
    return None, inv


def invitation_cancel(request: HttpRequest, invitation_id: str) -> HttpResponse:
    early, user, entity_id = _open(request, Permission.USER_INVITE,
                                   "You don't have permission to manage this company's invitations.")
    if early is not None:
        return early
    early, inv = _invitation_to_manage(request, user, entity_id, invitation_id, "cancel")
    if early is not None:
        return early
    ok, error = invitations.cancel_invitation(inv.id)
    if not ok:
        return refuse(request, error or SAVE_FAILED, 400)
    logger.info("hub invitation cancelled actor=%s entity=%s invitation=%s", user.id, entity_id, inv.id)
    return respond(request, {"message": "Invitation cancelled."})


def invitation_resend(request: HttpRequest, invitation_id: str) -> HttpResponse:
    early, user, entity_id = _open(request, Permission.USER_INVITE,
                                   "You don't have permission to manage this company's invitations.")
    if early is not None:
        return early
    early, inv = _invitation_to_manage(request, user, entity_id, invitation_id, "resend")
    if early is not None:
        return early
    inv, error, retry_after = invitations.resend_invitation(inv.id, actor_id=user.id)
    if error:
        return respond(request, {"error": error, "retry_after": retry_after}, 429 if retry_after > 0 else 400)
    if not invitations.send_invitation_email(inv):
        # The token already rotated: the old link is dead and the new one didn't go out.
        logger.error("hub invitation resend: email failed invitation=%s entity=%s", inv.id, entity_id)
        return respond(request, {"error": "I couldn't send that email just now. Mind trying again in a moment?",
                                 "retry_after": invitations.resend_cooldown_remaining(inv)}, 502)
    return respond(request, {"message": "Invitation sent again.",
                             "resend_cooldown": invitations.resend_cooldown_remaining(inv)})


# ---- one member: Flask's guards (roles.py) ----------------------------------------------------


class Refusal(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.message, self.status = message, status


def _may_touch(user, role: str, entity_id: str) -> None:
    if not can_manage_role_assignment_for_entity(user, role, entity_id):
        raise Refusal("I can't let you manage someone whose role matches or outranks your own.", 403)


def _facts(entity_id: str) -> dict:
    try:
        return subscription_client.entity_facts(entity_id)
    except subscription_client.SubscriptionUnavailable:
        raise Refusal(NO_SUBSCRIPTION_ANSWER, 503) from None


def _not_payer(member_id: str, entity_id: str) -> None:
    """Removal: ANY row naming them as payer refuses (a split state fails closed)."""
    if member_id in (_facts(entity_id).get("payers") or []):
        raise Refusal("I can't remove the person who pays for this company. "
                      "Move the subscription to someone else first.", 409)


def _not_pending_subscriber(member_id: str, entity_id: str) -> None:
    """A handover offered to them: open when unknown (Flask's documented choice)."""
    try:
        nominee = subscription_client.entity_facts(entity_id).get("pending_transfer_to")
    except subscription_client.SubscriptionUnavailable:
        logger.warning("roles: could not check for a pending handover on %s; allowing the change", entity_id)
        return
    if nominee and nominee == member_id:
        raise Refusal("This company's subscription is being handed over to them. "
                      "Cancel the handover first, then make the change.", 409)


def _not_last_admin(role: str, member_id: str, entity_id: str, action: str) -> None:
    """Never the company's only admin (approved, on a live account)."""
    if not role_at_least(role, Role.ADMIN.value):
        return
    others = [row for row in UserEntity.objects.filter(entity_id=entity_id, approved=True).exclude(user_id=member_id)
              if role_at_least(row.role, Role.ADMIN.value)]
    if others:
        deactivated = {str(u) for u in User.objects.filter(id__in=[r.user_id for r in others], approved=False)
                       .values_list("id", flat=True)}
        others = [row for row in others if str(row.user_id) not in deactivated]
    if not others:
        raise Refusal(f"I can't {action} the only admin — this company would have nobody who can manage it. "
                      "Make someone else an admin first.", 409)


def _role_change_allowed(current: str, new: str, member_id: str, entity_id: str) -> None:
    """Only a step DOWN out of admin can break what removal breaks."""
    if not (role_at_least(current, Role.ADMIN.value) and not role_at_least(new, Role.ADMIN.value)):
        return
    _not_pending_subscriber(member_id, entity_id)
    payer = _facts(entity_id).get("payer")
    if payer and payer == member_id:
        raise Refusal("I can't take admin away from the person who pays for this company. "
                      "Move the subscription to someone else first.", 409)
    _not_last_admin(current, member_id, entity_id, "demote")


def company_member(request: HttpRequest, member_id: str) -> HttpResponse:
    removing = request.method == "DELETE"
    if request.method not in ("PATCH", "DELETE", "OPTIONS"):
        return refuse(request, "Method not allowed", 405)
    early, user, entity_id = _open(
        request, Permission.USER_ROLE_DELETE if removing else Permission.USER_ROLE_ASSIGN,
        "You don't have permission to remove people from this company." if removing
        else "You don't have permission to change roles in this company.")
    if early is not None:
        return early
    member_id = _uuid(member_id)
    membership = UserEntity.objects.filter(user_id=member_id, entity_id=entity_id).first() if member_id else None
    if membership is None:
        return refuse(request, "I couldn't find that person on this entity.", 404)
    current = str(membership.role or "")
    try:
        if removing:
            _may_touch(user, current, entity_id)
            _not_payer(member_id, entity_id)
            _not_last_admin(current, member_id, entity_id, "remove")
            _not_pending_subscriber(member_id, entity_id)
            UserEntity.objects.filter(user_id=member_id, entity_id=entity_id).delete()
            logger.info("hub member removed actor=%s entity=%s member=%s", user.id, entity_id, member_id)
            return respond(request, {"message": "Removed from the company."})
        new = str(_body(request).get("role") or "").strip().lower()
        if new not in ASSIGNABLE:
            return refuse(request, "Pick one of the roles offered.", 400)
        if not can_manage_role_assignment_for_entity(user, new, entity_id):
            raise Refusal("I can't let you give someone a role above your own.", 403)
        _may_touch(user, current, entity_id)
        _role_change_allowed(current, new, member_id, entity_id)
    except Refusal as refusal:
        return refuse(request, refusal.message, refusal.status)
    UserEntity.objects.filter(user_id=member_id, entity_id=entity_id).update(role=new)
    logger.info("hub member role changed actor=%s entity=%s member=%s role=%s", user.id, entity_id, member_id, new)
    return respond(request, {"message": "Role saved.", "role": new, "role_label": role_label(new)})


# ---- Entity & Integration ---------------------------------------------------------------------


def _sync_connection(org: Entity, access_token: str) -> None:
    """The stored status reconciled with Xero's /connections (a grant revoked on Xero's site can
    leave the token working). Xero not answering keeps the stored status (logged)."""
    try:
        response = requests.get(CONNECTIONS_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=(10, 30))
    except requests.RequestException as exc:
        logger.warning("Entity & Integration: Xero connections unreachable for %s: %s", org.id, exc)
        return
    if response.status_code != 200:
        logger.warning("Entity & Integration: Xero connections answered %s for %s", response.status_code, org.id)
        return
    try:
        listed = any(c.get("tenantId") == str(org.xero_org_id) for c in response.json() or [])
    except (ValueError, AttributeError):
        return
    if listed and org.status != "connected":
        Entity.objects.filter(id=org.id).update(status="connected", last_connected_at=timezone.now())
    elif not listed and org.status != "disconnected":
        Entity.objects.filter(id=org.id).update(status="disconnected")


def _organisation_name(org: Entity, access_token: str) -> str | None:
    try:
        response = requests.get(f"{settings.XERO_API_BASE_URL}/Organisation", timeout=(10, 30), headers={
            "Authorization": f"Bearer {access_token}", "Xero-Tenant-Id": str(org.xero_org_id),
            "Accept": "application/json"})
        if response.status_code != 200:
            return None
        return ((response.json().get("Organisations") or [{}])[0] or {}).get("Name")
    except (requests.RequestException, ValueError, AttributeError) as exc:
        logger.warning("Entity & Integration: Xero organisation name not read for %s: %s", org.id, exc)
        return None


def _integration_page(user: User, entity_id: str) -> dict:
    org = Entity.objects.get(id=entity_id)
    token = resolve_entity_access_token(org.id) if org.xero_org_id else None
    if token:
        access_token, _org = token
        _sync_connection(org, access_token)
        org.refresh_from_db()
        if not org.xero_tenant_name and (name := _organisation_name(org, access_token)):
            Entity.objects.filter(id=org.id).update(xero_tenant_name=name)
            org.xero_tenant_name = name
    countries = CountryInfo.objects.filter(is_active=True) | CountryInfo.objects.filter(country_code=org.country_code)
    currencies = CurrencyInfo.objects.filter(is_active=True) | CurrencyInfo.objects.filter(id=org.currency_id)
    return {
        "company": {"id": str(org.id), "name": org.name, "country_code": org.country_code,
                    "currency_id": str(org.currency_id) if org.currency_id else None},
        "modules": sorted(_modules([org.id]).get(str(org.id), set())),
        "countries": [{"code": c.country_code, "name": c.country_name_en}
                      for c in countries.distinct().order_by("display_order", "country_name_en")],
        "currencies": [{"id": str(c.id), "name": c.currency_name} for c in currencies.distinct().order_by("currency_name")],
        "xero": {
            "status": str(org.status) if org.status is not None else None,
            "connected": bool(org.xero_org_id) and bool(token) and org.status == "connected",
            "needs_reconnect": bool(org.xero_org_id) and (not token or org.status == "disconnected"),
            "organisation": org.xero_tenant_name if org.xero_org_id else None,
            "last_connected_at": org.last_connected_at.isoformat() if org.last_connected_at else None,
        },
        "can_edit": has_permission(user, Permission.XERO_SETTINGS_UPDATE, entity_id),
        "can_rename": has_permission(user, Permission.ENTITY_RENAME, entity_id),
    }


def _conflict(user: User, entity_id: str, token: str | None) -> dict | None:
    """The company holding the organisation this company's connect was refused for, for "move it
    here" - re-checked now (still holding one, not this company), ``can_move`` only in development
    and on this person's own permission over the OTHER company."""
    held = read_conflict(token)
    if held is None or held["entity_id"] == entity_id:
        return None
    other = Entity.objects.filter(id=held["entity_id"]).first() if _uuid(held["entity_id"]) else None
    if other is None or not other.xero_org_id:
        return None
    return {"entity_id": str(other.id), "entity_name": other.name, "organisation": other.xero_tenant_name or None,
            "can_move": settings.DEVELOPMENT and has_permission(user, Permission.XERO_SETTINGS_UPDATE, str(other.id))}


def _save_integration(request: HttpRequest, user: User, entity_id: str, data: dict) -> HttpResponse | None:
    org = Entity.objects.get(id=entity_id)
    changed = []
    if "name" in data:
        name = str(data.get("name") or "").strip()
        if name != (org.name or ""):
            if not has_permission(user, Permission.ENTITY_RENAME, entity_id):
                return refuse(request, "Only an admin can rename the company.", 403)
            if not name:
                return refuse(request, "I need a name for this entity before I can save it.", 422)
            if len(name) > 100:
                return refuse(request, "That name goes on a bit! Please keep it to 100 characters or fewer.", 422)
            # Exact names, case and all, unique per person (owner, 2026-11-03).
            if Entity.objects.filter(name=name, id__in=UserEntity.objects.filter(user_id=user.id)
                                     .values("entity_id")).exclude(id=entity_id).exists():
                return refuse(request, f"You already have a company called “{name}”. Choose another name.", 422)
            org.name = name
            changed.append("name")
    country_code = str(data.get("country_code") or "").strip().upper()
    currency_id = str(data.get("currency_id") or "").strip()
    country = CountryInfo.objects.filter(country_code=country_code).first() if country_code else None
    if country_code and country is None:
        return refuse(request, "Pick one of the countries offered.", 422)
    currency = CurrencyInfo.objects.filter(id=currency_id).first() if currency_id and _uuid(currency_id) else None
    if currency_id and currency is None:
        return refuse(request, "Pick one of the currencies offered.", 422)
    if country is not None:
        org.country_code = country.country_code
        changed.append("country_code")
    if currency is None and country is not None and country.currency_id:
        currency = CurrencyInfo.objects.filter(id=country.currency_id).first()  # a country brings its own
    try:
        with transaction.atomic():
            if currency is not None:
                org.currency_id = currency.id
                org.currency_format = currency.symbol or "$"
                changed += ["currency", "currency_format"]
                sync_cash_settings(org.id, currency.id)
            if changed:
                org.save(update_fields=sorted(set(changed)))
    except Exception:
        logger.exception("Entity & Integration save failed entity=%s actor=%s", entity_id, user.id)
        return refuse(request, SAVE_FAILED, 500)
    logger.info("Entity & Integration saved entity=%s actor=%s fields=%s", entity_id, user.id, sorted(set(changed)))
    return None


def company_integration(request: HttpRequest) -> HttpResponse:
    if request.method == "PATCH":
        early, user, entity_id = _open(request, Permission.XERO_SETTINGS_UPDATE,
                                       "You don't have permission to change this company's settings.")
        if early is not None:
            return early
        refusal = _save_integration(request, user, entity_id, _body(request))
        if refusal is not None:
            return refusal
        return respond(request, {**_integration_page(user, entity_id), "notices": [], "xero_conflict": None,
                                 "message": "Settings saved!"})
    early, user, entity_id = _open(request, Permission.XERO_SETTINGS_VIEW,
                                   "You don't have permission to see this company's settings.")
    if early is not None:
        return early
    return respond(request, {**_integration_page(user, entity_id), "notices": read_notices(request.GET.get("flash")),
                             "xero_conflict": _conflict(user, entity_id, request.GET.get("xero_conflict"))})


# ---- Xero disconnect and the "move it here" release ------------------------------------------


def held_elsewhere_sentence(holder_name: str) -> str:
    """What to do when the move is not offered (Flask's ``entity_list.held_elsewhere_sentence``)."""
    return (f"Disconnect it from \"{holder_name}\" first (an accountant or admin of \"{holder_name}\" "
            "can do that in its settings), then connect again.")


def company_xero_disconnect(request: HttpRequest) -> HttpResponse:
    if request.method not in ("POST", "OPTIONS"):
        return refuse(request, "Method not allowed", 405)
    early, user, entity_id = _open(request, Permission.XERO_SETTINGS_UPDATE,
                                   "You don't have permission to disconnect this company from Xero.")
    if early is not None:
        return early
    try:
        disconnect_entity_from_xero(entity_id)
    except Exception:
        logger.exception("Xero disconnect failed entity=%s actor=%s", entity_id, user.id)
        # The revoke at Xero may have gone through while the local save did not.
        return refuse(request, "I couldn't finish disconnecting this company from Xero, so it may still show as "
                               "connected. Mind checking again before trying once more?", 502)
    logger.info("Xero disconnected entity=%s actor=%s", entity_id, user.id)
    return respond(request, {**_integration_page(user, entity_id), "notices": [], "xero_conflict": None,
                             "message": "You're disconnected from Xero."})


def company_xero_release(request: HttpRequest) -> HttpResponse:
    """Free the Xero organisation ANOTHER company holds, so it can be connected here.

    The company freed is the body's ``entity_id``, authorized on its own: ``XERO_SETTINGS_UPDATE``
    there, the permission its own Disconnect asks for. ``?entity=`` must still be a company of this
    person, and the two must differ. Development only (owner, 2026-11-03): anywhere else it is a 403,
    after the permission check, so a stranger learns nothing about the company."""
    if request.method not in ("POST", "OPTIONS"):
        return refuse(request, "Method not allowed", 405)
    early, user, entity_id = _open(request, Permission.XERO_SETTINGS_UPDATE,
                                   "You don't have permission to change this company's settings.")
    if early is not None:
        return early
    release_id = str(_body(request).get("entity_id") or "").strip()
    if not release_id:
        return refuse(request, "I need to know which company to disconnect from Xero.", 422)
    if release_id == str(entity_id):
        return refuse(request, "That company is the one you're connecting.", 422)
    other = Entity.objects.filter(id=release_id).first() if _uuid(release_id) else None
    if other is None:
        return refuse(request, NO_COMPANY, 403)
    if not has_permission(user, Permission.XERO_SETTINGS_UPDATE, str(other.id)):
        return refuse(request, f"Only an accountant or admin of \"{other.name}\" can disconnect it from Xero.", 403)
    if not settings.DEVELOPMENT:
        return refuse(request, "Moving a Xero organisation between companies isn't available. "
                      + held_elsewhere_sentence(other.name), 403)
    if not other.xero_org_id:
        logger.info("Xero release: entity=%s already free actor=%s", release_id, user.id)
        return respond(request, {"message": f"\"{other.name}\" is already disconnected from Xero."})
    try:
        disconnect_entity_from_xero(other.id)
    except Exception:
        logger.exception("Xero release failed entity=%s actor=%s", release_id, user.id)
        return refuse(request, f"I couldn't disconnect \"{other.name}\" from Xero, so the organisation is still "
                               "in use there. Mind trying again?", 502)
    logger.info("Xero released entity=%s for=%s actor=%s", release_id, entity_id, user.id)
    return respond(request, {"message": f"\"{other.name}\" is disconnected from Xero."})
