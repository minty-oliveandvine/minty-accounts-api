"""Xero's side of a sign-in, connect or reconnect - Flask's ``blueprints/xero/routes/routes.py``
helpers, ``xero/services/integration.get_auth_token`` / ``get_financial_year_end``,
``entity/services/settings.mirror_entity_xero_on_connect`` /
``sync_all_accounts_and_contacts_background`` and ``token_service.upsert_user_token`` (Phase 6D of
the Flask retirement, 2026-11-06). The routes are ``hub/xero_oauth.py``.

- **The code exchange** (``exchange_code``): ``POST identity.xero.com/connect/token`` with the
  client's Basic credentials. The id_token and access token are read WITHOUT verifying their
  signature, as Flask does (``utils.decode_jwt``): they come straight from Xero's token endpoint
  over TLS in exchange for the code and the client secret. A refusal, an error or an answer without
  both tokens is ``None`` (logged); Flask raised there (a 500).
- **The token bundle** (``store_bundle``) goes to ``user_token`` under the same advisory refresh
  lock as the token service (``xero/services/tokens.py``), so it never interleaves with a refresh.
- **"One Xero organisation = one company"** (``live_org_claimant``): a disconnected claimant's
  leftover organisation is cleared; a claim held by the person now connecting is believed without
  asking Xero (their token is the one just revived); anyone else's is probed with ITS connector's
  token and, when the grant is gone or can't be checked, reconciled as a ghost.
- **Handing a grant back** (``revoke_new_grant``, ``revoke_whole_auth_event``) when a connect is
  refused after Xero already made the grant. Best effort, logged.
- **The financial year end and the mirror**: a connection must carry Xero's year end, and every
  connect makes the cached chart and contacts match the organisation picked (owner, 2026-11-03).
  The mirror runs inside the caller's transaction, beside the company's write.
- **The connect-time sync** (``background_sync``, on a thread): the one sync's fetch and apply
  (``xero/services/sync.py``), plus what only a connect does - contacts Xero no longer has are
  dropped after a complete fetch.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

import jwt
import requests
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from minty_db.models import AccountInfo, Entity, User, UserToken, XeroContactSync

from xero.services import sync
from xero.services.tokens import TOKEN_URL, XERO_TIMEOUT, refresh_lock, resolve_entity_access_token

logger = logging.getLogger("minty-accounts-api")

CONNECTIONS_URL = "https://api.xero.com/connections"
#: Flask's ``REDIRECT_URI`` default: the local dev server's port, registered with the Xero app.
LOCAL_ACCOUNTS_API_URL = "http://localhost:8000"


def redirect_uri() -> str:
    """Xero's OAuth callback, derived from this service's own address (Flask's ``REDIRECT_URI``)."""
    return (settings.ACCOUNTS_API_URL or LOCAL_ACCOUNTS_API_URL) + "/callback"


# ---- the code exchange --------------------------------------------------------------------------


def decode_unverified(token) -> dict:
    """A Xero JWT's claims, its signature, expiry, audience and issuer NOT checked (Flask's
    ``utils.decode_jwt``). Raises on anything that isn't a JWT."""
    return jwt.decode(token, algorithms=["RS256"], options={
        "verify_signature": False, "verify_exp": False, "verify_aud": False, "verify_iss": False})


@dataclass(frozen=True)
class Grant:
    """What Xero gave for an authorization code."""

    bundle: dict
    id_claims: dict
    access_claims: dict

    @property
    def access_token(self) -> str:
        return self.bundle["access_token"]

    @property
    def email(self) -> str | None:
        """The Xero login's address (``email``, else ``preferred_username``), normalised."""
        return normalize_email(self.id_claims.get("email") or self.id_claims.get("preferred_username"))


def normalize_email(value) -> str | None:
    return (value or "").strip().lower() or None


def exchange_code(code) -> Grant | None:
    """The grant for ``code``, or None (logged) when Xero refused, could not be reached, or did not
    send both an id_token and an access token. Tokens are never logged."""
    try:
        response = requests.post(
            TOKEN_URL,
            data={"grant_type": "authorization_code", "code": code or "", "redirect_uri": redirect_uri()},
            auth=(settings.XERO_CLIENT_ID, settings.XERO_CLIENT_SECRET),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=XERO_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.error("xero oauth: code exchange failed, Xero unreachable: %s", exc)
        return None
    if response.status_code != 200:
        # Xero's error body names the reason (invalid_grant...), never a token.
        logger.error("xero oauth: code exchange refused with status %s: %s", response.status_code,
                     (response.text or "")[:300])
        return None
    try:
        bundle = response.json()
        id_claims = decode_unverified(bundle["id_token"])
        access_claims = decode_unverified(bundle["access_token"])
    except (ValueError, KeyError, TypeError, jwt.PyJWTError) as exc:
        logger.error("xero oauth: the code exchange answered 200 without a usable id_token and access "
                     "token: %s", type(exc).__name__)
        return None
    return Grant(bundle=bundle, id_claims=id_claims, access_claims=access_claims)


# ---- the person's token bundle ------------------------------------------------------------------


def store_bundle(user: User, bundle: dict) -> UserToken:
    """Write a fresh grant's bundle to the person's ``user_token`` row, creating it (Flask's
    ``upsert_user_token(user, response)``): each part Xero sent, the access token's obtained-at
    stamped now; ``refresh_token_last_used_at`` untouched (not a refresh). Under the token
    service's refresh lock, so a refresh of the old bundle never lands on top of this one; when
    the wait runs out it writes anyway (a fresh grant beats a refresh in flight). Raises."""
    with refresh_lock(user.id) as acquired:
        if not acquired:
            logger.warning("xero oauth: refresh lock for user %s still held after the wait; writing the new "
                           "grant anyway", user.id)
        row = UserToken.objects.filter(user_id=user.id).first() or UserToken(user_id=user.id)
        if bundle.get("access_token") is not None:
            row.access_token = bundle["access_token"]
            row.access_token_obtained_at = timezone.now()
        if bundle.get("expires_in") is not None:
            row.access_token_expires_in = int(bundle["expires_in"])
        if bundle.get("refresh_token") is not None:
            row.refresh_token = bundle["refresh_token"]
        if bundle.get("id_token") is not None:
            row.id_token = bundle["id_token"]
        row.save()
    return row


# ---- Xero's /connections ------------------------------------------------------------------------


def _bearer(access_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}"}


def auth_event_connections(access_token):
    """``GET /connections?authEventId=`` for this grant: the organisations it granted. Xero's answer
    as parsed JSON (a list, or whatever Xero sent); raises when Xero can't be asked."""
    auth_id = decode_unverified(access_token)["authentication_event_id"]
    logger.info("Xero auth event: %s", auth_id)
    response = requests.get(f"{CONNECTIONS_URL}?authEventId={auth_id}",
                            headers={**_bearer(access_token), "Content-Type": "application/json"}, timeout=10)
    return response.json()


def all_connections(access_token) -> list:
    """Every organisation this token can see, NOT only the ones this auth event granted. Best
    effort: a failure reads as "nothing to see", which only costs a better message."""
    try:
        response = requests.get(CONNECTIONS_URL, headers=_bearer(access_token), timeout=10)
        if response.status_code != 200:
            logger.warning("Empty auth event: GET /connections returned %s; cannot say why nothing was granted",
                           response.status_code)
            return []
        found = response.json()
        return found if isinstance(found, list) else []
    except Exception as exc:  # noqa: BLE001 - best effort, logged
        logger.warning("Empty auth event: GET /connections failed: %s", exc)
        return []


def revoke_new_grant(access_token, connections, tenant_id) -> None:
    """Hand back the grant this callback just created for ``tenant_id`` (``DELETE
    /connections/<id>``; the id comes from ``connections``, never the tenant id). Best effort:
    a failure never turns a clean refusal into an error."""
    try:
        conn_id = next((c.get("id") for c in (connections or []) if c.get("tenantId") == str(tenant_id)), None)
        if not conn_id:
            logger.warning("Blocked connect: no connection id for tenant %s; cannot revoke the grant just created",
                           tenant_id)
            return
        response = requests.delete(f"{CONNECTIONS_URL}/{conn_id}", headers=_bearer(access_token), timeout=10)
        if response.status_code in (200, 204):
            logger.info("Blocked connect: revoked the Xero grant just created for tenant %s so Xero matches our "
                        "state", tenant_id)
        else:
            logger.warning("Blocked connect: DELETE /connections/%s returned %s; the grant may linger on Xero",
                           conn_id, response.status_code)
    except Exception as exc:  # noqa: BLE001 - best effort, logged
        logger.warning("Blocked connect: failed to revoke the grant for tenant %s: %s", tenant_id, exc)


def revoke_whole_auth_event(access_token) -> None:
    """Hand back every grant this auth event made (the wrong Xero login: none of it may stay).
    Scoped by the auth event, so the person's earlier grants are untouched."""
    try:
        connections = auth_event_connections(access_token) or []
    except Exception as exc:  # noqa: BLE001 - best effort, logged
        logger.warning("Wrong-account block: could not list connections to revoke: %s", exc)
        return
    if not isinstance(connections, list):
        logger.warning("Wrong-account block: Xero's connections answer was not a list; nothing revoked")
        return
    for conn in connections:
        revoke_new_grant(access_token, connections, conn.get("tenantId"))


# ---- one Xero organisation = one company --------------------------------------------------------


def reconcile_ghost(claimant: Entity) -> None:
    """Clear a claimant's stale connection: it no longer holds the organisation."""
    logger.info("Conflict live-check: entity %s (%s) is no longer connected on Xero's side; clearing stale "
                "connection state and allowing the connect", claimant.id, claimant.name)
    Entity.objects.filter(id=claimant.id).update(status="disconnected", xero_org_id=None, connected_by_user_id=None)
    claimant.status, claimant.xero_org_id, claimant.connected_by_user_id = "disconnected", None, None


def still_live_on_xero(claimant: Entity) -> bool:
    """Whether ``claimant`` really still holds its organisation, asked with ITS connector's token
    (the connect in progress can't tell "still theirs" from "just re-authorized"). No usable token,
    Xero unreachable or not 200, or the tenant gone: a ghost - reconciled, and False."""
    if not claimant.xero_org_id:
        return False
    tenant = str(claimant.xero_org_id)
    token = resolve_entity_access_token(claimant.id)
    if token is None:
        logger.info("Conflict live-check: entity %s (%s) has no usable connector token; treating its connection "
                    "as revoked and allowing the connect", claimant.id, claimant.name)
        reconcile_ghost(claimant)
        return False
    try:
        response = requests.get(CONNECTIONS_URL, headers=_bearer(token[0]), timeout=10)
    except Exception as exc:  # noqa: BLE001 - unverifiable reads as revoked, as in Flask
        logger.warning("Conflict live-check: Xero /connections failed for entity %s: %s; treating its connection "
                       "as revoked and allowing the connect", claimant.id, exc)
        reconcile_ghost(claimant)
        return False
    if response.status_code != 200:
        logger.warning("Conflict live-check: Xero /connections returned %s for entity %s; treating its connection "
                       "as revoked and allowing the connect", response.status_code, claimant.id)
        reconcile_ghost(claimant)
        return False
    if any(conn.get("tenantId") == tenant for conn in response.json()):
        return True
    reconcile_ghost(claimant)
    return False


def _as_uuid_text(value) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def live_org_claimant(tenant_id, target_entity_id, connecting_user_id=None) -> Entity | None:
    """The company genuinely still holding ``tenant_id`` (other than ``target_entity_id``), or None
    once every stale claim is cleared. Every claimant is examined."""
    claimants = Entity.objects.filter(xero_org_id=tenant_id)
    if (target := _as_uuid_text(target_entity_id)) is not None:
        claimants = claimants.exclude(id=target)
    for claimant in claimants:
        if claimant.status == "disconnected":
            logger.info("Org claim check: entity %s (%s) is marked disconnected but still held tenant %s; clearing "
                        "the stale claim", claimant.id, claimant.name, tenant_id)
            reconcile_ghost(claimant)
            continue
        if connecting_user_id and claimant.connected_by_user_id == connecting_user_id:
            logger.info("Org claim check: entity %s (%s) holds tenant %s and is connected by the same user now "
                        "connecting (%s); the claim stands and the connect is blocked", claimant.id, claimant.name,
                        tenant_id, connecting_user_id)
            return claimant
        if still_live_on_xero(claimant):
            return claimant
    return None


def claimed_elsewhere(connections, target_entity_id, connecting_user_id) -> Entity | None:
    """The company already holding one of ``connections`` (each put through ``live_org_claimant``)."""
    for conn in connections or []:
        if tenant := conn.get("tenantId"):
            if (claimant := live_org_claimant(tenant, target_entity_id, connecting_user_id)) is not None:
                return claimant
    return None


def unclaimed_connections(connections) -> list:
    """After ``claimed_elsewhere`` found no holder, every connection with a tenant id is free."""
    return [c for c in connections or [] if c.get("tenantId")]


# ---- the financial year end and the mirror ------------------------------------------------------


def financial_year_end(access_token, tenant_id) -> tuple[int, int]:
    """``(day, month)`` from Xero's Organisation. Raises on any failure."""
    response = requests.get(f"{settings.XERO_API_BASE_URL}/Organisation", timeout=15, headers={
        **_bearer(access_token), "Xero-Tenant-Id": str(tenant_id), "Accept": "application/json"})
    logger.info("Xero Organisation: status=%s org=%s", response.status_code, tenant_id)
    if response.status_code != 200:
        raise RuntimeError(f"Xero Organisation answered {response.status_code}")
    orgs = response.json().get("Organisations") or []
    org = next((o for o in orgs if o.get("OrganisationID") == tenant_id), orgs[0] if orgs else None)
    if not org:
        raise RuntimeError("Xero Organisation answered no organisation")
    day, month = org.get("FinancialYearEndDay"), org.get("FinancialYearEndMonth")
    if not (isinstance(day, int) and isinstance(month, int) and 1 <= day <= 31 and 1 <= month <= 12):
        raise RuntimeError(f"Xero Organisation has no usable financial year end: {day!r}/{month!r}")
    return day, month


def financial_year_end_or_revoke(access_token, connections, tenant_id, entity_id) -> tuple[int, int] | None:
    """The year end, or None after handing the grant back (a connection must carry it)."""
    try:
        return financial_year_end(access_token, tenant_id)
    except Exception as err:  # noqa: BLE001 - the connect fails, loudly
        logger.error("Xero connect failed: no financial year end for entity %s (tenant %s): %s", entity_id,
                     tenant_id, err)
        revoke_new_grant(access_token, connections, tenant_id)
        return None


class MirrorFailed(Exception):
    """Xero gave no chart of accounts, or the mirror failed: the connect is refused."""


def mirror_on_connect(entity_id, access_token, tenant_id) -> None:
    """Make the cached chart and contacts match the organisation just connected, inside the
    caller's transaction (it commits with the company's write). Accounts the organisation lacks go
    (with their Petty Cash and bill rows), the rest are kept and refreshed (``sync.apply_accounts``),
    rows with no Xero id go, and so does every contact of another organisation (or none recorded).
    Raises ``MirrorFailed`` when Xero gives no chart; the caller's transaction then rolls back."""
    try:
        accounts = sync.fetch_active_accounts(access_token, tenant_id)
        if not accounts:
            logger.error("Xero connect: no chart of accounts from Xero for entity %s (tenant %s); nothing changed",
                         entity_id, tenant_id)
            raise MirrorFailed("no chart of accounts")
        sync.lock(entity_id)
        unmatched = AccountInfo.objects.filter(entity_id=entity_id, xero_account_id__isnull=True).delete()[0]
        contacts_removed = XeroContactSync.objects.filter(entity_id=entity_id).filter(
            Q(xero_org_id__isnull=True) | ~Q(xero_org_id=str(tenant_id))).delete()[0]
        counts = sync.apply_accounts(entity_id, accounts, tenant_id)
    except MirrorFailed:
        raise
    except Exception as err:
        logger.error("Xero connect failed: mirroring tenant %s into entity %s: %s", tenant_id, entity_id, err)
        raise MirrorFailed(str(err)) from err
    logger.info("Xero connect: entity %s mirrored to tenant %s: accounts %s, %s without a Xero id removed; %s "
                "contacts of another organisation removed", entity_id, tenant_id, counts, unmatched, contacts_removed)


# ---- the connect-time sync ----------------------------------------------------------------------


def background_sync(entity_id, access_token, tenant_id) -> None:
    """The sync a Connect or Reconnect starts: contacts, then accounts, each in its own transaction
    so one failing never blocks the other (logged). A complete contact fetch also drops the contacts
    Xero no longer has - only here; the nightly and page-load syncs never remove."""
    logger.info("Starting background sync for entity %s, xero_org_id=%s", entity_id, tenant_id)
    try:
        contacts = sync.fetch_contacts(access_token, tenant_id)
        if contacts is None:
            logger.error("Contact sync failed for entity %s: no complete contact list from Xero, nothing changed",
                         entity_id)
        else:
            fetched = {c.get("ContactID") for c in contacts if c.get("ContactID")}
            with transaction.atomic():
                sync.lock(entity_id)
                removed = 0
                if fetched:
                    removed = XeroContactSync.objects.filter(entity_id=entity_id).exclude(
                        xero_contact_id__in=fetched).delete()[0]
                counts = sync.apply_contacts(entity_id, contacts, tenant_id)
            logger.info("Contact sync entity %s: %s, %s no longer in Xero removed", entity_id, counts, removed)
    except Exception:
        logger.exception("Contact sync failed for entity %s", entity_id)
    try:
        accounts = sync.fetch_active_accounts(access_token, tenant_id)
        if accounts:
            sync.apply_accounts(entity_id, accounts, tenant_id)
        else:
            logger.error("Account sync for entity %s: no accounts from Xero, nothing changed", entity_id)
    except Exception:
        logger.exception("Account sync failed for entity %s", entity_id)
    logger.info("Background sync completed for entity %s", entity_id)


def background_sync_thread(entity_id, access_token, tenant_id) -> None:
    """``background_sync`` as a thread's whole life: its own database connection, closed at the end."""
    try:
        background_sync(entity_id, access_token, tenant_id)
    finally:
        connection.close()
