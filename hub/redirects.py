"""Addresses that only send a person on - Flask's ``entity/routes/settings.py``,
``report/routes/history.py``, ``report/routes/legacy.py`` (the share link),
``invitation/routes/accept.py`` (xero-not-connected) and ``shared/legacy_addresses.py`` (Phase 6 of
the Flask retirement, 2026-11-06).

- A company's settings tabs: Users and Entity & Integration are minty-web's (through its landing,
  carrying ``?flash=`` and a forwarded ``?xero_conflict=``), Petty Cash Settings is
  minty-petty-cash-web's, Modules minty-web's module page, Payment Request the payments app's
  settings. Each keeps Flask's guards: signed in, a member (or superuser), the tab's permission,
  Petty Cash switched on for its tab.
- ``/entity/<co>/petty-cash/reports``: minty-petty-cash-web's history, the filters kept.
- ``/Minty_Report/<path>/``: 308 to minty-petty-cash-web's ``/share/<path>``.
- The old addresses (``LEGACY``): 308, the method and query kept.
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.conf import settings
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseNotAllowed,
    HttpResponseRedirect,
)

from hub.addresses import canonical_redirect, canonical_ref
from hub.front_door import signed_in_or_door
from hub.handoffs import (
    MODULE_PETTY_CASH,
    NO_PERMISSION,
    _entity,
    minty_web_company_path,
    minty_web_landing_url,
    module_enabled,
)
from hub.notices import sign_notices
from hub.policy import Permission, has_entity_access, has_permission
from hub.session import drain_flashes, flash


def _forbidden(request: HttpRequest, message: str, category: str = "danger") -> HttpResponse:
    flash(request, message, category)
    return HttpResponseRedirect("/entity")


def _gate(request: HttpRequest, view: str, entity_id, *, permission: Permission | None = None,
          denied: str = "Not authorized", module: str | None = None, module_off: str = ""):
    """``(early, user)``: canonical address, GET, signed in, member, permission, module."""
    if (moved := canonical_redirect(request, view, entity_id=entity_id)) is not None:
        return moved, None
    if request.method not in ("GET", "HEAD"):
        return HttpResponseNotAllowed(["GET"]), None
    early, user = signed_in_or_door(request)
    if early is not None:
        return early, None
    if not has_entity_access(user, str(entity_id)) or _entity(entity_id) is None:  # None: archived
        return _forbidden(request, NO_PERMISSION), None
    if permission is not None and not has_permission(user, permission, str(entity_id)):
        return _forbidden(request, denied), None
    if module is not None and not module_enabled(entity_id, module):
        return _forbidden(request, module_off, "warning"), None
    return None, user


def _org(entity_id):
    org = _entity(entity_id)
    if org is None:
        raise Http404
    return org


def _to_hub_tab(request: HttpRequest, user, entity_id, tab: str) -> HttpResponse:
    org = _org(entity_id)
    carried = {}
    if notices := sign_notices(drain_flashes(request)):
        carried["flash"] = notices
    if conflict := (request.GET.get("xero_conflict") or "").strip():
        carried["xero_conflict"] = conflict
    path = minty_web_company_path(entity_id, f"/settings/{tab}") + (f"?{urlencode(carried)}" if carried else "")
    return HttpResponseRedirect(minty_web_landing_url(request, path, org, user))


def settings_integration(request: HttpRequest, entity_id) -> HttpResponse:
    early, user = _gate(request, "settings_integration", entity_id, permission=Permission.XERO_SETTINGS_VIEW,
                        denied="You do not have permission to view Xero settings for this entity.")
    return early or _to_hub_tab(request, user, entity_id, "integration")


def settings_general(request: HttpRequest, entity_id) -> HttpResponse:
    """The company's name, country and currency - the hub's General tab (2026-11-10). It reads the
    same page model as Integration, so the same permission opens it."""
    early, user = _gate(request, "settings_general", entity_id, permission=Permission.XERO_SETTINGS_VIEW,
                        denied="You do not have permission to view this entity's settings.")
    return early or _to_hub_tab(request, user, entity_id, "general")


def settings_users(request: HttpRequest, entity_id) -> HttpResponse:
    early, user = _gate(request, "settings_users", entity_id, permission=Permission.USER_VIEW_ALL,
                        denied="You do not have permission to view all users for this entity.")
    return early or _to_hub_tab(request, user, entity_id, "users")


def settings_petty_cash(request: HttpRequest, entity_id) -> HttpResponse:
    """Petty Cash Settings - a hub tab since 2026-11-10 (owner), no longer the Petty Cash app's page."""
    early, user = _gate(request, "settings_petty_cash", entity_id, permission=Permission.COA_VIEW,
                        denied="You do not have permission to view CoA settings for this entity.",
                        module=MODULE_PETTY_CASH, module_off="Petty Cash is not activated for this entity.")
    return early or _to_hub_tab(request, user, entity_id, "petty-cash")


def settings_modules(request: HttpRequest, entity_id) -> HttpResponse:
    early, user = _gate(request, "settings_modules", entity_id, permission=Permission.MODULE_VIEW,
                        denied="You do not have permission to view module settings for this entity.")
    if early is not None:
        return early
    org = _org(entity_id)
    return HttpResponseRedirect(minty_web_landing_url(request, minty_web_company_path(org.id, "/settings/modules"),
                                                      org, user))


def settings_payment_request(request: HttpRequest, entity_id) -> HttpResponse:
    """Payment Settings - the hub's ``payment`` tab since 2026-11-10 (owner); this old address stays."""
    early, user = _gate(request, "settings_payment_request", entity_id)
    return early or _to_hub_tab(request, user, entity_id, "payment")


def report_history(request: HttpRequest, entity_id) -> HttpResponse:
    early, _user = _gate(request, "report_history", entity_id)
    if early is not None:
        return early
    destination = minty_web_company_path(entity_id, "/petty-cash/reports")
    if request.META.get("QUERY_STRING"):
        destination += "?" + request.META["QUERY_STRING"]
    return HttpResponseRedirect(f"/handoff/petty-cash-web?{urlencode({'entity_id': str(entity_id), 'next': destination})}")


def xero_not_connected(request: HttpRequest, entity_id) -> HttpResponse:
    if (moved := canonical_redirect(request, "xero_not_connected", entity_id=entity_id)) is not None:
        return moved
    early, _user = signed_in_or_door(request)
    return early or HttpResponseRedirect(f"/entity/{canonical_ref(entity_id)}/settings/integration")


def _permanent(location: str, request: HttpRequest) -> HttpResponse:
    if request.META.get("QUERY_STRING"):
        location += "?" + request.META["QUERY_STRING"]
    response = HttpResponseRedirect(location)
    response.status_code = 308
    return response


def minty_report_share(request: HttpRequest, share_path: str) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return HttpResponseNotAllowed(["GET"])
    response = HttpResponseRedirect(f"{settings.PETTY_CASH_WEB_URL}/share/{share_path.strip('/')}")
    response.status_code = 308
    return response


def _moved(sub: str, methods=("GET", "HEAD")):
    """A 308 from an old address to ``/entity/<co>/<sub>``."""

    def view(request: HttpRequest, entity_id, **_ignored) -> HttpResponse:
        if request.method not in methods:
            return HttpResponseNotAllowed(list(methods))
        return _permanent(f"/entity/{canonical_ref(entity_id)}/{sub}".rstrip("/"), request)

    return view


def reports_moved(request: HttpRequest, entity_id, rest: str = "") -> HttpResponse:
    if request.method not in ("GET", "HEAD", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    return _permanent(f"/entity/{canonical_ref(entity_id)}/petty-cash/reports" + (f"/{rest}" if rest else ""), request)


ANY = ("GET", "HEAD", "POST")
#: ``(Django route, url name, target sub-path, methods)`` - Flask's ``LEGACY_ADDRESSES``.
LEGACY = [
    ("entity/<uuid:entity_id>/bills", "legacy_bills", "payment-request", ("GET", "HEAD")),
    ("entity/<uuid:entity_id>/settings/xero", "legacy_settings_xero", "settings/integration", ANY),
    ("entity/settings/users/<uuid:entity_id>", "legacy_settings_users", "settings/users", ("GET", "HEAD")),
    ("entity/settings/entity/<uuid:entity_id>", "legacy_settings_entity", "settings/petty-cash", ANY),
    ("entity/settings/module/<uuid:entity_id>", "legacy_settings_module", "settings/modules", ("GET", "HEAD")),
    ("entity/settings/payments/<uuid:entity_id>", "legacy_settings_payments", "settings/payment-request",
     ("GET", "HEAD")),
    ("invitation/xero-not-connected/<uuid:entity_id>", "legacy_xero_not_connected", "xero-not-connected",
     ("GET", "HEAD")),
    ("entity/<uuid:entity_id>/report/opening", "legacy_report_opening", "petty-cash", ("GET", "HEAD")),
    ("entity/<uuid:entity_id>/ending/<str:report_id>", "legacy_ending_with_report", "petty-cash", ("GET", "HEAD")),
    ("entity/<uuid:entity_id>/ending", "legacy_ending", "petty-cash", ("GET", "HEAD")),
]
