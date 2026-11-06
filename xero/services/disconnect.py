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
   are per person and shared across their companies.
3. **Drop the cached Xero data** (the shared disconnect only), because whatever connects next may be
   another organisation. That covers the mappings in ``entity_pettycash_settings``, the contacts of
   the organisation being left, ``account_info`` (and ``entity_account_xero`` with it) and
   ``entity_bill_account_xero``.
4. **Mark the company** ``disconnected`` (onboarding: ``onboarding``), with no organisation and no
   connector.

Steps 2-4 are one transaction: when they cannot be saved nothing is, and the caller says so. The
revoke at Xero may already have gone through, so the two sides can disagree.
"""

from __future__ import annotations

import logging

import requests
from django.db import transaction
from minty_db.models import (
    AccountInfo,
    Entity,
    EntityBillAccountXero,
    EntityPettycashSettings,
    User,
    UserToken,
    XeroContactSync,
)

from xero.services.tokens import resolve_entity_access_token

logger = logging.getLogger("minty-accounts-api")

CONNECTIONS_URL = "https://api.xero.com/connections"

#: The mappings that name an organisation's accounts and contacts.
ORG_SCOPED_SETTINGS = (
    "pettycash_account_id", "bank_account_id", "cash_sale_account_id", "discrepancy_bank_account_id",
    "discrepancy_account_id", "director_account_id", "cash_sale_contact_id", "director_contact_id",
    "discrepancy_contact_id",
)


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


def invalidate_entity_xero_cache(entity_id, old_org_id) -> None:
    """Drop every cached Xero object of ``old_org_id`` (inside the caller's transaction)."""
    if not entity_id or not old_org_id:
        return
    EntityPettycashSettings.objects.filter(entity_id=entity_id).update(**dict.fromkeys(ORG_SCOPED_SETTINGS))
    contacts = XeroContactSync.objects.filter(entity_id=entity_id, xero_org_id=str(old_org_id)).delete()[0]
    accounts = AccountInfo.objects.filter(entity_id=entity_id).delete()[0]
    bills = EntityBillAccountXero.objects.filter(entity_id=entity_id).delete()[0]
    logger.info("Entity %s Xero cache cleared: %s contacts, %s account rows, %s bill accounts. Account and contact "
                "mappings are now unset and must be re-selected.", entity_id, contacts, accounts, bills)


def _disconnect(entity_id, *, status: str, clear_cache: bool) -> None:
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
                     .exclude(id=org.id).exists())
            if still:
                logger.info("Keeping the Xero tokens of user %s: still connected to other companies", connector.id)
            else:
                UserToken.objects.filter(user_id=connector.id).update(
                    access_token=None, access_token_obtained_at=None, access_token_expires_in=None,
                    refresh_token=None, id_token=None)
                logger.info("Cleared the Xero tokens of user %s", connector.id)
        if clear_cache:
            invalidate_entity_xero_cache(org.id, org.xero_org_id)
        Entity.objects.filter(id=org.id).update(status=status, xero_org_id=None, connected_by_user_id=None)
    logger.info("Entity %s disconnected from Xero", entity_id)


def disconnect_entity_from_xero(entity_id) -> None:
    """The one disconnect: the company ends ``disconnected`` with its Xero cache dropped."""
    _disconnect(entity_id, status="disconnected", clear_cache=True)


def disconnect_onboarding_entity(entity_id) -> None:
    """Onboarding's own Disconnect (owner, 2026-11-06: kept as Flask has it). The company stays in
    ``onboarding``, and its synced Xero lists and mapping are kept."""
    _disconnect(entity_id, status="onboarding", clear_cache=False)
