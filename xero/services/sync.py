"""A company's Xero accounts and contacts, brought into Minty's tables - the one sync every service
asks for (Phase 3 of the Flask retirement, 2026-11-06; the owner's choice: one sync, in
accounts-api).

Ported from Flask's ``entity/services/settings.py``: ``sync_xero_accounts_to_db`` /
``reconcile_accounts_with_snapshot``, ``sync_expense_account_info_from_xero``,
``sync_xero_coa_pettycash``, ``sync_xero_coa_bill`` and ``sync_contacts_if_changed``, and
``xero/services/integration.py``'s two fetches. Flask asked Xero for the same ACTIVE accounts three
times each time Petty Cash Settings opened; this asks once and does all of it:

- ``account_info`` matches Xero's ACTIVE accounts: new ones added, returning ones re-activated,
  renamed or recoded ones refreshed (a type Petty Cash's chart excludes keeps its old name, as in
  Flask), and any that Xero no longer has ACTIVE deleted (which cascades their
  ``entity_account_xero`` rows and clears the Petty Cash mapping that named them). A stale
  ARCHIVED row always goes.
- ``entity_account_xero`` (Petty Cash's code list) gains a row per eligible account; the ticks
  (``is_active``) are never changed here.
- ``entity_bill_account_xero`` (Payment Requests' list) likewise, and loses rows whose account is gone.
- ``xero_contact_sync`` gains new contacts and renames changed ones. Nothing is removed: absence
  from Xero's answer is ambiguous (archived, a partial fetch).

A failed or empty fetch changes nothing. Each half runs in one transaction under Flask's lock
(``pg_advisory_xact_lock(hashtextextended('xero-sync:<entity>', 0))``), so it never overlaps
Flask's own syncs while both exist.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.db import IntegrityError, connection, transaction
from minty_db.models import AccountInfo, EntityAccountXero, EntityBillAccountXero, XeroContactSync

logger = logging.getLogger("minty-accounts-api")

TIMEOUT = (10, 60)
CONTACTS_PAGE = 1000
#: Types Petty Cash's chart never shows (Flask's ``COA_EXCLUDED_TYPES``): their names aren't refreshed.
COA_EXCLUDED_TYPES = frozenset({"BANK", "EQUITY", "OTHERINCOME", "SALES", "REVENUE"})
#: Petty Cash's code list (``COA_INCLUDED_TYPES``) and Payment Requests' (``BILL_COA_INCLUDED_TYPES``).
COA_INCLUDED_TYPES = frozenset({"DIRECTCOSTS", "EXPENSE", "OVERHEADS", "PREPAYMENT"})
BILL_COA_INCLUDED_TYPES = frozenset({"DIRECTCOSTS", "EXPENSE", "FIXED", "OVERHEADS", "PREPAYMENT"})


def _headers(access_token: str, org) -> dict[str, str]:
    return {"Authorization": f"Bearer {access_token}", "Xero-Tenant-Id": str(org), "Accept": "application/json"}


def _trunc(value, length: int):
    return str(value)[:length] if value else value


def fetch_active_accounts(access_token: str, org) -> list[dict] | None:
    """Xero's ACTIVE accounts; None when Xero could not be asked or refused (logged)."""
    try:
        response = requests.get(f"{settings.XERO_API_BASE_URL}/Accounts", headers=_headers(access_token, org),
                                params={"where": 'Status=="ACTIVE"'}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        logger.error("xero sync: accounts unreachable for org %s: %s", org, exc)
        return None
    if response.status_code != 200:
        logger.error("xero sync: accounts answered %s for org %s: %s", response.status_code, org,
                     (response.text or "")[:300])
        return None
    try:
        return (response.json() or {}).get("Accounts") or []
    except ValueError:
        logger.error("xero sync: accounts answer for org %s was not JSON", org)
        return None


def fetch_contacts(access_token: str, org) -> list[dict] | None:
    """Every contact, page by page; None when any page failed (a part list would read as removals)."""
    contacts: list[dict] = []
    page = 1
    while True:
        try:
            response = requests.get(f"{settings.XERO_API_BASE_URL}/Contacts", headers=_headers(access_token, org),
                                    params={"page": page, "pageSize": CONTACTS_PAGE, "order": "Name ASC"},
                                    timeout=TIMEOUT)
        except requests.RequestException as exc:
            logger.error("xero sync: contacts page %s unreachable for org %s: %s", page, org, exc)
            return None
        if response.status_code != 200:
            logger.error("xero sync: contacts page %s answered %s for org %s; %s gathered so far discarded",
                         page, response.status_code, org, len(contacts))
            return None
        try:
            found = (response.json() or {}).get("Contacts") or []
        except ValueError:
            logger.error("xero sync: contacts page %s for org %s was not JSON", page, org)
            return None
        contacts.extend(found)
        if len(found) < CONTACTS_PAGE:
            return contacts
        page += 1


def lock(entity_id) -> None:
    """Flask's ``lock_entity_xero_sync``: one sync at a time writes this company's Xero rows.
    Transaction-scoped: call inside ``transaction.atomic``."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [f"xero-sync:{entity_id}"])


def _status(account: dict) -> str:
    raw = account.get("Status")
    return str(raw).strip().upper() if raw is not None and str(raw).strip() else "ACTIVE"


def apply_accounts(entity_id, accounts: list[dict], org) -> dict:
    """Make the company's account tables match Xero's ACTIVE ``accounts`` (non-empty)."""
    counts = {"added": 0, "activated": 0, "refreshed": 0, "deleted": 0}
    with transaction.atomic():
        lock(entity_id)
        snapshot = {a["AccountID"]: a for a in accounts if a.get("AccountID")}
        rows = list(AccountInfo.objects.filter(entity_id=entity_id))
        kept = {}
        for row in rows:
            if row.status == "ARCHIVED" or (row.xero_account_id and row.xero_account_id not in snapshot):
                row.delete()
                counts["deleted"] += 1
                continue
            if row.xero_account_id:
                kept[row.xero_account_id] = row
        for account_id, account in snapshot.items():
            row = kept.get(account_id)
            if row is None:
                try:
                    with transaction.atomic():
                        AccountInfo.objects.create(
                            entity_id=entity_id, xero_account_id=account_id, status="ACTIVE",
                            name=_trunc(account.get("Name", ""), 80) or "", type=_trunc(account.get("Type", ""), 50) or "",
                            xero_code=_trunc(account.get("Code", ""), 50),
                            class_type=_trunc(account.get("Class", ""), 50),
                            bank_account_number=_trunc(account.get("BankAccountNumber", ""), 50),
                            bank_account_type=_trunc(account.get("BankAccountType", ""), 50),
                            description=_trunc(account.get("Description", ""), 255),
                        )
                    counts["added"] += 1
                except IntegrityError:  # uq_account_entity_xero: a writer that skipped the lock
                    logger.warning("xero sync: account %s already stored for company %s", account_id, entity_id)
                continue
            changed = []
            if row.status != "ACTIVE" and _status(account) == "ACTIVE":
                row.status = "ACTIVE"
                changed.append("status")
                counts["activated"] += 1
            code = str(account.get("Code") or "").strip()
            if account.get("Type") not in COA_EXCLUDED_TYPES and code:
                for field, value in (("name", account.get("Name", row.name)), ("type", account.get("Type", row.type)),
                                     ("xero_code", code)):
                    if getattr(row, field) != value:
                        setattr(row, field, value)
                        changed.append(field)
            if changed:
                row.save(update_fields=sorted(set(changed)))
                if set(changed) - {"status"}:
                    counts["refreshed"] += 1
        _mirror_petty_cash(entity_id, org)
        _mirror_bills(entity_id)
    logger.info("xero sync: accounts for company %s: %s", entity_id, counts)
    return counts


def _mirror_petty_cash(entity_id, org) -> None:
    """Every Petty Cash-eligible account has an ``entity_account_xero`` row; ticks untouched."""
    existing = {e.account_id: e for e in EntityAccountXero.objects.filter(account__entity_id=entity_id)}
    for account in AccountInfo.objects.filter(entity_id=entity_id, type__in=COA_INCLUDED_TYPES):
        row = existing.get(account.id)
        if row is None:
            EntityAccountXero.objects.create(account_id=account.id, name=account.name, type=account.type,
                                             xero_org_id=org, xero_account_id=account.xero_account_id,
                                             is_active=account.status == "ACTIVE")
        else:
            row.name, row.type, row.xero_account_id = account.name, account.type, account.xero_account_id
            if org:
                row.xero_org_id = org
            row.save(update_fields=["name", "type", "xero_account_id", "xero_org_id"])


def _mirror_bills(entity_id) -> None:
    """Every bill-eligible account has an ``entity_bill_account_xero`` row (``is_active`` and
    ``is_deleted`` untouched); rows whose account is gone are deleted."""
    existing = {b.xero_account_id: b for b in EntityBillAccountXero.objects.filter(entity_id=entity_id)}
    for account in AccountInfo.objects.filter(entity_id=entity_id, type__in=BILL_COA_INCLUDED_TYPES,
                                              xero_account_id__isnull=False):
        code = str(account.xero_code or "").strip()
        row = existing.get(account.xero_account_id)
        if row is None:
            EntityBillAccountXero.objects.create(
                entity_id=entity_id, xero_account_id=account.xero_account_id, account_code=code,
                account_name=account.name or "", account_type=account.type or "", is_default=False,
                is_active=account.status == "ACTIVE", is_deleted=False, sort_order=0)
        else:
            row.account_code, row.account_name, row.account_type = code, account.name or "", account.type or ""
            row.save(update_fields=["account_code", "account_name", "account_type"])
    if AccountInfo.objects.filter(entity_id=entity_id).exists():  # never wipe on a transiently empty table
        live = AccountInfo.objects.filter(entity_id=entity_id).values_list("xero_account_id", flat=True)
        EntityBillAccountXero.objects.filter(entity_id=entity_id).exclude(xero_account_id__in=live).delete()


def apply_contacts(entity_id, contacts: list[dict], org) -> dict:
    """New contacts added, changed names updated; nothing removed."""
    counts = {"added": 0, "renamed": 0}
    with transaction.atomic():
        lock(entity_id)
        stored = {c.xero_contact_id: c for c in XeroContactSync.objects.filter(entity_id=entity_id)}
        for contact in contacts:
            contact_id = contact.get("ContactID")
            if not contact_id:
                continue
            name = _trunc(contact.get("Name", ""), 150) or ""
            row = stored.get(contact_id)
            if row is None:
                XeroContactSync.objects.create(entity_id=entity_id, xero_contact_id=contact_id, xero_org_id=str(org),
                                               name=name)
                stored[contact_id] = True
                counts["added"] += 1
            elif row is not True and (row.name or "") != name:
                row.name = name
                row.save(update_fields=["name"])
                counts["renamed"] += 1
    logger.info("xero sync: contacts for company %s: %s", entity_id, counts)
    return counts


def sync_company(entity_id, access_token: str, org) -> dict:
    """Both halves. ``{"accounts": counts | None, "contacts": counts | None}``; None = Xero
    could not be asked (nothing changed)."""
    accounts = fetch_active_accounts(access_token, org)
    contacts = fetch_contacts(access_token, org)
    return {
        "accounts": apply_accounts(entity_id, accounts, org) if accounts else None,
        "contacts": apply_contacts(entity_id, contacts, org) if contacts is not None else None,
    }
