"""The hub's matching page (Settings > Integration > Match your accounts to Xero; Settings Phase 2,
2026-11-11) - the routes over ``xero/services/matching.py``.

Each is the hub's door plus ``?entity=`` and a permission, as the Integration tab's
(``hub/company.py`` ``_open``): reading needs ``XERO_SETTINGS_VIEW``, everything else
``XERO_SETTINGS_UPDATE``. Refusals are ``{"error": <sentence>}`` with the service's status.

- ``GET  /api/me/company/xero/matching``: the page.
- ``PATCH /api/me/company/xero/matching`` ``{account_id, xero_account_id | null}``: one pick (saved at
  once, so the page can be finished later). PATCH, not PUT: the hub's CORS answer
  (``hub/transport.py`` ``METHODS``) allows no PUT, so a browser never sent one.
- ``POST /api/me/company/xero/matching/refresh``: Xero's accounts read again.
- ``POST /api/me/company/xero/matching/finish``: the replace, then the suppliers; answers
  ``{connected, failed_contacts}`` with the page.
- ``POST /api/me/company/xero/matching/contacts``: the suppliers Xero refused, sent again.
- ``POST /api/me/company/xero/matching/change``: change the matches (Phase 3, 2026-11-12) - a connected
  company's page again, prefilled; ``POST .../cancel`` leaves a change, nothing changed.
"""

from __future__ import annotations

import logging

from django.http import HttpRequest, HttpResponse
from minty_db.models import Entity

from hub.company import _body, _open, _uuid
from hub.policy import Permission
from hub.transport import refuse, respond
from xero.services import matching
from xero.services.connect import MirrorFailed
from xero.services.tokens import resolve_entity_access_token

logger = logging.getLogger("minty-accounts-api")

NO_VIEW = "You don't have permission to see this company's settings."
NO_EDIT = "You don't have permission to connect this company to Xero."


def _token(entity_id):
    token = resolve_entity_access_token(entity_id)
    if token is None:
        raise matching.Refused(matching.NO_TOKEN)
    return token


def _finish_suppliers(request: HttpRequest, user, entity_id) -> HttpResponse:
    """The suppliers step, then - when every one is in Xero - connected, and the first sync."""
    from hub.xero_oauth import start_background_sync

    access_token, org = _token(entity_id)
    failed = matching.link_suppliers(entity_id, access_token, org, user.id)
    if not failed:
        matching.complete(entity_id)
        start_background_sync(entity_id, access_token, org, "Xero matching")
    entity = Entity.objects.get(id=entity_id)
    return respond(request, {**matching.read(entity), "connected": not failed, "failed_contacts": failed})


def company_xero_matching(request: HttpRequest) -> HttpResponse:
    if request.method == "PATCH":
        early, user, entity_id = _open(request, Permission.XERO_SETTINGS_UPDATE, NO_EDIT)
        if early is not None:
            return early
        data = _body(request)
        account_id = _uuid(data.get("account_id"))
        xero_account_id = data.get("xero_account_id")
        if account_id is None or (xero_account_id is not None and _uuid(xero_account_id) is None):
            return refuse(request, matching.NOT_OWN, 400)
        try:
            return respond(request, matching.pick(entity_id, account_id, xero_account_id and _uuid(xero_account_id),
                                                  user.id))
        except matching.Refused as refusal:
            return refuse(request, refusal.message, refusal.status)
    if request.method not in ("GET", "OPTIONS"):
        return refuse(request, "Method not allowed", 405)
    early, user, entity_id = _open(request, Permission.XERO_SETTINGS_VIEW, NO_VIEW)
    if early is not None:
        return early
    return respond(request, matching.read(Entity.objects.get(id=entity_id)))


def company_xero_matching_refresh(request: HttpRequest) -> HttpResponse:
    if request.method not in ("POST", "OPTIONS"):
        return refuse(request, "Method not allowed", 405)
    early, user, entity_id = _open(request, Permission.XERO_SETTINGS_UPDATE, NO_EDIT)
    if early is not None:
        return early
    try:
        access_token, _org = _token(entity_id)
        return respond(request, matching.refresh(entity_id, access_token))
    except matching.Refused as refusal:
        return refuse(request, refusal.message, refusal.status)
    except MirrorFailed:
        return refuse(request, "Xero didn't send your accounts just now. Try Refresh again in a moment.", 502)


def company_xero_matching_finish(request: HttpRequest) -> HttpResponse:
    if request.method not in ("POST", "OPTIONS"):
        return refuse(request, "Method not allowed", 405)
    early, user, entity_id = _open(request, Permission.XERO_SETTINGS_UPDATE, NO_EDIT)
    if early is not None:
        return early
    changing = Entity.objects.filter(id=entity_id, xero_matching="change").exists()
    try:
        matching.finish(entity_id, user.id)
    except matching.Refused as refusal:
        return refuse(request, refusal.message, refusal.status)
    if changing:
        # A change of the matches is about the accounts (Phase 3): its suppliers are left as they are,
        # and the company stays connected - nothing is created in Xero.
        entity = Entity.objects.get(id=entity_id)
        return respond(request, {**matching.read(entity), "connected": True, "failed_contacts": []})
    try:
        return _finish_suppliers(request, user, entity_id)
    except matching.Refused as refusal:
        # The accounts are matched (committed); the suppliers wait for Try again.
        entity = Entity.objects.get(id=entity_id)
        return respond(request, {**matching.read(entity), "connected": False, "failed_contacts": [],
                                 "error": refusal.message})


def company_xero_matching_contacts(request: HttpRequest) -> HttpResponse:
    if request.method not in ("POST", "OPTIONS"):
        return refuse(request, "Method not allowed", 405)
    early, user, entity_id = _open(request, Permission.XERO_SETTINGS_UPDATE, NO_EDIT)
    if early is not None:
        return early
    if not matching.contacts_pending(Entity.objects.get(id=entity_id)):
        return refuse(request, matching.NOT_PENDING, 409)
    try:
        return _finish_suppliers(request, user, entity_id)
    except matching.Refused as refusal:
        return refuse(request, refusal.message, refusal.status)


def _change_route(request: HttpRequest, act) -> HttpResponse:
    if request.method not in ("POST", "OPTIONS"):
        return refuse(request, "Method not allowed", 405)
    early, user, entity_id = _open(request, Permission.XERO_SETTINGS_UPDATE, NO_EDIT)
    if early is not None:
        return early
    try:
        return respond(request, act(entity_id))
    except matching.Refused as refusal:
        return refuse(request, refusal.message, refusal.status)


def company_xero_matching_change(request: HttpRequest) -> HttpResponse:
    return _change_route(request, matching.start_change)


def company_xero_matching_cancel(request: HttpRequest) -> HttpResponse:
    return _change_route(request, matching.cancel_change)
