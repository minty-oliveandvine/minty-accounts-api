"""Into the apps, and out - Flask's ``entity/routes/modules.py``, ``list.py``, ``create.py`` (the
onboarding launch), ``auth/routes/logout.py`` and ``leave_entity.py`` (Phase 6 of the Flask
retirement, 2026-11-06).

**Tokens** (HS256 over ``SECRET_KEY``, Flask's claims exactly): the module token (30 min,
``module: "billing"``) every app's ``/landing`` keeps, and the onboarding token (60 min,
``scope: "onboarding"``). Their URLs carry the token, so they are never logged.

**Routes** (signed in, else the front door with ``next``):
- ``/entity``: minty-web's company list with an unscoped token, carrying what was flashed.
- ``/entity/create``: the onboarding wizard, fresh.
- ``/entity/<co>/modules``: mid-onboarding to the wizard; one module to that app; both to
  minty-web's module choice. ``/entity/<co>/petty-cash`` and ``/payment-request`` straight in.
- ``/handoff/minty-web``, ``/handoff/petty-cash-web``, ``/profile``: a fresh token at the click.
- ``/entity/<co>/enter?token=``: a module token (only) for a member of ``<co>`` becomes a session.
- ``POST /logout`` ends the session; ``POST /leave-entity`` leaves the company (presence) and keeps
  it. **POST only (gap 3, 2026-11-06):** a GET is 405, and a POST must come from one of the front
  ends (``Origin``; SameSite=Lax already keeps other sites' POSTs from carrying the cookie).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit

import jwt
from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, HttpResponseRedirect
from minty_db.models import Entity, EntityFunction, EntityFunctionMap, User, UserEntity

from hub.addresses import canonical_redirect, canonical_ref, company_path, resolve
from hub.front_door import signed_in_or_door
from hub.links import handoff_origin, parse
from hub.notices import sign_notices
from hub.policy import (
    Permission,
    Role,
    has_entity_access,
    has_permission,
    is_superuser,
    is_superuser_readonly,
)
from hub.profile import entity_context
from hub.session import (
    drain_flashes,
    flash,
    login_sid,
    resume_presence,
    safe_next,
    sign_in,
    sign_out,
)

logger = logging.getLogger("minty-accounts-api")

MODULE_TOKEN_MINUTES = 30
MODULE_TOKEN_CLAIM = "billing"
ONBOARDING_TOKEN_MINUTES = 60
MODULE_BILL = "PAYMENT_REQUEST"
MODULE_PETTY_CASH = "PETTY_CASH"
MINTY_WEB_ENTITIES_PATH = "/entities"
REQUEST_REFERENCE_MAX = 255
NO_PERMISSION = "Hmm, it looks like you don't have permission to look there."
NOT_FOUND = "Hmm, I looked everywhere but couldn't find that one."


# ---- tokens -------------------------------------------------------------------------------------


def module_token(request: HttpRequest | None, user_id, entity_id, xero_org_id, role: str = "",
                 billing_enabled: bool = True, petty_cash_enabled: bool = True) -> str:
    user = User.objects.filter(id=user_id).first() if user_id else None
    now = datetime.now(UTC)
    payload = {
        "user_id": str(user_id), "entity_id": str(entity_id) if entity_id else "",
        "xero_org_id": str(xero_org_id or ""), "role": role or "",
        "system_role": str(user.system_role or "") if user else "", "module": MODULE_TOKEN_CLAIM,
        "sid": (login_sid(request) or "") if request is not None else "",
        "billing_enabled": billing_enabled, "petty_cash_enabled": petty_cash_enabled,
        "is_view_only": bool(entity_id) and is_superuser_readonly(user, str(entity_id)),
        "exp": now + timedelta(minutes=MODULE_TOKEN_MINUTES), "iat": now,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def onboarding_token(user_id) -> str:
    now = datetime.now(UTC)
    return jwt.encode({"user_id": str(user_id), "scope": "onboarding", "iat": now,
                       "exp": now + timedelta(minutes=ONBOARDING_TOKEN_MINUTES)}, settings.SECRET_KEY, algorithm="HS256")


def module_enabled(entity_id, code: str) -> bool:
    """``entity_function_map.is_enabled``; every unknown is NO."""
    if code not in (MODULE_PETTY_CASH, MODULE_BILL):
        return False
    function = EntityFunction.objects.filter(function_code=code).first()
    if function is None:
        logger.warning("Function %s not found - denying access", code)
        return False
    row = EntityFunctionMap.objects.filter(entity_id=entity_id, entity_function_id=function.id).first()
    return bool(row and row.is_enabled)


def _role(user: User, entity_id) -> str:
    if entity_id:
        row = UserEntity.objects.filter(user_id=user.id, entity_id=entity_id).first()
        if row is not None:
            return str(row.role or "")
    return Role.SUPER_ADMIN.value if is_superuser(user) else ""


# ---- where each app is entered ------------------------------------------------------------------


def _landing(origin: str, next_path: str, entity_id, entity_name: str, token: str) -> str:
    return (f"{origin}/landing?next={quote(next_path, safe='')}&entity_id={entity_id}"
            f"&entity_name={quote(entity_name, safe='')}&token={token}")


def minty_web_company_path(entity_id, sub: str = "") -> str:
    ref = canonical_ref(entity_id)
    return f"/entity/{ref if '/' in ref else ref + '/company'}{sub}"


def minty_web_landing_url(request: HttpRequest, next_path: str, org: Entity | None, user: User) -> str:
    if org is not None:
        token = module_token(request, user.id, org.id, org.xero_org_id, _role(user, org.id),
                             module_enabled(org.id, MODULE_BILL), module_enabled(org.id, MODULE_PETTY_CASH))
        entity_id, name = str(org.id), org.name or ""
    else:
        token, entity_id, name = module_token(request, user.id, "", "", ""), "", ""
    return _landing(handoff_origin(request, "my", settings.MINTY_WEB_URL), next_path, entity_id, name, token)


def minty_web_entity_list_url(request: HttpRequest, user: User) -> str:
    notices = sign_notices(drain_flashes(request))
    path = MINTY_WEB_ENTITIES_PATH + (f"?flash={notices}" if notices else "")
    return minty_web_landing_url(request, path, None, user)


def payment_request_landing_url(request: HttpRequest, org: Entity, user: User, page: str) -> str:
    token = module_token(request, user.id, org.id, org.xero_org_id, _role(user, org.id),
                         module_enabled(org.id, MODULE_BILL), module_enabled(org.id, MODULE_PETTY_CASH))
    origin = handoff_origin(request, "payment-request", settings.PAYMENT_REQUEST_WEB_URL)
    return _landing(origin, f"/entity/{canonical_ref(org.id)}/{page}", org.id, org.name or "", token)


def onboarding_launch_url(request: HttpRequest, user: User, *, entity_name: str = "", entity_id: str = "",
                          fresh: bool = False) -> str:
    params = {"token": onboarding_token(user.id), "first": (user.first_name or "").strip(),
              "last": (user.last_name or "").strip(), "entity_name": entity_name, "entity_id": str(entity_id or ""),
              "fresh": "1" if fresh and not entity_id else ""}
    query = urlencode({k: v for k, v in params.items() if v})
    return f"{handoff_origin(request, 'onboarding', settings.ONBOARDING_WEB_URL)}/?{query}"


def _to_list(request: HttpRequest, message: str | None = None, category: str = "danger") -> HttpResponse:
    if message:
        flash(request, message, category)
    return HttpResponseRedirect("/entity")


def _get_only(request: HttpRequest) -> HttpResponse | None:
    return None if request.method in ("GET", "HEAD") else HttpResponseNotAllowed(["GET"])


def _signed_in_get(request: HttpRequest) -> tuple[HttpResponse | None, User | None]:
    """A GET (else 405) from a signed-in person (else the front door with ``next``)."""
    if (refused := _get_only(request)) is not None:
        return refused, None
    return signed_in_or_door(request)


def _entity(raw) -> Entity | None:
    try:
        return Entity.objects.filter(id=uuid.UUID(str(raw))).first()
    except ValueError:
        return None


# ---- routes -------------------------------------------------------------------------------------


def entity_list(request: HttpRequest) -> HttpResponse:
    early, user = _signed_in_get(request)
    if early is not None:
        return early
    return HttpResponseRedirect(minty_web_entity_list_url(request, user))


def entity_create(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    early, user = signed_in_or_door(request)
    return early or HttpResponseRedirect(onboarding_launch_url(request, user, fresh=True))


def report_dashboard(request: HttpRequest, entity_id) -> HttpResponse:
    """``/entity/<co>/petty-cash``: on to minty-petty-cash-web (old links, emails, Xero's callback)."""
    if (moved := canonical_redirect(request, "report_dashboard", entity_id=entity_id)) is not None:
        return moved
    early, user = _signed_in_get(request)
    if early is not None:
        return early
    if not module_enabled(entity_id, MODULE_PETTY_CASH):
        return _to_list(request, "Petty Cash is not activated for this entity.", "warning")
    if not is_superuser(user) and not UserEntity.objects.filter(user_id=user.id).exists():
        return _to_list(request, "You'll need to create an entity before I can show you the report dashboard.", "info")
    if _entity(entity_id) is None:
        return _to_list(request, NOT_FOUND)
    if not is_superuser(user) and not UserEntity.objects.filter(user_id=user.id, entity_id=entity_id).exists():
        return _to_list(request, "You don't have access to this entity")
    if not has_permission(user, Permission.ENTITY_VIEW, str(entity_id)):
        return _to_list(request, "You do not have permission to view this entity.")
    target = minty_web_company_path(entity_id, "/petty-cash")
    return HttpResponseRedirect(f"/handoff/petty-cash-web?{urlencode({'entity_id': str(entity_id), 'next': target})}")


def module_selector(request: HttpRequest, entity_id) -> HttpResponse:
    if (moved := canonical_redirect(request, "module_selector", entity_id=entity_id)) is not None:
        return moved
    early, user = _signed_in_get(request)
    if early is not None:
        return early
    org = _entity(entity_id)
    if org is None:
        return _to_list(request, NOT_FOUND)
    if org.status == "onboarding":
        return HttpResponseRedirect(onboarding_launch_url(request, user, entity_name=org.name or "", entity_id=org.id))
    if not is_superuser(user) and not UserEntity.objects.filter(user_id=user.id, entity_id=org.id).exists():
        return _to_list(request, NO_PERMISSION)
    Entity.objects.filter(id=org.id).update(last_accessed_at=datetime.now(UTC), last_accessed_by_user_id=user.id)
    resume_presence(user)
    if not module_enabled(org.id, MODULE_BILL):
        return HttpResponseRedirect(company_path(org.id, "petty-cash"))
    if not module_enabled(org.id, MODULE_PETTY_CASH):
        return HttpResponseRedirect(payment_request_landing_url(request, org, user, "payment-request"))
    return HttpResponseRedirect(minty_web_landing_url(request, minty_web_company_path(org.id), org, user))


def _request_segment(raw: str) -> str | None:
    value = (raw or "").strip()
    try:
        return str(uuid.UUID(value))
    except ValueError:
        pass
    if not 1 <= len(value) <= REQUEST_REFERENCE_MAX:
        return None
    if "\\" in value or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    if any(piece in ("", ".", "..") for piece in value.split("/")):
        return None
    return value


def go_to_payment_requests(request: HttpRequest, entity_id) -> HttpResponse:
    if (moved := canonical_redirect(request, "go_to_payment_requests", entity_id=entity_id)) is not None:
        return moved
    early, user = _signed_in_get(request)
    if early is not None:
        return early
    org = _entity(entity_id)
    if org is None:
        return _to_list(request, NOT_FOUND)
    if not is_superuser(user) and not UserEntity.objects.filter(user_id=user.id, entity_id=org.id).exists():
        return _to_list(request, NO_PERMISSION)
    if not module_enabled(org.id, MODULE_BILL):
        flash(request, "The Payment module isn't switched on for this entity yet - an admin can turn it on in the "
                       "entity's module settings.", "warning")
        return HttpResponseRedirect(company_path(org.id, "petty-cash"))
    page = "payment-request"
    if segment := _request_segment(request.GET.get("request", "")):
        page = f"{page}/{quote(segment, safe='')}"
    return HttpResponseRedirect(payment_request_landing_url(request, org, user, page))


def _member_org(request: HttpRequest, user: User, raw: str) -> tuple[HttpResponse | None, Entity | None]:
    org = _entity(raw)
    if org is None or not (is_superuser(user) or has_entity_access(user, str(org.id))):
        return _to_list(request, NO_PERMISSION), None
    return None, org


def handoff_minty_web(request: HttpRequest) -> HttpResponse:
    early, user = _signed_in_get(request)
    if early is not None:
        return early
    destination = safe_next(request.GET.get("next"), "/subscription")
    org = None
    if raw := (request.GET.get("entity_id") or "").strip():
        refused, org = _member_org(request, user, raw)
        if refused is not None:
            return refused
    return HttpResponseRedirect(minty_web_landing_url(request, destination, org, user))


def handoff_petty_cash_web(request: HttpRequest) -> HttpResponse:
    early, user = _signed_in_get(request)
    if early is not None:
        return early
    raw = (request.GET.get("entity_id") or "").strip()
    if not raw:
        return HttpResponseRedirect(minty_web_entity_list_url(request, user))
    entity_id = str(resolve(raw))
    if entity_context(user, entity_id) is None:
        return _to_list(request, NO_PERMISSION)
    org = Entity.objects.get(id=entity_id)
    if org.status == "onboarding":
        return HttpResponseRedirect(onboarding_launch_url(request, user, entity_name=org.name or "", entity_id=org.id))
    if not module_enabled(org.id, MODULE_PETTY_CASH):
        return _to_list(request, "Petty Cash isn't switched on for this company yet - an admin can turn it on in the "
                                 "company's modules.", "warning")
    destination = safe_next(request.GET.get("next"), minty_web_company_path(org.id, "/petty-cash"))
    token = module_token(request, user.id, org.id, org.xero_org_id, _role(user, org.id),
                         module_enabled(org.id, MODULE_BILL), True)
    origin = handoff_origin(request, "pettycash", settings.PETTY_CASH_WEB_URL)
    return HttpResponseRedirect(_landing(origin, destination, org.id, org.name or "", token))


def open_profile(request: HttpRequest) -> HttpResponse:
    early, user = _signed_in_get(request)
    if early is not None:
        return early
    org = None
    if raw := (request.GET.get("entity_id") or "").strip():
        refused, org = _member_org(request, user, raw)
        if refused is not None:
            return refused
    return HttpResponseRedirect(minty_web_landing_url(request, MINTY_WEB_ENTITIES_PATH, org, user))


def module_reenter(request: HttpRequest, entity_id) -> HttpResponse:
    """``/entity/<co>/enter?token=&next=``: back from an app. A module token for a member of the
    company becomes a session; ``next`` is a path on this site, else the company's Petty Cash."""
    if (moved := canonical_redirect(request, "module_reenter", entity_id=entity_id)) is not None:
        return moved
    if (refused := _get_only(request)) is not None:
        return refused
    from hub.session import signed_in_user

    destination = safe_next(request.GET.get("next"), company_path(entity_id, "petty-cash"))
    if signed_in_user(request) is not None:
        return HttpResponseRedirect(destination)
    raw = request.GET.get("token", "")
    lapsed = "Your session ran out. Mind logging back in?"
    off = "Something's off with your session. Mind logging back in?"
    if not raw:
        flash(request, lapsed, "warning")
        return HttpResponseRedirect("/")
    try:
        claims = jwt.decode(raw, settings.SECRET_KEY, algorithms=["HS256"], leeway=60)
    except jwt.ExpiredSignatureError:
        flash(request, lapsed, "warning")
        return HttpResponseRedirect("/")
    except jwt.PyJWTError:
        flash(request, off, "warning")
        return HttpResponseRedirect("/")
    if claims.get("module") != MODULE_TOKEN_CLAIM:
        logger.warning("/enter refused a non-module token (scope=%r) for entity %s", claims.get("scope"), entity_id)
        flash(request, off, "warning")
        return HttpResponseRedirect("/")
    user = User.objects.filter(id=claims.get("user_id")).first() if _entity_uuid(claims.get("user_id")) else None
    if user is None:
        flash(request, "Hmm, that name doesn't seem to be in my list.", "danger")
        return HttpResponseRedirect("/")
    if not (is_superuser(user) or has_entity_access(user, str(entity_id))):
        logger.warning("/enter refused user %s: not a member of entity %s", user.id, entity_id)
        flash(request, NO_PERMISSION, "danger")
        return HttpResponseRedirect("/")
    sign_in(request, user)
    return HttpResponseRedirect(destination)


def _entity_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


# ---- out ----------------------------------------------------------------------------------------


def _from_a_front_end(request: HttpRequest) -> bool:
    origin = (request.headers.get("Origin") or "").rstrip("/")
    own = urlsplit(settings.ACCOUNTS_API_URL or request.build_absolute_uri("/"))
    allowed = {settings.MINTY_WEB_URL, settings.PAYMENT_REQUEST_WEB_URL, settings.ONBOARDING_WEB_URL,
               settings.PETTY_CASH_WEB_URL, f"{own.scheme}://{own.netloc}", *settings.EXTRA_FRONTEND_ORIGINS.values()}
    return origin in allowed or parse(origin) is not None


def _post_from_front_end(request: HttpRequest) -> HttpResponse | None:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if not _from_a_front_end(request):
        logger.warning("%s refused: not posted by one of the front ends (origin=%r)", request.path,
                       request.headers.get("Origin"))
        return HttpResponse("Forbidden", status=403, content_type="text/plain")
    return None


def logout(request: HttpRequest) -> HttpResponse:
    if (refused := _post_from_front_end(request)) is not None:
        return refused
    drain_flashes(request)
    sign_out(request)
    return HttpResponseRedirect("/")


def leave_entity(request: HttpRequest) -> HttpResponse:
    """Out of the COMPANY, back to the list: presence is dropped, the session kept."""
    if (refused := _post_from_front_end(request)) is not None:
        return refused
    early, user = signed_in_or_door(request)
    if early is not None:
        return early
    User.objects.filter(id=user.id).update(signed_in_at=None, last_seen_at=datetime.now(UTC))
    return HttpResponseRedirect("/entity")
