"""Disconnecting a company from Xero - Flask's ``blueprints/xero/services/disconnect.py`` and
``entity/services/settings.invalidate_entity_xero_cache`` (Phase 5 of the Flask retirement,
2026-11-06). The hub's Disconnect, its "move it here" release and onboarding's release use
``disconnect_entity_from_xero``; onboarding's own Disconnect is ``disconnect_onboarding_entity``.

In order:
1. **Revoke the company's grant at Xero**, as a best effort. The connector's token comes from this
   service's own token service, under the shared refresh lock. Xero's ``/connections`` lists the
   grants, and the one for this organisation is deleted. A lapsed token or Xero being down is
   logged, and the local side goes on.
2. **Clear the connector's stored tokens** when this was the LAST company they connected. Tokens
   are per person and shared across their companies; a company disconnected earlier, which keeps
   its organisation, is not a connection.
3. **Mark the company** with no connector:
   - the shared disconnect: ``disconnected`` (so ``xero_status = 'disconnected'``) and it KEEPS its
     organisation, its synced chart and contacts, its bill codes and its Petty Cash mapping (Xero
     optional, 2026-11-10; Minty's ``docs/modernisation/xero_optional_database.md`` section 4). It
     works on in Minty and only the Xero actions are refused, each already gated on ``status``; a
     reconnect to the same organisation finds everything again, and a connect to another one
     replaces the copy (``connect.mirror_on_connect``). The kept organisation is no claim on it:
     the connect conflict check counts only companies not ``disconnected``
     (``connect.live_org_claimant``).
   - onboarding's own Disconnect: stays ``onboarding`` with no organisation (its synced lists
     kept, as before) - an ``onboarding`` company holding an organisation reads as connected.

Steps 2-3 are one transaction: when they cannot be saved nothing is, and the caller says so. The
revoke at Xero may already have gone through, so the two sides can disagree.
"""

from __future__ import annotations

import logging

import requests
from django.db import transaction
from minty_db.models import Entity, User, UserToken

from xero.services.tokens import resolve_entity_access_token

logger = logging.getLogger("minty-accounts-api")

CONNECTIONS_URL = "https://api.xero.com/connections"

def _revoke_at_xero(org: Entity) -> None:
    try:
        token = resolve_entity_access_token(org.id)
        if token is None:
            return
        headers = {"Authorization": f"Bearer {token[0]}"}
        listed = requests.get(CONNECTIONS_URL, headers=headers, timeout=10)
        if listed.status_code != 200:
            logger.warning("Xero GET /connections returned %s for entity %s", listed.status_code, org.id)
            return
        for conn in listed.json():
            if conn.get("tenantId") == str(org.xero_org_id):
                deleted = requests.delete(f"{CONNECTIONS_URL}/{conn.get('id')}", headers=headers, timeout=10)
                if deleted.status_code in (200, 204):
                    logger.info("Revoked the Xero grant of entity %s", org.id)
                else:
                    logger.warning("Xero DELETE /connections/%s returned %s", conn.get("id"), deleted.status_code)
                return
    except Exception as exc:  # noqa: BLE001 - the local disconnect goes on; Xero is told when it can be
        logger.warning("Failed to revoke the Xero grant of entity %s: %s", org.id, exc)


def _mark(org: Entity, *, status: str, keep_org: bool) -> None:
    """The company's own row (inside the caller's transaction)."""
    changes = {"status": status, "connected_by_user_id": None}
    if not keep_org:
        changes["xero_org_id"] = None
    Entity.objects.filter(id=org.id).update(**changes)


def _disconnect(entity_id, *, status: str, keep_org: bool) -> None:
    org = Entity.objects.filter(id=entity_id).first()
    if org is None:
        raise LookupError(f"no entity {entity_id}")
    logger.info("Disconnecting entity %s from Xero", entity_id)
    connector = User.objects.filter(id=org.connected_by_user_id).first() if org.connected_by_user_id else None
    if org.connected_by_user_id and connector is None:
        logger.warning("connected_by_user_id %s on entity %s points to a missing user; skipping the remote "
                       "disconnect", org.connected_by_user_id, entity_id)
    if connector is not None and org.xero_org_id:
        _revoke_at_xero(org)
    with transaction.atomic():
        if connector is not None:
            still = (Entity.objects.filter(connected_by_user_id=connector.id, xero_org_id__isnull=False)
                     .exclude(status="disconnected").exclude(id=org.id).exists())
            if still:
                logger.info("Keeping the Xero tokens of user %s: still connected to other companies", connector.id)
            else:
                UserToken.objects.filter(user_id=connector.id).update(
                    access_token=None, access_token_obtained_at=None, access_token_expires_in=None,
                    refresh_token=None, id_token=None)
                logger.info("Cleared the Xero tokens of user %s", connector.id)
        _mark(org, status=status, keep_org=keep_org)
    logger.info("Entity %s disconnected from Xero%s", entity_id,
                f"; organisation {org.xero_org_id} and its copy kept" if keep_org and org.xero_org_id else "")


def disconnect_entity_from_xero(entity_id) -> None:
    """The one disconnect: the company ends ``disconnected``, keeping its organisation and its copy."""
    _disconnect(entity_id, status="disconnected", keep_org=True)


def disconnect_onboarding_entity(entity_id) -> None:
    """Onboarding's own Disconnect (owner, 2026-11-06: kept as Flask has it). The company stays in
    ``onboarding`` with no organisation, and its synced Xero lists and mapping are kept."""
    _disconnect(entity_id, status="onboarding", keep_org=False)
