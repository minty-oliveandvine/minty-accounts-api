"""Signing in with Xero, and connecting or reconnecting a company to Xero - Flask's
``blueprints/xero/routes/routes.py`` (everything above ``_authenticate_internal_caller``; Phase 6D
of the Flask retirement, 2026-11-06). Xero's side is ``xero/services/connect.py``.

- ``GET /xero_auth``: sign in (or up) with Xero; ``?next=`` (a path on this site) is where to go
  after, ``?invite=`` an invitation the sign-in accepts.
- ``GET /xero_connect``: connect a company (signed in). ``?entity_id=`` / ``?entity_name=`` name it,
  ``?from=onboarding`` sends the answer back to the onboarding wizard's Accounting step.
- ``GET /xero_reconnect?entity_id=``: reconnect one (signed in, a member, ``XERO_SETTINGS_UPDATE``).
- ``GET /callback``: Xero's answer to all three, told apart by the OAuth ``state``.

**The state** is Flask's (``auth``, ``auth:invite:<token>``, ``entity_connect[_onboarding]
[:<entity id>:<quoted name>[:<initiator id>]]``, ``entity_reconnect[:<initiator id>]``) plus, as
its LAST part, a nonce (gap 2, 2026-11-06): each start route stores a fresh one in the session and
``/callback`` pops it (single use) and refuses a state whose nonce is missing or different before
anything else - no code exchange - so nobody can complete a sign-in or connect someone else
started (a forged ``/callback`` link signing a victim in as the attacker, or attaching the
attacker's organisation to the victim's company). One flow at a time per browser: a second start
replaces the first's nonce.

Everything else - the checks, their order, the sentences flashed and where each answer goes - is
Flask's. The flashes travel with the redirect (``/``, ``/entity`` and the settings tab carry them to
minty-web); a hand-back to the onboarding wizard drops them, as Flask's ``drop_flashes_leaving_flask``
did, because the wizard reads ``?xero=`` instead.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
import uuid
from urllib.parse import quote, unquote, urlencode

from django.conf import settings
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, HttpResponseRedirect
from django.utils import timezone
from minty_db.models import Entity, Invitation, User, UserEntity
from minty_shared import logctx

from core.flask_json import flask_json
from hub.addresses import company_path
from hub.company import _sync_connection, held_elsewhere_sentence
from hub.email_signin import hash_code, resolve_user_by_email
from hub.front_door import signed_in_or_door
from hub.invitations import accept_invitation
from hub.links import handoff_origin, hub_login_url
from hub.notices import sign_conflict
from hub.policy import Permission, has_entity_access, has_permission
from hub.session import drain_flashes, flash, safe_internal_path, safe_next, sign_in
from xero.services import connect
from xero.services.connect import MirrorFailed, normalize_email
from xero.services.tokens import resolve_entity_access_token

logger = logging.getLogger("minty-accounts-api")

NONCE_KEY = "xero_oauth_nonce"
AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
SIGN_IN_SCOPE = "openid profile email offline_access"
#: Every Xero call this service and the others make with the connector's token (Xero's GRANULAR
#: transaction scopes - the app is provisioned with those, and the composite
#: ``accounting.transactions`` is refused). Connect and reconnect ask for the same set.
CONNECT_SCOPE = ("openid profile email offline_access accounting.settings accounting.contacts "
                 "accounting.invoices accounting.banktransactions files")
NO_ACCESS = "Hmm, it looks like you don't have permission to look there."
SEVERAL_UNCLAIMED_ORGS = (
    "Xero didn't say which organisation you picked, and this Xero login has more than one linked to Minty. "
    "In Xero, disconnect Minty from the organisations this company won't use, then connect again.")
CONNECT_FAILED = "I couldn't connect that to Xero. Mind trying again?"
RECONNECT_FAILED = "I couldn't reconnect that to Xero, so it's still disconnected. Mind trying again?"
SIGN_IN_FAILED = "I couldn't get you in with that Xero account. Mind trying again?"
NO_EMAIL = "Xero didn't pass me an email address. Could you sign in with the email link instead?"
NOT_FROM_HERE_SIGN_IN = "That Xero sign-in didn't come from here. Mind trying again?"
NOT_FROM_HERE_CONNECT = "That Xero connection didn't start here, so nothing changed. Mind trying again?"


# ---- the nonce ----------------------------------------------------------------------------------


def _with_nonce(request: HttpRequest, state: str) -> str:
    nonce = secrets.token_urlsafe(32)
    request.session[NONCE_KEY] = nonce
    return f"{state}:{nonce}"


def _verified_state(request: HttpRequest, raw: str) -> tuple[str, bool]:
    """Flask's state (the nonce stripped) and whether its nonce is the one this session holds.
    The session's nonce is spent either way."""
    expected = request.session.pop(NONCE_KEY, None)
    state, _, nonce = raw.rpartition(":")
    ok = bool(expected) and bool(nonce) and hmac.compare_digest(nonce.encode(), str(expected).encode())
    return state, ok


# ---- shared answers -----------------------------------------------------------------------------


def _get_only(request: HttpRequest) -> HttpResponse | None:
    return None if request.method in ("GET", "HEAD") else HttpResponseNotAllowed(["GET"])


def _wants_json(request: HttpRequest) -> bool:
    """Flask's ``services/authz._wants_json_response``."""
    return (request.path.startswith("/api") or request.content_type == "application/json"
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in (request.headers.get("Accept") or "").lower())


def _forbidden(request: HttpRequest, message: str) -> HttpResponse:
    """Flask's ``authz._forbidden``: JSON 403 for a JSON caller, else a flash and the company list."""
    if _wants_json(request):
        return flask_json({"status": "error", "message": message}, 403)
    flash(request, message, "danger")
    return HttpResponseRedirect("/entity")


def _bad_request(request: HttpRequest, message: str) -> HttpResponse:
    if _wants_json(request):
        return flask_json({"status": "error", "message": message}, 400)
    flash(request, message, "warning")
    return HttpResponseRedirect("/index")


def _authorize(scope: str, state: str, extra: str = "") -> HttpResponse:
    """Xero's consent screen. ``prompt=login`` makes Xero ask who is signing in instead of reusing
    a Xero session, so a person signed into Xero as someone else can pick the right account. Built
    as Flask builds it (the redirect quotes what needs quoting)."""
    return HttpResponseRedirect(
        f"{AUTHORIZE_URL}?response_type=code&client_id={settings.XERO_CLIENT_ID}"
        f"&redirect_uri={connect.redirect_uri()}&scope={scope}&state={state}{extra}&prompt=login")


def _settings_tab(entity_id, **query) -> HttpResponse:
    """The company's Entity & Integration tab (Flask's ``url_for("entity_settings", ...)``)."""
    path = company_path(entity_id, "settings/integration")
    return HttpResponseRedirect(path + (f"?{urlencode(query)}" if query else ""))


def _to_list() -> HttpResponse:
    return HttpResponseRedirect("/entity")


def _to_sign_in() -> HttpResponse:
    return HttpResponseRedirect("/")


def _login(request: HttpRequest, user: User) -> None:
    """Flask-Login's ``login_user``: a switched-off account (``is_active`` false) is not signed in."""
    if not user.is_active:
        logger.warning("Xero callback: user %s is switched off; not signed in", user.id)
        return
    sign_in(request, user)


def _onboarding_xero_return(request: HttpRequest, connected: bool, org_name: str = "", *, mismatch: bool = False,
                            expected: str = "", conflict: bool = False, conflict_entity: str = "",
                            conflict_entity_id: str = "", conflict_can_move: bool = False,
                            choose: bool = False) -> HttpResponse:
    """Back to the onboarding wizard's Accounting step (3) with ``?xero=`` mismatch | conflict |
    choose | connected | cancelled, the Xero organisation's name, the address to use on a mismatch,
    and the company holding the organisation on a conflict (``conflict_can_move`` only when true:
    the wizard reads its absence as "cannot move"). The wizard draws no flash: they are dropped."""
    for category, message in drain_flashes(request):
        (logger.warning if category in ("danger", "error") else logger.info)(
            "Flash dropped on the way to the onboarding wizard (%s): %s", category, message)
    if mismatch:
        flag = "mismatch"
    elif conflict:
        flag = "conflict"
    elif choose:
        flag = "choose"
    elif connected:
        flag = "connected"
    else:
        flag = "cancelled"
    query: dict[str, object] = {"xero": flag, "step": 3}
    if org_name:
        query["org"] = org_name
    if mismatch and expected:
        query["expected"] = expected
    if conflict and conflict_entity:
        query["conflict_entity"] = conflict_entity
    if conflict and conflict_entity_id:
        query["conflict_entity_id"] = conflict_entity_id
        if conflict_can_move:
            query["conflict_can_move"] = "1"
    return HttpResponseRedirect(f"{handoff_origin(request, 'onboarding', settings.ONBOARDING_WEB_URL)}/?"
                                f"{urlencode(query)}")


# ---- the start routes ---------------------------------------------------------------------------


def xero_auth(request: HttpRequest) -> HttpResponse:
    if (refused := _get_only(request)) is not None:
        return refused
    # A path on this site only: an outside address here was an open redirect run straight after
    # a successful sign-in.
    if next_url := safe_internal_path((request.GET.get("next") or "").strip()):
        request.session["next_after_login"] = next_url
    invite_token = (request.GET.get("invite") or "").strip()
    state = f"auth:invite:{invite_token}" if invite_token else "auth"
    return _authorize(SIGN_IN_SCOPE, _with_nonce(request, state))


def xero_connect(request: HttpRequest) -> HttpResponse:
    if (refused := _get_only(request)) is not None:
        return refused
    early, user = signed_in_or_door(request)
    if early is not None:
        return early
    if not has_permission(user, Permission.ENTITY_CREATE):
        return _forbidden(request, "You do not have permission to connect a new entity to Xero.")
    from_onboarding = request.GET.get("from") == "onboarding"
    entity_id = (request.GET.get("entity_id") or "").strip()
    entity_name = (request.GET.get("entity_name") or "").strip()
    # A named company must be one this person may connect (Flask asked only ENTITY_CREATE, which
    # everyone holds, and wrote onto whatever id the state named - closed in the move, 2026-11-06).
    if entity_id and not _may_connect(user, entity_id):
        return _forbidden(request, "You do not have permission to connect this entity to Xero.")
    state = "entity_connect_onboarding" if from_onboarding else "entity_connect"
    if entity_id or entity_name:
        # The name is quoted: it may hold ":" or spaces. The trailing id is who clicked Connect;
        # the callback requires the Xero login to be one of their addresses.
        state = f"{state}:{entity_id}:{quote(entity_name, safe='')}:{user.id}"
    return _authorize(CONNECT_SCOPE, _with_nonce(request, state))


def _uuid_text(value) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def xero_reconnect(request: HttpRequest) -> HttpResponse:
    if (refused := _get_only(request)) is not None:
        return refused
    early, user = signed_in_or_door(request)
    if early is not None:
        return early
    entity_id = request.GET.get("entity_id")
    if not entity_id:
        return _bad_request(request, "I need to know which entity we're working with first!")
    known = _uuid_text(entity_id)
    if known is None or not has_entity_access(user, known):
        return _forbidden(request, NO_ACCESS)
    if not has_permission(user, Permission.XERO_SETTINGS_UPDATE, known):
        return _forbidden(request, "You do not have permission to reconnect Xero for this entity.")
    # The id as checked (canonical), never the raw parameter.
    request.session["reconnect_entity_id"] = known
    state = _with_nonce(request, f"entity_reconnect:{user.id}")
    return _authorize(CONNECT_SCOPE, state, f"&entity_id={known}")


# ---- the callback's helpers ---------------------------------------------------------------------


def _create_user_from_xero(claims: dict, xero_email: str) -> User | None:
    """A passwordless, approved account for a Xero login nobody holds yet (any row already owning
    the address is reused). Names from the id_token, else "Xero" / "User"."""
    try:
        with transaction.atomic():
            existing = resolve_user_by_email(xero_email) or User.objects.filter(username=xero_email).first()
            if existing is not None:
                return existing
            user = User.objects.create(
                id=uuid.uuid4(), email=xero_email, xero_email=xero_email, username=xero_email,
                first_name=(claims.get("given_name") or "").strip() or "Xero",
                last_name=(claims.get("family_name") or "").strip() or "User",
                password=hash_code(secrets.token_urlsafe(32)), system_role="normal", approved=True,
                is_active=True)
    except Exception as exc:  # noqa: BLE001 - answered with a sentence, logged
        logger.error("Failed to create Xero-invite User for %s: %s", xero_email, exc)
        return None
    logger.info("Created Xero-invite User %s for %s", user.id, xero_email)
    return user


def _connect_initiator_mismatch(state_initiator_id: str, xero_email) -> str | None:
    """The address(es) expected when the Xero login is not one of the clicker's own (``email``,
    ``xero_email``, ``username``), else None. No initiator in the state, or none of their addresses
    known: not checked (fail open, as in Flask)."""
    if not state_initiator_id:
        return None
    initiator_id = _uuid_text(state_initiator_id)
    initiator = User.objects.filter(id=initiator_id).first() if initiator_id else None
    owned = set() if initiator is None else {
        e for e in (normalize_email(initiator.email), normalize_email(initiator.xero_email),
                    normalize_email(initiator.username)) if e}
    if not owned or normalize_email(xero_email) in owned:
        return None
    display = sorted(e for e in owned if "@" in e)
    return " or ".join(display) if display else "your own Xero account"


def _may_connect(user: User | None, entity_id) -> bool:
    """``XERO_SETTINGS_UPDATE`` on the company - the permission its own Connect asks for."""
    known = _uuid_text(entity_id)
    return bool(user is not None and known and has_permission(user, Permission.XERO_SETTINGS_UPDATE, known))


def _resolve_connect_entity(entity_id: str, user: User | None, from_onboarding: bool,
                            entity_name: str = "") -> Entity | None:
    """The company the connection attaches to: the state's id; else (onboarding) the person's own
    onboarding company with the state's name, else their newest onboarding company; else none.
    Flask's last fallback - the newest company of ANYONE - is gone (2026-11-06): with nothing named,
    nothing is connected."""
    if entity_id:
        known = _uuid_text(entity_id)
        return Entity.objects.filter(id=known, archived_at__isnull=True).first() if known else None
    if from_onboarding and user is not None:
        owned = Entity.objects.filter(id__in=UserEntity.objects.filter(user_id=user.id).values("entity_id"),
                                      status="onboarding", archived_at__isnull=True)
        if name := (entity_name or "").strip():
            if (by_name := owned.filter(name=name).order_by("-created_at").first()) is not None:
                return by_name
        return owned.order_by("-created_at").first()
    return None


def _refuse_conflict(request: HttpRequest, conflict: Entity, user: User) -> dict:
    """Flash the one refusal sentence and hand back ``{"token", "can_move"}`` - the signed
    ``?xero_conflict=`` for the tab, and whether to offer the move (development only, and only with
    ``XERO_SETTINGS_UPDATE`` on the OTHER company; the release route checks again)."""
    can_move = bool(settings.DEVELOPMENT) and has_permission(user, Permission.XERO_SETTINGS_UPDATE, str(conflict.id))
    if can_move:
        flash(request, f"This Xero organisation is already connected to \"{conflict.name}\". A Xero organisation can "
                       "only be linked to one company at a time — move it here, or connect a different organisation.",
              "danger")
    else:
        flash(request, f"This Xero organisation is already connected to \"{conflict.name}\". A Xero organisation can "
                       "only be linked to one company at a time. " + held_elsewhere_sentence(conflict.name), "danger")
    return {"token": sign_conflict(conflict.id, conflict.name, can_move), "can_move": can_move}


def _store_bundle_logged(user: User, bundle: dict, what: str) -> None:
    """Flask's ``upsert_user_token`` where a failure doesn't stop the flow: logged at error."""
    try:
        connect.store_bundle(user, bundle)
    except Exception:
        logger.exception("%s: storing the Xero tokens of user %s failed", what, user.id)


def start_background_sync(entity_id, access_token, tenant_id, what: str) -> None:
    """The connect-time sync on a daemon thread, carrying this request's log context."""
    try:
        thread = threading.Thread(target=logctx.carry_context(connect.background_sync_thread),
                                  args=(str(entity_id), access_token, tenant_id), daemon=True)
        thread.start()
        time.sleep(0.2)
        if thread.is_alive():
            logger.info("Background sync thread started successfully for entity %s", entity_id)
        else:
            logger.warning("Background sync thread may not have started for entity %s", entity_id)
    except Exception as exc:  # noqa: BLE001 - the connection stands; the settings page syncs again
        logger.warning("%s: failed to start background sync: %s", what, exc)


def _record(entity: Entity, user: User, grant: connect.Grant, tenant_id, tenant_name, fy_end, *,
            status: str | None) -> None:
    """The connection, in ONE transaction: the mirror, the company's organisation, name, year end,
    status (None keeps it: mid-onboarding), last connection and connector, and the connector's new
    tokens. ``MirrorFailed`` (no chart) rolls all of it back; so does any other failure, which
    propagates."""
    with transaction.atomic():
        connect.mirror_on_connect(entity.id, grant.access_token, tenant_id)
        values = {"xero_org_id": tenant_id, "xero_tenant_name": tenant_name,
                  "financial_year_end_day": fy_end[0], "financial_year_end_month": fy_end[1],
                  "last_connected_at": timezone.now(), "connected_by_user_id": user.id}
        if status is not None:
            values["status"] = status
        Entity.objects.filter(id=entity.id).update(**values)
        connect.store_bundle(user, grant.bundle)
    for field, value in values.items():
        setattr(entity, field, value)


# ---- the callback -------------------------------------------------------------------------------


def _refuse_unverified(request: HttpRequest, raw_state: str) -> HttpResponse:
    """A callback whose nonce is missing or not this session's: nothing exchanged, nothing written;
    the person lands where a failed attempt of that kind lands (the kind read from the unverified
    state only picks which of this site's own pages that is)."""
    base = raw_state.partition(":")[0]
    logger.warning("Xero callback refused: the state's nonce is missing or not this session's (kind=%r)", base[:40])
    if base == "entity_connect_onboarding":
        flash(request, NOT_FROM_HERE_CONNECT, "danger")
        return _onboarding_xero_return(request, False)
    if base == "entity_connect":
        flash(request, NOT_FROM_HERE_CONNECT, "danger")
        return _to_list()
    if base == "entity_reconnect":
        flash(request, NOT_FROM_HERE_CONNECT, "danger")
        entity_id = request.session.get("reconnect_entity_id")
        return _settings_tab(entity_id) if entity_id else _to_list()
    flash(request, NOT_FROM_HERE_SIGN_IN, "danger")
    return _to_sign_in()


def xero_callback(request: HttpRequest) -> HttpResponse:
    if (refused := _get_only(request)) is not None:
        return refused
    code = request.GET.get("code")
    error = request.GET.get("error")
    raw_state = request.GET.get("state") or ""
    state, verified = _verified_state(request, raw_state)
    if not verified:
        return _refuse_unverified(request, raw_state)

    base_state, _, state_suffix = state.partition(":")
    state_suffix = state_suffix.strip()
    invite_token = ""
    if base_state == "auth" and state_suffix.startswith("invite:"):
        invite_token = state_suffix[len("invite:"):].strip()
        state_suffix = ""
    if base_state == "entity_reconnect":
        state_initiator_id, state_entity_id, state_entity_name = state_suffix.strip(), "", ""
    else:
        state_entity_id, _, rest = state_suffix.partition(":")
        state_entity_id = state_entity_id.strip()
        # The name sits between the id and the initiator, and may itself hold ":" - the quoting is
        # undone once on the way back from Xero - so the initiator (a uuid, always last) is taken
        # from the right. Flask split from the left and misread such a name (2026-11-06).
        name_raw, sep, tail = rest.rpartition(":")
        if sep and _uuid_text(tail.strip()):
            state_initiator_id = tail.strip()
        else:
            name_raw, state_initiator_id = rest, ""
        state_entity_name = unquote(name_raw).strip()
    # Legacy: entity_id could also arrive as a query parameter (reconnect).
    entity_id = state_entity_id or (request.GET.get("entity_id") or "").strip()
    if entity_id:
        logger.info("Entity ID: %s", entity_id)
    if state_entity_name:
        logger.info("Entity name (fallback resolver): %s", state_entity_name)

    if base_state == "auth":
        return _sign_in_with_xero(request, code, invite_token)
    if base_state in ("entity_connect", "entity_connect_onboarding"):
        return _connect(request, code, error, base_state == "entity_connect_onboarding", entity_id,
                        state_entity_name, state_initiator_id)
    if base_state == "entity_reconnect":
        return _reconnect(request, code, error, entity_id, state_initiator_id)
    return _settings_tab(entity_id) if entity_id else _to_list()


def _sign_in_with_xero(request: HttpRequest, code, invite_token: str) -> HttpResponse:
    grant = connect.exchange_code(code)
    if grant is None:
        flash(request, SIGN_IN_FAILED, "danger")
        return _to_sign_in()
    xero_email = grant.email
    # Resolved by email or xero_email, so a code-sign-in row with the same address is reused.
    user = resolve_user_by_email(xero_email)

    if not user and invite_token:
        if not xero_email:
            logger.warning("Xero invite login: id_token carried no email claim")
            flash(request, NO_EMAIL, "danger")
            return _to_sign_in()
        # Checked before anything is made, so a Xero login for another address leaves no account.
        invitation = Invitation.objects.filter(token=invite_token, status="pending").first()
        if not invitation:
            flash(request, "This invitation doesn't work anymore — it may have already been used.", "danger")
            return _to_sign_in()
        if normalize_email(invitation.email) != xero_email:
            flash(request, "This invite went to a different account — mind signing in with the Xero account it "
                           "was sent to?", "danger")
            return _to_sign_in()
        user = _create_user_from_xero(grant.id_claims, xero_email)
        if user is None:
            flash(request, "Something went wrong on my end while setting up your account. Could you let your "
                           "inviter know?", "danger")
            return _to_sign_in()

    # No "sign up first": a Xero login nobody holds gets an account.
    if not user:
        if not xero_email:
            logger.warning("Xero login: id_token carried no email claim")
            flash(request, NO_EMAIL, "danger")
            return _to_sign_in()
        user = _create_user_from_xero(grant.id_claims, xero_email)
        if user is None:
            flash(request, "I couldn't get you in just now. Mind trying again?", "danger")
            return _to_sign_in()
        logger.info("Xero login: auto-created account for %s", xero_email)

    if not user.xero_email:
        User.objects.filter(id=user.id).update(xero_email=xero_email)
        user.xero_email = xero_email
    _store_bundle_logged(user, grant.bundle, "Xero login")

    if invite_token:
        # The membership before the sign-in, as on the code sign-in; a login for another address
        # is refused here and NOT signed in.
        entity_id_accepted, accept_error = accept_invitation(invite_token, user.id)
        if accept_error:
            logger.warning("Xero invite acceptance failed for user %s: %s", user.id, accept_error)
            invitation = Invitation.objects.filter(token=invite_token).first()
            flash(request, accept_error, "danger")
            return HttpResponseRedirect(hub_login_url(request, invite=invite_token,
                                                      email=invitation.email if invitation else None))
        _login(request, user)
        logger.info("Xero login: user %s accepted invite to entity %s.", user.id, entity_id_accepted)
        flash(request, "Invitation accepted — welcome aboard!", "success")
        return HttpResponseRedirect(company_path(entity_id_accepted, "petty-cash"))

    _login(request, user)
    # Whatever a pre-sign-in request left queued ("couldn't find that one") would toast beside
    # the welcome.
    drain_flashes(request)
    flash(request, "You're signed in with Xero.", "success")
    return HttpResponseRedirect(safe_next(request.session.pop("next_after_login", None), "/entity"))


def _connect(request: HttpRequest, code, error, from_onboarding: bool, entity_id: str, state_entity_name: str,
             state_initiator_id: str) -> HttpResponse:
    def failed(message: str = CONNECT_FAILED) -> HttpResponse:
        flash(request, message, "danger")
        return _onboarding_xero_return(request, False) if from_onboarding else _to_list()

    if error:
        if from_onboarding:
            # Mid-onboarding a cancel goes back to the Accounting step, the company untouched.
            logger.info("Onboarding Xero connect cancelled/denied by user")
            return _onboarding_xero_return(request, False)
        # Declining leaves the company as it was: live without an organisation is 'disconnected'.
        logger.info("Xero connect cancelled/denied for entity '%s': %s", entity_id, error)
        flash(request, "Your new petty cash entity is ready!", "success")
        return _to_list()

    grant = connect.exchange_code(code)
    if grant is None:
        return failed()
    xero_email = grant.email
    # The Xero login must be the clicker's: checked before anything is written or anyone signed in.
    if expected_email := _connect_initiator_mismatch(state_initiator_id, xero_email):
        logger.warning("Xero connect blocked: expected %s, logged in as %s", expected_email, xero_email)
        connect.revoke_whole_auth_event(grant.access_token)
        flash(request, f"You must connect with the Xero account for {expected_email}. You logged in as "
                       f"{xero_email}.", "danger")
        if from_onboarding:
            return _onboarding_xero_return(request, False, mismatch=True, expected=expected_email.split(" or ")[0])
        return _to_list()

    user = resolve_user_by_email(xero_email)
    if not user:
        # Flask said nothing and left the grant live at Xero; now it is handed back and said (2026-11-06).
        logger.error("Xero connect: no Minty account holds the Xero login %s; nothing connected", xero_email)
        connect.revoke_whole_auth_event(grant.access_token)
        flash(request, _unknown_xero_login(xero_email, "connected"), "danger")
        if from_onboarding:
            return _onboarding_xero_return(request, False)
        return _settings_tab(entity_id) if entity_id else _to_list()

    try:
        curr_conn = connect.auth_event_connections(grant.access_token)
        if not curr_conn:
            # The auth event granted nothing: no organisation was picked, or the one picked is
            # already connected to the app for another company. Ask what this token can see.
            target = _resolve_connect_entity(entity_id, user, from_onboarding, entity_name=state_entity_name)
            seen = connect.all_connections(grant.access_token)
            held = connect.claimed_elsewhere(seen, target.id if target is not None else entity_id, user.id)
            if held is not None:
                logger.warning("Xero connect blocked: the auth event granted nothing and tenant of entity %s (%s) "
                               "is already connected", held.id, held.name)
                # Nothing handed back: what this token sees is the other company's grant.
                refusal = _refuse_conflict(request, held, user)
                if from_onboarding:
                    return _onboarding_xero_return(request, False, conflict=True, conflict_entity=held.name,
                                                   conflict_entity_id=str(held.id),
                                                   conflict_can_move=refusal["can_move"])
                return _to_list()
            free = connect.unclaimed_connections(seen)
            if len(free) == 1:
                logger.info("Xero connect: the auth event granted nothing; connecting the one organisation the token "
                            "sees that no company holds (%s)", free[0].get("tenantId"))
                curr_conn = free
            elif free:
                logger.warning("Xero connect: the auth event granted nothing and %d unclaimed organisations are "
                               "visible; not guessing", len(free))
                flash(request, SEVERAL_UNCLAIMED_ORGS, "warning")
                if from_onboarding:
                    return _onboarding_xero_return(request, False, choose=True)
                return _to_list()
            else:
                flash(request, "Entity created! It's not connected to Xero yet — want to connect it now?", "warning")
                logger.error("User did not select an entity in the dropdown in allow in xero's allow access page")
                if from_onboarding:
                    return _onboarding_xero_return(request, False)
                return _to_list()

        tenant_id = curr_conn[0]["tenantId"]
        if tenant_id is None:
            return failed()
        entity = _resolve_connect_entity(entity_id, user, from_onboarding, entity_name=state_entity_name)
        if entity is None:
            logger.error("Entity Connect: entity '%s' not found", entity_id)
            connect.revoke_new_grant(grant.access_token, curr_conn, tenant_id)
            return failed("Hmm, I couldn't find that entity to connect.")
        if not _may_connect(user, entity.id):
            # The Xero login's owner must be allowed to change this company's Xero settings.
            logger.warning("Xero connect refused: user %s may not connect entity %s", user.id, entity.id)
            connect.revoke_new_grant(grant.access_token, curr_conn, tenant_id)
            return failed("You do not have permission to connect this entity to Xero.")
        # One Xero organisation = one company (a re-connect of the same company is allowed).
        conflict = connect.live_org_claimant(tenant_id, entity.id, connecting_user_id=user.id)
        if conflict is not None:
            logger.warning("Xero connect blocked: tenant %s already connected to entity %s (%s)", tenant_id,
                           conflict.id, conflict.name)
            connect.revoke_new_grant(grant.access_token, curr_conn, tenant_id)
            refusal = _refuse_conflict(request, conflict, user)
            if from_onboarding:
                return _onboarding_xero_return(request, False, conflict=True, conflict_entity=conflict.name,
                                               conflict_entity_id=str(conflict.id),
                                               conflict_can_move=refusal["can_move"])
            return _to_list()
        fy_end = connect.financial_year_end_or_revoke(grant.access_token, curr_conn, tenant_id, entity.id)
        if fy_end is None:
            return failed()
        try:
            # Mid-onboarding the status stays "onboarding" (only finalize clears it).
            _record(entity, user, grant, tenant_id, curr_conn[0].get("tenantName"), fy_end,
                    status=None if from_onboarding else "connected")
        except MirrorFailed:
            connect.revoke_new_grant(grant.access_token, curr_conn, tenant_id)
            return failed()
        start_background_sync(entity.id, grant.access_token, tenant_id, "Entity Connect")
        _login(request, user)
        flash(request, "Connected to Xero! I'm pulling in your accounts and contacts now.", "success")
        if from_onboarding:
            return _onboarding_xero_return(request, True, entity.xero_tenant_name or "")
        return _to_list()
    except Exception as err:  # noqa: BLE001 - Flask's catch-all: a sentence, logged
        logger.exception("There is an error connecting to xero: %s", err)
        return failed()


def _unknown_xero_login(xero_email, done: str) -> str:
    return (f"The Xero login {xero_email or '(no email)'} isn't linked to a Minty account, so nothing was {done}. "
            "Sign in to Xero with your own account and try again.")


def _reconnect(request: HttpRequest, code, error, entity_id: str, state_initiator_id: str) -> HttpResponse:
    entity_id = request.session.get("reconnect_entity_id") or entity_id or request.GET.get("entity_id")
    if not entity_id:
        logger.error("Entity Reconnect: Missing entity_id after callback")
        flash(request, "I need to know which entity to reconnect.", "danger")
        return _to_list()
    if error:
        logger.error("Error in xero auth %s", error)
        flash(request, "Xero didn't approve that, so this entity is still disconnected. Mind trying again and "
                       "allowing access?", "danger")
        return _settings_tab(entity_id)

    grant = connect.exchange_code(code)
    if grant is None:
        flash(request, RECONNECT_FAILED, "danger")
        return _settings_tab(entity_id)
    xero_email = grant.email
    if expected_email := _connect_initiator_mismatch(state_initiator_id, xero_email):
        logger.warning("Xero reconnect blocked: expected %s, logged in as %s", expected_email, xero_email)
        connect.revoke_whole_auth_event(grant.access_token)
        flash(request, f"You must reconnect with the Xero account for {expected_email}. You logged in as "
                       f"{xero_email}.", "danger")
        return _settings_tab(entity_id)

    user = resolve_user_by_email(xero_email)
    if not user:
        logger.error("Xero reconnect: no Minty account holds the Xero login %s; nothing reconnected", xero_email)
        connect.revoke_whole_auth_event(grant.access_token)
        flash(request, _unknown_xero_login(xero_email, "reconnected"), "danger")
        return _settings_tab(entity_id)

    try:
        connect.store_bundle(user, grant.bundle)
        try:
            _login(request, user)
        except Exception:
            logger.warning("Entity Reconnect: login_user refresh skipped")
    except Exception:
        logger.warning("Entity Reconnect: failed to update user tokens")

    try:
        curr_conn = connect.auth_event_connections(grant.access_token)
        if not curr_conn:
            # Xero granted NOTHING: no organisation was picked, or the one picked is already
            # connected to the app for another company (owner, 2026-10-09). Name the holder.
            seen = connect.all_connections(grant.access_token)
            held = connect.claimed_elsewhere(seen, entity_id, user.id)
            if held is not None:
                logger.warning("Xero reconnect blocked: the auth event granted nothing and tenant of entity %s (%s) "
                               "is already connected", held.id, held.name)
                # NOTHING handed back: those grants are the other company's.
                return _settings_tab(entity_id, xero_conflict=_refuse_conflict(request, held, user)["token"])
            free = connect.unclaimed_connections(seen)
            if len(free) == 1:
                logger.info("Xero reconnect: the auth event granted nothing; connecting the one organisation the "
                            "token sees that no company holds (%s)", free[0].get("tenantId"))
                curr_conn = free
            elif free:
                logger.warning("Xero reconnect: the auth event granted nothing and %d unclaimed organisations are "
                               "visible; not guessing", len(free))
                flash(request, SEVERAL_UNCLAIMED_ORGS, "warning")
            else:
                flash(request, "No organization was picked on Xero's approval screen, so this entity is still "
                               "disconnected. Mind trying again?", "danger")
                logger.error("User did not select an entity in the dropdown in allow in xero's allow access page")
        if curr_conn:
            try:
                tenant_id = curr_conn[0]["tenantId"]
                if tenant_id is None:
                    flash(request, RECONNECT_FAILED, "danger")
                    return _settings_tab(entity_id)
                conflict = connect.live_org_claimant(tenant_id, entity_id, connecting_user_id=user.id)
                if conflict is not None:
                    logger.warning("Xero reconnect blocked: tenant %s already connected to entity %s (%s)",
                                   tenant_id, conflict.id, conflict.name)
                    connect.revoke_new_grant(grant.access_token, curr_conn, tenant_id)
                    return _settings_tab(entity_id, xero_conflict=_refuse_conflict(request, conflict, user)["token"])
                fy_end = connect.financial_year_end_or_revoke(grant.access_token, curr_conn, tenant_id, entity_id)
                if fy_end is None:
                    flash(request, RECONNECT_FAILED, "danger")
                    return _settings_tab(entity_id)
                known = _uuid_text(entity_id)
                entity = Entity.objects.filter(id=known, archived_at__isnull=True).first() if known else None
                if entity is None:
                    raise LookupError(f"no entity {entity_id!r} to reconnect")
                old_org = entity.xero_org_id
                try:
                    _record(entity, user, grant, tenant_id, curr_conn[0].get("tenantName"), fy_end,
                            status="connected")
                except MirrorFailed:
                    connect.revoke_new_grant(grant.access_token, curr_conn, tenant_id)
                    flash(request, RECONNECT_FAILED, "danger")
                    return _settings_tab(entity_id)
                if not old_org or str(old_org) != str(tenant_id):
                    logger.info("Entity %s xero_org_id update: %s -> %s", entity_id, old_org, tenant_id)
                start_background_sync(entity.id, grant.access_token, tenant_id, "Entity Reconnect")
                try:
                    if (token := resolve_entity_access_token(entity.id)) is not None:
                        entity.refresh_from_db()
                        _sync_connection(entity, token[0])
                    else:
                        logger.warning("Invalid token for entity status sync: %s, preserving existing status",
                                       entity_id)
                except Exception:
                    logger.warning("Entity Reconnect: status sync skipped")
                # A danger left from an earlier BLOCKED reconnect would sit beside the success.
                drain_flashes(request)
                flash(request, "Reconnected to Xero! I'm pulling in your accounts and contacts now.", "success")
                logger.info("Xero Reconnect: Successfully reconnected")
                return _settings_tab(entity_id)
            except Exception as err:  # noqa: BLE001 - Flask's: a sentence, then the tab
                flash(request, "Something went wrong reconnecting to Xero. Mind trying again?", "danger")
                logger.exception("There is an error connecting to xero: %s", err)
    except Exception as err:  # noqa: BLE001 - Flask's catch-all: a sentence, logged
        flash(request, RECONNECT_FAILED, "danger")
        logger.exception("There is an error connecting to xero: %s", err)
        return _settings_tab(entity_id)
    return _settings_tab(entity_id)
