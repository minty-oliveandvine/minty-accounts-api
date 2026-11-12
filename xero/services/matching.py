"""Matching a company's own accounts to Xero when a company that kept its books in Minty connects
Xero (Settings Phase 2, 2026-11-11; Minty ``docs/modernisation/xero_optional_database.md`` §4 -
decisions 5/5a/6/9/12/13/18; Figma 353:999 frames SET-1b, 3, 3b, 4, 5).

Before this, the connect's mirror deleted every account with no Xero id - all of a Minty company's
own accounts - and every supplier with no organisation, leaving its expenses, daily-closing
accounts and sales methods pointing at nothing.

**Begin** (``begin``, in the connect's transaction): the organisation is recorded, the company stays
``disconnected`` and ``setup_mode`` ``minty``; only the Xero copies (``xero_account`` /
``xero_contact``) are filled, and each own account whose code AND type equal a Xero account's is
picked automatically (``account_match_draft``, ``picked_by`` NULL). Pending while setup_mode is
``minty``, an organisation is set and own accounts remain (``accounts_pending``).

**Picks** (``pick``) save at once, so matching can be finished later. **Finish** (``finish``) is the
replace, one transaction: refused while a used account (``usage.used_account_ids``) has no pick;
accounts picked to one Xero account are combined (the survivor holds Xero's code, else the most
records, else the oldest; every reference re-pointed, the others archived with ``merged_into``);
each picked account takes Xero's identity IN PLACE (Xero id, code, name, type, link) so every
expense, daily-closing account and sales method keeps pointing at it; the bill codes and unpublished
bill lines follow a new code; unused unmatched accounts are archived (never deleted); every change is
logged (``account_change_log``, one batch); ``setup_mode`` becomes ``xero`` and
``xero_publish_from`` today (owner: only records from then on go to Xero).

**Suppliers** (``link_suppliers``) after the replace: each own supplier is linked to the Xero
contact with the same name (case and surrounding spaces ignored; the active one first, else the
first by Xero id), else created in Xero under its Minty name; logged in ``contact_change_log``. Any
Xero refuses are named; the company stays disconnected until they are through (``complete``).

**Phase 3 (owner, 2026-11-12).** ``entities.xero_matching`` (minty-db v0.6) marks an open matching
and why: ``first`` (the above), ``new_accounts`` (a reconnect to the same organisation found accounts
made in Minty meanwhile), ``new_org`` (a reconnect to another organisation: every account matched
again from scratch, nothing mirrored, archived or deleted before Finish) or ``change`` (the matches
changed on the same page, prefilled; the company stays connected). The scope is every active account
not linked to the current organisation (all of them for ``change``; for ``first``, Phase 2's own
accounts). Finish relinks only what changed, may swap two accounts' Xero accounts, clears the marker,
and resets ``xero_publish_from`` to today for ``first``/``new_org`` (older records stay with the old
organisation) but keeps it for ``new_accounts``/``change`` (only what is published from then on).
While a matching is open the sync refreshes only the Xero copies (``sync.sync_company``).
"""

from __future__ import annotations

import difflib
import logging
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from minty_db.models import (
    AccountChangeLog,
    AccountInfo,
    AccountMatchDraft,
    AccountModule,
    Bill,
    BillLine,
    ContactChangeLog,
    Entity,
    EntityAccountXero,
    EntityBillAccountXero,
    EntityPettycashSettings,
    ReportExpense,
    XeroAccount,
    XeroContact,
    XeroContactSync,
)

from xero.services import sync, usage
from xero.services.connect import MirrorFailed

logger = logging.getLogger("minty-accounts-api")

HK = ZoneInfo("Asia/Hong_Kong")
XERO_TIMEOUT = (10, 30)

#: Xero's account type -> Minty's (the schema's ``account_type_from_xero``, for the same rows).
TYPE_FROM_XERO = {
    "BANK": "bank", "CURRENT": "current", "FIXED": "fixed", "INVENTORY": "inventory",
    "NONCURRENT": "noncurrent", "PREPAYMENT": "prepayment", "CURRLIAB": "current_liability",
    "LIABILITY": "liability", "TERMLIAB": "term_liability", "EQUITY": "equity", "SALES": "sales",
    "REVENUE": "revenue", "OTHERINCOME": "other_income", "DIRECTCOSTS": "direct_costs",
    "EXPENSE": "expense", "OVERHEADS": "overheads", "DEPRECIATN": "depreciation",
    "WAGESEXPENSE": "expense", "SUPERANNUATIONEXPENSE": "expense", "PAYGLIABILITY": "current_liability",
    "SUPERANNUATIONLIABILITY": "current_liability",
}
CLASS_OF = {
    **dict.fromkeys(("bank", "current", "fixed", "inventory", "noncurrent", "prepayment"), "asset"),
    **dict.fromkeys(("current_liability", "liability", "term_liability"), "liability"),
    "equity": "equity",
    **dict.fromkeys(("sales", "revenue", "other_income"), "revenue"),
    **dict.fromkeys(("direct_costs", "expense", "overheads", "depreciation"), "expense"),
}
#: The matching page's groups (frame 3), in order.
GROUPS = (
    ("cash_bank", "Cash & bank"),
    ("sales", "Sales"),
    ("cost_of_sales", "Cost of what you sell"),
    ("running", "Running the business"),
    ("owed", "Owed to the director"),
    ("other", "Other"),
)

USED_UNMATCHED = (
    "{n} of the accounts your records use still need a Xero match. Match them, or create them in Xero "
    "and refresh, to finish."
)
PICK_GONE = "Some of the Xero accounts you picked are no longer in Xero. Refresh from Xero and pick again."
NOT_PENDING = "This company has nothing to match with Xero."
NOT_OWN = "That account isn't one to match. Reload the page and try again."
WRONG_KIND = "That Xero account can't be used for this one. Choose one from the list."
NO_TOKEN = "Xero needs to be connected again. Sign in to Xero once more to carry on."
NOT_CONNECTED = "Connect this company to Xero before changing its matches."
ALREADY_OPEN = "Finish the matching that's already open before changing the matches."
NOT_CHANGING = "There's no change of matches to cancel."
#: Why a matching is open (``entities.xero_matching``).
REASONS = ("first", "new_accounts", "new_org", "change")
#: Accounts made in Minty (onboarding's templates and a person's own), as ``sync.MINTY_MADE``.
MINTY_MADE = ("template", "user")
CONTACTS_UNANSWERED = "Xero didn't send your contacts just now. Mind trying again in a moment?"
#: Daily closing moves cash from the float to the bank: they are two accounts (onboarding's
#: accounting.SAME_BANK), so they never share a Xero account - combining would make them one.
SAME_BANK = (
    "Petty cash and the bank you deposit into can't be the same Xero account. "
    "Choose a different one for each."
)


class Refused(Exception):
    """A refusal with its sentence and status (the hub answers ``{"error": ...}``)."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.message, self.status = message, status


# ---- reading -----------------------------------------------------------------------------------


def own_accounts(entity_id):
    """The company's own accounts still to match: active, no Xero id, not combined away."""
    return AccountInfo.objects.filter(
        entity_id=entity_id, xero_account_id__isnull=True, merged_into__isnull=True
    ).exclude(record_status="archived")


def own_suppliers(entity_id, org=None):
    """The company's suppliers still to link: active, not combined away, with no Xero id - or, given
    the current organisation, one of another organisation (Phase 3: a new organisation)."""
    unlinked = Q(xero_contact_id__isnull=True) | Q(xero_contact_id="")
    if org:
        unlinked |= Q(xero_org_id__isnull=False) & ~Q(xero_org_id=str(org))
    return XeroContactSync.objects.filter(entity_id=entity_id, merged_into__isnull=True).exclude(
        record_status="archived").filter(unlinked)


def _active_accounts(entity_id):
    """Every account a matching may touch: active, not combined away, not archived in Xero."""
    return (AccountInfo.objects.filter(entity_id=entity_id, merged_into__isnull=True)
            .exclude(record_status="archived").exclude(status="ARCHIVED"))


def _org_guids(entity_id, org) -> set:
    """The Xero ids of the organisation's accounts as the copies hold them (ACTIVE)."""
    if not org:
        return set()
    return set(XeroAccount.objects.filter(entity_id=entity_id, xero_org_id=str(org), status="ACTIVE")
               .values_list("xero_guid", flat=True))


def scope_accounts(entity_id, org, reason):
    """The accounts a matching for ``reason`` covers (Phase 3). ``first``: the company's own accounts
    (Phase 2); ``change``: every active one; ``new_org`` / ``new_accounts``: every active one not
    linked to ``org`` (no Xero id, or one the organisation's copies don't hold), with Minty's hidden
    system rows left out for ``new_accounts``."""
    if reason == "first" or not reason:
        return own_accounts(entity_id)
    rows = _active_accounts(entity_id)
    if reason == "change":
        return rows
    rows = rows.filter(Q(xero_account_id__isnull=True) | ~Q(xero_account_id__in=_org_guids(entity_id, org)))
    if reason == "new_accounts":
        rows = rows.exclude(is_system=True, xero_account_id__isnull=True)
    return rows


def needs_matching(entity: Entity, tenant_id=None) -> str | None:
    """Why a connect to ``tenant_id`` must match first, or None (the mirror, as before): the company
    keeps its books in Minty with its own accounts (``first``); it had another organisation
    (``new_org``); or, the same organisation, accounts were made in Minty meanwhile (``new_accounts``)."""
    if entity.setup_mode == "minty" and own_accounts(entity.id).exists():
        return "first"
    if not tenant_id or not entity.xero_org_id:
        return None
    if str(entity.xero_org_id) != str(tenant_id):
        return "new_org" if _active_accounts(entity.id).exists() else None
    if entity.setup_mode == "xero" and own_accounts(entity.id).filter(
            source__in=MINTY_MADE, is_system=False).exists():
        return "new_accounts"
    return None


def accounts_pending(entity: Entity) -> bool:
    """A matching is open (``entities.xero_matching``)."""
    return entity.xero_matching in REASONS


def contacts_pending(entity: Entity) -> bool:
    """The accounts are matched (Finish ran: ``xero_publish_from`` set, no matching open) and some
    suppliers aren't in this organisation yet, so the company is still disconnected."""
    return (entity.setup_mode == "xero" and bool(entity.xero_org_id) and entity.status != "connected"
            and not entity.xero_matching and entity.xero_publish_from is not None
            and own_suppliers(entity.id, entity.xero_org_id).exists())


def pending(entity: Entity) -> bool:
    return accounts_pending(entity) or contacts_pending(entity)


def _minty_type(account: AccountInfo) -> str | None:
    return account.account_type or TYPE_FROM_XERO.get((account.type or "").strip().upper())


def _class(minty_type: str | None) -> str | None:
    return CLASS_OF.get(minty_type or "")


def _group(minty_type: str | None) -> str:
    if minty_type == "bank":
        return "cash_bank"
    if minty_type in ("sales", "revenue", "other_income"):
        return "sales"
    if minty_type == "direct_costs":
        return "cost_of_sales"
    if minty_type in ("expense", "overheads", "depreciation"):
        return "running"
    if minty_type in ("current_liability", "liability", "term_liability"):
        return "owed"
    return "other"


def _code(account: AccountInfo) -> str:
    return (account.code or account.xero_code or "").strip()


def _copies(entity_id, org) -> list[XeroAccount]:
    return list(XeroAccount.objects.filter(entity_id=entity_id, xero_org_id=str(org), status="ACTIVE")
                .order_by("code", "name"))


def _copy_type(copy: XeroAccount) -> str | None:
    return TYPE_FROM_XERO.get((copy.xero_type or "").strip().upper())


def _fits(account_type: str | None, copy: XeroAccount) -> bool:
    """A Xero account this one may be matched to: a bank only to a bank (daily closing moves money
    through it), anything else to one of the same class."""
    copy_type = _copy_type(copy)
    if account_type == "bank" or copy_type == "bank":
        return account_type == copy_type
    return _class(account_type) is not None and _class(account_type) == _class(copy_type)


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").strip().lower(), (b or "").strip().lower()).ratio()


def _suggestion(account: AccountInfo, copies: list[XeroAccount]) -> XeroAccount | None:
    """A pair Minty suggests (never picks): a bank by its account number, else by name; anything
    else of the same type with a similar name."""
    account_type = _minty_type(account)
    fitting = [c for c in copies if _fits(account_type, c)]
    if account_type == "bank" and account.bank_account_number:
        by_number = [c for c in fitting if (c.bank_account_number or "").strip()
                     == account.bank_account_number.strip()]
        if by_number:
            return by_number[0]
    same_type = [c for c in fitting if _copy_type(c) == account_type] or fitting
    scored = sorted(((_similar(account.name, c.name), c) for c in same_type), key=lambda s: -s[0])
    return scored[0][1] if scored and scored[0][0] >= 0.6 else None


def auto_pick(entity_id, org, reason="first") -> int:
    """Pick each account in the matching's scope whose code and type equal one Xero account's
    (decision: same code, same type = no question). Existing picks are kept."""
    picked = set(AccountMatchDraft.objects.filter(entity_id=entity_id).values_list("account_id", flat=True))
    by_key: dict = {}
    for copy in _copies(entity_id, org):
        if copy.code:
            by_key.setdefault((copy.code, _copy_type(copy)), copy)
    made = 0
    for account in scope_accounts(entity_id, org, reason):
        if account.id in picked:
            continue
        copy = by_key.get((_code(account), _minty_type(account)))
        if copy is not None:
            AccountMatchDraft.objects.create(account_id=account.id, entity_id=entity_id, xero_account_id=copy.id,
                                             picked_by=None)
            made += 1
    return made


def read(entity: Entity) -> dict:
    """The matching page (frame 3): every own account in its group, its pick or suggestion, whether a
    record uses it, conflicts; the Xero accounts to pick from; what Finish would archive and combine."""
    org = entity.xero_org_id
    copies = _copies(entity.id, org) if org else []
    by_id = {c.id: c for c in copies}
    drafts = {d.account_id: d for d in AccountMatchDraft.objects.filter(entity_id=entity.id)}
    used = usage.used_account_ids(entity.id)
    accounts = sorted(scope_accounts(entity.id, org, entity.xero_matching), key=lambda a: (_code(a), a.name or ""))
    picks_per_copy: dict = {}
    for draft in drafts.values():
        picks_per_copy.setdefault(draft.xero_account_id, []).append(draft.account_id)
    groups = {key: [] for key, _title in GROUPS}
    matched = blocking = 0
    to_archive, combines = [], []
    names = {a.id: a.name for a in accounts}
    for account in accounts:
        account_type = _minty_type(account)
        draft = drafts.get(account.id)
        copy = by_id.get(draft.xero_account_id) if draft else None
        same_code = [c for c in copies if c.code and c.code == _code(account)]
        # Same code, another type: listed, never picked; the person decides (design doc §4 rule 2).
        conflict = next((c for c in same_code if _copy_type(c) != account_type), None) if copy is None else None
        suggestion = _suggestion(account, copies) if copy is None else None
        if copy is not None:
            matched += 1
        elif account.id in used:
            blocking += 1
        else:
            to_archive.append(account.name)
        groups[_group(account_type)].append({
            "account_id": str(account.id),
            "code": _code(account),
            "name": account.name,
            "type": account_type,
            "kind": "bank" if account_type == "bank" else _class(account_type),
            "used": account.id in used,
            "system": bool(account.is_system),
            "pick": {"xero_account_id": str(copy.id), "auto": draft.picked_by is None} if copy else None,
            "suggestion": str(suggestion.id) if suggestion else None,
            "conflict": {"code": conflict.code, "name": conflict.name} if conflict else None,
            "combined_with": [names[i] for i in picks_per_copy.get(copy.id, []) if i != account.id and i in names]
            if copy else [],
        })
    for copy_id, account_ids in picks_per_copy.items():
        if len(account_ids) > 1 and copy_id in by_id:
            combines.append({"xero": by_id[copy_id].name, "accounts": sorted(names.get(i, "") for i in account_ids)})
    return {
        "organisation": entity.xero_tenant_name,
        # why this matching is open (Phase 3): first | new_accounts | new_org | change
        "reason": entity.xero_matching,
        "groups": [{"key": key, "title": title, "rows": groups[key]} for key, title in GROUPS if groups[key]],
        "options": [{"id": str(c.id), "code": c.code or "", "name": c.name,
                     "kind": "bank" if _copy_type(c) == "bank" else _class(_copy_type(c))} for c in copies],
        "matched": matched,
        "total": len(accounts),
        "blocking": blocking,
        "will_archive": sorted(to_archive),
        "will_combine": combines,
        "pending": {"accounts": accounts_pending(entity), "contacts": contacts_pending(entity),
                    "reason": entity.xero_matching},
        "failed_contacts": [],
    }


# ---- begin, pick, refresh ------------------------------------------------------------------------


def _fill_copies(entity_id, access_token, org) -> None:
    accounts = sync.fetch_active_accounts(access_token, org)
    if not accounts:
        logger.error("xero matching: no chart of accounts from Xero for company %s (org %s)", entity_id, org)
        raise MirrorFailed("no chart of accounts")
    sync.lock(entity_id)
    sync._copy_accounts(entity_id, {a["AccountID"]: a for a in accounts if a.get("AccountID")}, org)
    contacts = sync.fetch_contacts(access_token, org)
    if contacts:
        sync._copy_contacts(entity_id, contacts, org)


def begin(entity_id, access_token, org, reason="first") -> None:
    """A connect that matches first (``needs_matching``): the Xero copies of ``org``, the marker, and
    the automatic picks - nothing mirrored, archived or deleted (inside the connect's transaction;
    ``MirrorFailed`` when Xero sends no chart, which rolls it back). The company is disconnected until
    Finish and its suppliers."""
    _fill_copies(entity_id, access_token, org)
    AccountMatchDraft.objects.filter(entity_id=entity_id).exclude(
        account_id__in=scope_accounts(entity_id, org, reason).values("id")).delete()
    Entity.objects.filter(id=entity_id).update(xero_matching=reason, status="disconnected")
    made = auto_pick(entity_id, org, reason)
    logger.info("xero matching: company %s began matching org %s (%s); %s picked automatically", entity_id, org,
                reason, made)


def start_change(entity_id) -> dict:
    """Change the matches (Phase 3): a connected company's matching page again, each account on its
    current Xero account. The company stays connected; publishing goes on."""
    with transaction.atomic():
        entity = Entity.objects.select_for_update().get(id=entity_id)
        if entity.xero_matching:
            raise Refused(ALREADY_OPEN)
        if entity.setup_mode != "xero" or not entity.xero_org_id or entity.status != "connected":
            raise Refused(NOT_CONNECTED)
        copies = {c.xero_guid: c for c in XeroAccount.objects.filter(
            entity_id=entity_id, xero_org_id=str(entity.xero_org_id), status="ACTIVE")}
        AccountMatchDraft.objects.filter(entity_id=entity_id).delete()
        for account in _active_accounts(entity_id).filter(xero_account_id__in=list(copies)):
            AccountMatchDraft.objects.create(account_id=account.id, entity_id=entity_id,
                                             xero_account_id=copies[account.xero_account_id].id, picked_by=None)
        Entity.objects.filter(id=entity_id).update(xero_matching="change")
        entity.xero_matching = "change"
    logger.info("xero matching: company %s is changing its matches", entity_id)
    return read(entity)


def cancel_change(entity_id) -> dict:
    """Leave a change of matches: nothing changed, the picks dropped."""
    with transaction.atomic():
        entity = Entity.objects.select_for_update().get(id=entity_id)
        if entity.xero_matching != "change":
            raise Refused(NOT_CHANGING)
        AccountMatchDraft.objects.filter(entity_id=entity_id).delete()
        Entity.objects.filter(id=entity_id).update(xero_matching=None)
        entity.xero_matching = None
    return read(entity)


def _pending_entity(entity_id) -> Entity:
    entity = Entity.objects.get(id=entity_id)
    if not accounts_pending(entity):
        raise Refused(NOT_PENDING)
    return entity


def _cash_and_bank(entity_id) -> set:
    """Daily closing's petty cash and bank accounts (the pair that must stay two accounts)."""
    row = EntityPettycashSettings.objects.filter(entity_id=entity_id).values(
        "pettycash_account_id", "bank_account_id").first()
    return {v for v in (row or {}).values() if v}


def _shares_cash_and_bank(entity_id, drafts: dict) -> bool:
    pair = _cash_and_bank(entity_id)
    picked = [drafts[a].xero_account_id for a in pair if a in drafts]
    return len(pair) == 2 and len(picked) == 2 and picked[0] == picked[1]


def pick(entity_id, account_id, xero_account_id, user_id) -> dict:
    """Save (or, with no Xero account, clear) one account's pick."""
    entity = _pending_entity(entity_id)
    account = scope_accounts(entity_id, entity.xero_org_id, entity.xero_matching).filter(id=account_id).first()
    if account is None:
        raise Refused(NOT_OWN, 404)
    with transaction.atomic():
        AccountMatchDraft.objects.filter(account_id=account.id).delete()
        if xero_account_id:
            copy = XeroAccount.objects.filter(id=xero_account_id, entity_id=entity_id,
                                              xero_org_id=str(entity.xero_org_id), status="ACTIVE").first()
            if copy is None or not _fits(_minty_type(account), copy):
                raise Refused(WRONG_KIND, 400)
            pair = _cash_and_bank(entity_id)
            if account.id in pair and AccountMatchDraft.objects.filter(
                    account_id__in=pair - {account.id}, xero_account_id=copy.id).exists():
                raise Refused(SAME_BANK, 400)
            AccountMatchDraft.objects.create(account_id=account.id, entity_id=entity_id, xero_account_id=copy.id,
                                             picked_by=user_id)
    return read(entity)


def refresh(entity_id, access_token) -> dict:
    """Refresh from Xero: the copies again (an account created in Xero meanwhile appears) and any new
    automatic picks."""
    entity = _pending_entity(entity_id)
    with transaction.atomic():
        _fill_copies(entity_id, access_token, entity.xero_org_id)
        auto_pick(entity_id, entity.xero_org_id, entity.xero_matching)
    return read(entity)


# ---- finish: the replace -------------------------------------------------------------------------


def _log(batch, entity_id, account, kind, before, user_id, *, merged_into=None, copy=None, org=None) -> None:
    AccountChangeLog.objects.create(
        entity_id=entity_id, account_id=account.id, batch_id=batch, kind=kind,
        before_code=(before[0] or None) if before else None, before_name=before[1] if before else None,
        before_type=before[2] if before else None,
        after_code=_code(account) or None, after_name=account.name, after_type=_minty_type(account),
        merged_into_id=merged_into.id if merged_into else None, xero_org_id=str(org) if org else None,
        xero_guid=copy.xero_guid if copy else None, changed_by=user_id,
    )


def _records(account_id) -> int:
    return (ReportExpense.objects.filter(account_id=account_id).count()
            + BillLine.objects.filter(account_id=account_id).count())


def _survivor(accounts: list[AccountInfo], copy: XeroAccount) -> AccountInfo:
    """Decision 18's survivor: the one already holding Xero's code, else the most records, else the
    oldest."""
    holding = [a for a in accounts if copy.code and _code(a) == copy.code]
    if holding:
        return holding[0]
    return sorted(accounts, key=lambda a: (-_records(a.id), a.created_at or timezone.now(), str(a.id)))[0]


def _combine(entity_id, survivor: AccountInfo, others: list[AccountInfo], batch, user_id) -> None:
    ids = [o.id for o in others]
    ReportExpense.objects.filter(account_id__in=ids).update(account_id=survivor.id)
    BillLine.objects.filter(account_id__in=ids).update(account_id=survivor.id)
    for column in usage.ACCOUNT_SLOTS:
        EntityPettycashSettings.objects.filter(entity_id=entity_id, **{f"{column}__in": ids}).update(
            **{column: survivor.id})
    for module in AccountModule.objects.filter(account_id__in=ids):
        mine = AccountModule.objects.filter(account_id=survivor.id, module_code=module.module_code).first()
        if mine is None:
            AccountModule.objects.create(account_id=survivor.id, module_code=module.module_code, entity_id=entity_id,
                                         is_active=module.is_active, is_default=False)
        elif module.is_active and not mine.is_active:
            AccountModule.objects.filter(account_id=survivor.id, module_code=module.module_code).update(is_active=True)
    AccountModule.objects.filter(account_id__in=ids).delete()
    if EntityAccountXero.objects.filter(account_id=survivor.id).exists():
        EntityAccountXero.objects.filter(account_id__in=ids).delete()
    else:
        first = EntityAccountXero.objects.filter(account_id__in=ids).first()
        if first is not None:
            EntityAccountXero.objects.filter(id=first.id).update(account_id=survivor.id)
            EntityAccountXero.objects.filter(account_id__in=ids).delete()
    for other in others:
        before = (_code(other), other.name, _minty_type(other))
        AccountInfo.objects.filter(id=other.id).update(record_status="archived", status="ARCHIVED",
                                                       merged_into_id=survivor.id)
        other.record_status, other.status = "archived", "ARCHIVED"
        _log(batch, entity_id, other, "merged", before, user_id, merged_into=survivor)


def _link(entity_id, account: AccountInfo, copy: XeroAccount, batch, user_id, extra_codes=()) -> None:
    """The account takes Xero's identity in place; its bill codes and unpublished bill lines follow."""
    before = (_code(account), account.name, _minty_type(account))
    new_type = _copy_type(copy) or _minty_type(account)
    AccountInfo.objects.filter(id=account.id).update(
        xero_account_id=copy.xero_guid, xero_code=copy.code, code=copy.code, type=copy.xero_type,
        account_type=new_type, name=(copy.name or account.name)[:80], status="ACTIVE", record_status="active",
        xero_account_link_id=copy.id,
    )
    account.refresh_from_db()
    old_codes = {c for c in (before[0], *extra_codes) if c}
    if old_codes:
        EntityBillAccountXero.objects.filter(entity_id=entity_id, account_code__in=old_codes).filter(
            Q(xero_account_id__isnull=True) | Q(xero_account_id="")).update(
            account_code=copy.code or "", account_name=account.name, account_type=copy.xero_type,
            xero_account_id=copy.xero_guid)
        BillLine.objects.filter(bill__entity_id=entity_id, account_code__in=old_codes).exclude(
            bill__published="published").update(account_code=copy.code or "", account_id=account.id)
    EntityAccountXero.objects.filter(account_id=account.id).update(
        xero_account_id=copy.xero_guid, xero_org_id=copy.xero_org_id, name=account.name, type=copy.xero_type)
    # Minty used it, so it is one of the Xero accounts Minty uses (X2/X3's tick), and the sales
    # categories and Payment Settings keep listing it.
    if not account.is_system:
        AccountModule.objects.get_or_create(account_id=account.id, module_code="PETTY_CASH",
                                            defaults={"entity_id": entity_id, "is_active": True})
    changed = before != (_code(account), account.name, _minty_type(account))
    _log(batch, entity_id, account, "overwritten" if changed else "linked", before, user_id, copy=copy,
         org=copy.xero_org_id)


def finish(entity_id, user_id) -> dict:
    """The replace (decision 5a's confirm). Refused, with nothing written, while a used account has
    no pick or a pick names a Xero account that is gone."""
    with transaction.atomic():
        entity = Entity.objects.select_for_update().get(id=entity_id)
        if not accounts_pending(entity):
            raise Refused(NOT_PENDING)
        reason = entity.xero_matching
        sync.lock(entity_id)
        # Two accounts may swap Xero accounts (Phase 3's change): the one-active-link rule and the
        # active code/name rules are deferrable, checked at COMMIT.
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        accounts = list(scope_accounts(entity_id, entity.xero_org_id, reason).select_for_update())
        in_scope = {a.id for a in accounts}
        drafts = {d.account_id: d for d in AccountMatchDraft.objects.filter(entity_id=entity_id)}
        used = usage.used_account_ids(entity_id)
        unmatched_used = [a for a in accounts if a.id in used and a.id not in drafts]
        if unmatched_used:
            raise Refused(USED_UNMATCHED.format(n=len(unmatched_used)))
        if _shares_cash_and_bank(entity_id, drafts):
            raise Refused(SAME_BANK)
        copies = {c.id: c for c in XeroAccount.objects.filter(
            entity_id=entity_id, xero_org_id=str(entity.xero_org_id), status="ACTIVE",
            id__in=[d.xero_account_id for d in drafts.values()])}
        if any(d.xero_account_id not in copies for d in drafts.values()):
            raise Refused(PICK_GONE)
        held = list(AccountInfo.objects.filter(entity_id=entity_id, xero_account_id__in=[
            c.xero_guid for c in copies.values()]).exclude(id__in=in_scope))
        if any(row.id in used for row in held):
            raise Refused("A Xero account you picked is already linked to another account here. Refresh from "
                          "Xero and pick again.")
        if held:
            # Xero-made rows nothing uses (a sync imported the chart before matching finished): the
            # company's own account takes the Xero account, so these go, with their old-reader rows.
            guids = [row.xero_account_id for row in held]
            EntityBillAccountXero.objects.filter(entity_id=entity_id, xero_account_id__in=guids).delete()
            AccountInfo.objects.filter(id__in=[row.id for row in held]).delete()
            logger.info("xero matching: company %s - %s unused Xero-made row(s) removed for its own accounts",
                        entity_id, len(held))
        batch = uuid.uuid4()
        by_copy: dict = {}
        for account in accounts:
            if account.id in drafts:
                by_copy.setdefault(drafts[account.id].xero_account_id, []).append(account)
        linked = archived = combined = 0
        # A changed match first lets go of its old Xero id: (company, Xero id) is unique and not
        # deferrable, so two accounts swapping Xero accounts would otherwise collide mid-way.
        moving = [a.id for copy_id, group in by_copy.items()
                  if not (len(group) == 1 and group[0].xero_account_id == copies[copy_id].xero_guid)
                  for a in group if a.xero_account_id]
        if moving:
            AccountInfo.objects.filter(id__in=moving).update(xero_account_id=None, xero_account_link_id=None)
        for copy_id, group in by_copy.items():
            copy = copies[copy_id]
            if len(group) == 1 and group[0].xero_account_id == copy.xero_guid:
                continue  # the match didn't change: nothing to write or log
            survivor = _survivor(group, copy)
            others = [a for a in group if a.id != survivor.id]
            if others:
                _combine(entity_id, survivor, others, batch, user_id)
                combined += len(others)
            _link(entity_id, survivor, copy, batch, user_id, extra_codes=[_code(o) for o in others])
            linked += 1
        for account in accounts:
            if account.id not in drafts:
                before = (_code(account), account.name, _minty_type(account))
                AccountInfo.objects.filter(id=account.id).update(record_status="archived", status="ARCHIVED")
                account.record_status, account.status = "archived", "ARCHIVED"
                _log(batch, entity_id, account, "archived", before, user_id)
                archived += 1
        AccountMatchDraft.objects.filter(entity_id=entity_id).delete()
        done = {"setup_mode": "xero", "xero_matching": None}
        if reason in ("first", "new_org", None):
            # only records from today go to this organisation; older ones stay with the old one
            done["xero_publish_from"] = datetime.now(HK).date()
        Entity.objects.filter(id=entity_id).update(**done)
    logger.info("xero matching: company %s finished: %s linked, %s combined, %s archived (batch %s)", entity_id,
                linked, combined, archived, batch)
    return {"linked": linked, "combined": combined, "archived": archived}


# ---- suppliers (frames 4, 5) ------------------------------------------------------------------------


def _create_in_xero(access_token, org, name: str) -> dict | None:
    """One supplier made in Xero under its Minty name; None when Xero refused it (logged)."""
    try:
        response = requests.post(f"{settings.XERO_API_BASE_URL}/Contacts", timeout=XERO_TIMEOUT, headers={
            "Authorization": f"Bearer {access_token}", "Xero-Tenant-Id": str(org), "Accept": "application/json",
            "Content-Type": "application/json"},
            json={"Contacts": [{"Name": name, "IsCustomer": False, "IsSupplier": True}]})
    except requests.RequestException as exc:
        logger.error("xero matching: contact %r unreachable for org %s: %s", name, org, exc)
        return None
    if response.status_code not in (200, 201):
        logger.error("xero matching: contact %r refused for org %s: %s %s", name, org, response.status_code,
                     (response.text or "")[:300])
        return None
    try:
        made = ((response.json() or {}).get("Contacts") or [{}])[0]
    except (ValueError, IndexError):
        made = {}
    return made if made.get("ContactID") else None


def _merge_supplier(entity_id, supplier: XeroContactSync, into: XeroContactSync, batch, user_id) -> None:
    """A supplier whose Xero contact is already stored here: its records move to that row."""
    ReportExpense.objects.filter(contact_id=supplier.id).update(contact_id=into.id)
    Bill.objects.filter(entity_id=entity_id, contact_id=supplier.id).update(contact_id=into.id)
    for column in usage.CONTACT_SLOTS:
        EntityPettycashSettings.objects.filter(entity_id=entity_id, **{column: supplier.id}).update(
            **{column: into.id})
    XeroContactSync.objects.filter(id=supplier.id).update(record_status="archived", merged_into_id=into.id)
    ContactChangeLog.objects.create(entity_id=entity_id, contact_id=supplier.id, batch_id=batch, kind="merged",
                                    before_name=supplier.name, after_name=into.name, merged_into_id=into.id,
                                    xero_org_id=into.xero_org_id, xero_guid=into.xero_contact_id, changed_by=user_id)


def _link_supplier(entity_id, supplier, guid, name, org, batch, user_id, kind) -> None:
    copy = XeroContact.objects.filter(entity_id=entity_id, xero_org_id=str(org), xero_guid=guid).first()
    if copy is None:
        copy = XeroContact.objects.create(entity_id=entity_id, xero_org_id=str(org), xero_guid=guid,
                                          name=(name or supplier.name)[:255], status="ACTIVE")
    XeroContactSync.objects.filter(id=supplier.id).update(xero_contact_id=guid, xero_org_id=str(org),
                                                          xero_contact_link_id=copy.id)
    ContactChangeLog.objects.create(entity_id=entity_id, contact_id=supplier.id, batch_id=batch, kind=kind,
                                    before_name=supplier.name, after_name=supplier.name, xero_org_id=str(org),
                                    xero_guid=guid, changed_by=user_id)


def link_suppliers(entity_id, access_token, org, user_id) -> list[str]:
    """Every own supplier linked to the Xero contact of its name, else created in Xero (decision 13).
    Each in its own transaction, so the ones that went through stay; the names Xero refused come back."""
    contacts = sync.fetch_contacts(access_token, org)
    if contacts is None:
        raise Refused(CONTACTS_UNANSWERED, 502)
    with transaction.atomic():
        sync.lock(entity_id)
        sync._copy_contacts(entity_id, contacts, org)
    by_name: dict = {}
    for contact in sorted(contacts, key=lambda c: (str(c.get("ContactStatus") or "ACTIVE").upper() != "ACTIVE",
                                                   str(c.get("ContactID") or ""))):
        key = (contact.get("Name") or "").strip().lower()
        if key and contact.get("ContactID"):
            by_name.setdefault(key, contact)
    batch = uuid.uuid4()
    failed: list[str] = []
    for supplier in list(own_suppliers(entity_id, org).order_by("name")):
        match = by_name.get((supplier.name or "").strip().lower())
        with transaction.atomic():
            if match is not None:
                guid = match["ContactID"]
                stored = XeroContactSync.objects.filter(entity_id=entity_id, xero_contact_id=guid).first()
                if stored is not None:
                    _merge_supplier(entity_id, supplier, stored, batch, user_id)
                else:
                    _link_supplier(entity_id, supplier, guid, match.get("Name"), org, batch, user_id, "linked")
                continue
            made = _create_in_xero(access_token, org, supplier.name)
            if made is None:
                failed.append(supplier.name)
                continue
            _link_supplier(entity_id, supplier, made["ContactID"], made.get("Name"), org, batch, user_id,
                           "created_in_xero")
    logger.info("xero matching: company %s suppliers linked; %s Xero refused", entity_id, len(failed))
    return failed


def complete(entity_id) -> None:
    """Matching done and every supplier in Xero: the company is connected."""
    Entity.objects.filter(id=entity_id).update(status="connected", last_connected_at=timezone.now())
