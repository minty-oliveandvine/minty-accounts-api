"""Matching a Minty company's accounts to Xero on connect (Settings Phase 2, 2026-11-11;
``xero/services/matching.py``, ``hub/xero_matching.py``).

The owner's rules (Minty ``docs/modernisation/xero_optional_database.md`` §4): same code AND type is
picked automatically; any other pair is the person's; Finish is refused while an account a record uses
(an expense or bill line, any daily-closing account, a sales method) has no pick; an unused unmatched
account is archived, never deleted; accounts picked to one Xero account are combined; each picked
account takes Xero's identity in place, so every reference stays; suppliers are linked by name else
created in Xero; the company stays disconnected until they are all through. Xero faked at
``requests``; Postgres only (the sync's lock and the schema's triggers).
"""

from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
import requests
from django.db import transaction
from minty_db.models import (
    AccountChangeLog,
    AccountInfo,
    AccountMatchDraft,
    AccountModule,
    ContactChangeLog,
    Entity,
    EntityBillAccountXero,
    EntityPettycashSettings,
    XeroAccount,
    XeroContactSync,
)

from hub import xero_matching
from hub.tests.test_company import call
from xero.services import matching, sync

pytestmark = pytest.mark.skipif(not os.environ.get("MINTY_TEST_PG_URI"), reason="Postgres only")

ORG = "org-match"
BASE = "/api/me/company/xero/matching"


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


class FakeXero:
    def __init__(self):
        self.accounts = [
            {"AccountID": "x-pc", "Code": "090", "Name": "Petty Cash", "Type": "BANK", "Status": "ACTIVE"},
            {"AccountID": "x-bank", "Code": "091", "Name": "HSBC Current", "Type": "BANK", "Status": "ACTIVE",
             "BankAccountNumber": "123-456"},
            {"AccountID": "x-rent", "Code": "469", "Name": "Rent", "Type": "EXPENSE", "Status": "ACTIVE"},
            {"AccountID": "x-wages", "Code": "477", "Name": "Wages and Salaries", "Type": "EXPENSE",
             "Status": "ACTIVE"},
            {"AccountID": "x-sales", "Code": "200", "Name": "Sales", "Type": "REVENUE", "Status": "ACTIVE"},
            {"AccountID": "x-clean", "Code": "420", "Name": "Cleaning", "Type": "OVERHEADS", "Status": "ACTIVE"},
            {"AccountID": "x-dir", "Code": "880", "Name": "Owner A Funds Introduced", "Type": "CURRLIAB",
             "Status": "ACTIVE"},
        ]
        self.contacts = [{"ContactID": "c-abc", "Name": "ABC Supplies", "ContactStatus": "ACTIVE"}]
        self.refuse_create: set[str] = set()
        self.created: list[str] = []

    def get(self, url, headers=None, params=None, timeout=None, **_):
        if url.endswith("/Accounts"):
            return _Resp(200, {"Accounts": self.accounts})
        if url.endswith("/Contacts"):
            return _Resp(200, {"Contacts": self.contacts if (params or {}).get("page", 1) == 1 else []})
        raise AssertionError(url)

    def post(self, url, headers=None, json=None, timeout=None, **_):
        name = json["Contacts"][0]["Name"]
        if name in self.refuse_create:
            return _Resp(400, {"Message": "refused"})
        self.created.append(name)
        return _Resp(200, {"Contacts": [{"ContactID": f"c-new-{len(self.created)}", "Name": name}]})


@pytest.fixture
def xero(monkeypatch) -> FakeXero:
    fake = FakeXero()
    monkeypatch.setattr(requests, "get", fake.get)
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(xero_matching, "resolve_entity_access_token", lambda entity_id: ("tok", ORG))
    from hub import xero_oauth

    monkeypatch.setattr(xero_oauth, "start_background_sync", lambda *a, **k: None)
    return fake


def own(entity, code, name, xero_type, minty_type, **extra):
    return AccountInfo.objects.create(entity_id=entity.id, code=code, xero_code=code, name=name, type=xero_type,
                                      account_type=minty_type, status="ACTIVE", source="template", **extra)


@pytest.fixture
def minty(shop, xero):
    """Olive & Vine keeping its books in Minty: its own chart and daily closing set (Cash Sales
    included)."""
    entity = shop["entity"]
    Entity.objects.filter(id=entity.id).update(setup_mode="minty")
    rows = {
        "petty": own(entity, "1000", "Petty Cash", "BANK", "bank"),
        "bank": own(entity, "1001", "Bank Account", "BANK", "bank", bank_account_number="123-456"),
        "rent": own(entity, "469", "Rent", "EXPENSE", "expense"),
        "clean": own(entity, "420", "Cleaning Expense", "EXPENSE", "expense"),   # same code, other type
        "wages_ft": own(entity, "6001", "Salaries - Full Time", "EXPENSE", "expense"),
        "wages_pt": own(entity, "6002", "Salaries - Part Time", "EXPENSE", "expense"),
        "sales": own(entity, "4001", "Online Sales", "REVENUE", "revenue"),
        "unused": own(entity, "6999", "Licences", "EXPENSE", "expense"),
        "director": own(entity, "2100", "Amount Due to Director", "CURRLIAB", "current_liability"),
    }
    EntityPettycashSettings.objects.create(entity_id=entity.id, pettycash_account_id=rows["petty"].id,
                                           discrepancy_bank_account_id=rows["petty"].id,
                                           bank_account_id=rows["bank"].id, director_account_id=rows["director"].id,
                                           discrepancy_account_id=rows["wages_pt"].id,
                                           cash_sale_account_id=rows["sales"].id)
    EntityBillAccountXero.objects.create(entity_id=entity.id, account_code="6001", account_name="Salaries - Full Time",
                                         account_type="EXPENSE", xero_account_id="", is_active=True)
    entity.refresh_from_db()
    matching.begin(entity.id, "tok", ORG)
    Entity.objects.filter(id=entity.id).update(xero_org_id=ORG, xero_tenant_name="Olive Ltd", status="disconnected")
    entity.refresh_from_db()
    return {"entity": entity, "admin": shop["admin"], "cashier": shop["cashier"], **rows}


def _copy(guid):
    return XeroAccount.objects.get(xero_guid=guid, xero_org_id=ORG)


def _pick(m, row, guid):
    return call("patch", BASE, m["admin"], m["entity"].id,
                {"account_id": str(m[row].id), "xero_account_id": str(_copy(guid).id) if guid else None})


def _rows(body):
    return {r["name"]: r for g in body["groups"] for r in g["rows"]}


# ---- begin and the page ------------------------------------------------------------------------------


def test_a_connect_picks_same_code_and_type_only_and_reads_the_page(minty):
    drafts = {d.account_id: d for d in AccountMatchDraft.objects.filter(entity_id=minty["entity"].id)}
    assert set(drafts) == {minty["rent"].id}, "469 Rent = 469 Rent; 420 Cleaning is another type"
    assert drafts[minty["rent"].id].picked_by is None

    body = call("get", BASE, minty["admin"], minty["entity"].id).json()
    rows = _rows(body)
    assert (body["matched"], body["total"]) == (1, 9)
    assert rows["Rent"]["pick"]["auto"] is True
    assert rows["Cleaning Expense"]["conflict"] == {"code": "420", "name": "Cleaning"}
    assert rows["Bank Account"]["suggestion"] == str(_copy("x-bank").id), "a bank by its number"
    assert rows["Online Sales"]["used"] is True, "daily closing's cash sale account"
    assert rows["Salaries - Part Time"]["used"] is True, "daily closing's discrepancy account"
    assert rows["Licences"]["used"] is False
    assert body["will_archive"] == ["Cleaning Expense", "Licences", "Salaries - Full Time"]
    assert [g["title"] for g in body["groups"]] == ["Cash & bank", "Sales", "Running the business",
                                                     "Owed to the director"]


def test_a_cashier_can_read_but_not_pick(minty):
    assert call("get", BASE, minty["cashier"], minty["entity"].id).status_code in (200, 403)
    assert _pick({**minty, "admin": minty["cashier"]}, "bank", "x-bank").status_code == 403


def test_every_matching_method_is_one_a_browser_may_send(minty):
    """The page runs in minty-web: a method the hub's CORS answer leaves out is never sent (the pick
    was a PUT, and the browser refused it before it reached here)."""
    from hub.transport import METHODS

    for method in ("GET", "PATCH", "POST"):
        assert method in METHODS


def test_a_pick_must_fit(minty):
    resp = _pick(minty, "bank", "x-rent")
    assert (resp.status_code, resp.json()["error"]) == (400, matching.WRONG_KIND)
    assert _pick(minty, "bank", "x-bank").status_code == 200
    assert _pick(minty, "bank", None).status_code == 200
    assert not AccountMatchDraft.objects.filter(account_id=minty["bank"].id).exists()


# ---- finish -----------------------------------------------------------------------------------------


def test_finish_is_refused_while_a_used_account_has_no_match(minty):
    resp = call("post", BASE + "/finish", minty["admin"], minty["entity"].id)
    assert resp.status_code == 409 and "still need a Xero match" in resp.json()["error"]
    assert Entity.objects.get(id=minty["entity"].id).setup_mode == "minty"
    assert not AccountChangeLog.objects.filter(entity_id=minty["entity"].id).exists()


def _match_everything_used(m):
    for row, guid in (("petty", "x-pc"), ("bank", "x-bank"), ("wages_ft", "x-wages"), ("wages_pt", "x-wages"),
                      ("sales", "x-sales"), ("director", "x-dir")):
        assert _pick(m, row, guid).status_code == 200, row


def test_finish_links_in_place_combines_archives_and_logs(minty, xero):
    _match_everything_used(minty)
    resp = call("post", BASE + "/finish", minty["admin"], minty["entity"].id)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert (body["connected"], body["failed_contacts"]) == (True, [])

    entity = Entity.objects.get(id=minty["entity"].id)
    assert (entity.setup_mode, entity.status) == ("xero", "connected")
    assert entity.xero_publish_from == datetime.now(ZoneInfo("Asia/Hong_Kong")).date()

    petty = AccountInfo.objects.get(id=minty["petty"].id)
    assert (petty.xero_account_id, petty.xero_code, petty.name) == ("x-pc", "090", "Petty Cash")
    settings = EntityPettycashSettings.objects.get(entity_id=entity.id)
    assert settings.pettycash_account_id == petty.id, "the same row: daily closing still points at it"

    sales = AccountInfo.objects.get(id=minty["sales"].id)
    assert (sales.xero_account_id, sales.xero_code) == ("x-sales", "200")
    assert settings.cash_sale_account_id == sales.id
    assert AccountModule.objects.get(account_id=sales.id, module_code="PETTY_CASH").is_active

    full, part = AccountInfo.objects.get(id=minty["wages_ft"].id), AccountInfo.objects.get(id=minty["wages_pt"].id)
    survivor, merged = (full, part) if full.merged_into_id is None else (part, full)
    assert survivor.xero_account_id == "x-wages" and merged.merged_into_id == survivor.id
    assert merged.record_status == "archived"
    assert settings.discrepancy_account_id == survivor.id, "re-pointed to the survivor"
    bill_code = EntityBillAccountXero.objects.get(entity_id=entity.id, xero_account_id="x-wages")
    assert bill_code.account_code == "477", "the bill code follows the new code"

    for row in ("unused", "clean"):
        archived = AccountInfo.objects.get(id=minty[row].id)
        assert (archived.record_status, archived.xero_account_id) == ("archived", None), row

    kinds = sorted(AccountChangeLog.objects.filter(entity_id=entity.id).values_list("kind", flat=True))
    assert kinds.count("merged") == 1 and kinds.count("archived") == 2
    assert not AccountMatchDraft.objects.filter(entity_id=entity.id).exists()


def test_finish_combines_accounts_that_each_have_a_bill_code(minty, xero):
    """Both accounts picked to one Xero account had a bill code (production, 2026-11-13: 6010 Other
    Expense and 6900 Cash Over / Short to one Xero account): one bill code takes the Xero account, the
    other goes - two would break the one-row-per-Xero-account key and Finish failed every time."""
    entity = minty["entity"]
    EntityBillAccountXero.objects.filter(entity_id=entity.id, account_code="6001").update(is_default=False)
    EntityBillAccountXero.objects.create(entity_id=entity.id, account_code="6002", account_name="Salaries - Part Time",
                                         account_type="EXPENSE", xero_account_id="", is_active=True, is_default=True)
    _match_everything_used(minty)

    resp = call("post", BASE + "/finish", minty["admin"], entity.id)
    assert resp.status_code == 200, resp.content
    codes = EntityBillAccountXero.objects.filter(entity_id=entity.id)
    assert list(codes.values_list("account_code", "xero_account_id")) == [("477", "x-wages")]
    assert codes.get().is_default, "the default bill code stays the default"


# ---- suppliers ---------------------------------------------------------------------------------------


def test_suppliers_are_linked_by_name_else_created_and_a_refusal_keeps_it_disconnected(minty, xero):
    entity = minty["entity"]
    abc = XeroContactSync.objects.create(entity_id=entity.id, name="  abc supplies ", source="user")
    new = XeroContactSync.objects.create(entity_id=entity.id, name="Wing Lee Trading Co.", source="user")
    stuck = XeroContactSync.objects.create(entity_id=entity.id, name="Star Ferry Logistics", source="user")
    xero.refuse_create = {"Star Ferry Logistics"}
    _match_everything_used(minty)

    body = call("post", BASE + "/finish", minty["admin"], entity.id).json()
    assert (body["connected"], body["failed_contacts"]) == (False, ["Star Ferry Logistics"])
    assert XeroContactSync.objects.get(id=abc.id).xero_contact_id == "c-abc", "case and spaces ignored"
    assert XeroContactSync.objects.get(id=new.id).xero_contact_id.startswith("c-new-")
    assert Entity.objects.get(id=entity.id).status == "disconnected"
    page = call("get", "/api/me/company/integration", minty["admin"], entity.id).json()
    assert page["matching"] == {"accounts": False, "contacts": True, "reason": None}

    xero.refuse_create = set()
    retry = call("post", BASE + "/contacts", minty["admin"], entity.id).json()
    assert (retry["connected"], retry["failed_contacts"]) == (True, [])
    assert XeroContactSync.objects.get(id=stuck.id).xero_contact_id
    kinds = sorted(ContactChangeLog.objects.filter(entity_id=entity.id).values_list("kind", flat=True))
    assert kinds == ["created_in_xero", "created_in_xero", "linked"]
    assert Entity.objects.get(id=entity.id).status == "connected"


def test_the_integration_page_says_matching_is_pending(minty):
    page = call("get", "/api/me/company/integration", minty["admin"], minty["entity"].id).json()
    assert page["setup_mode"] == "minty" and page["own_accounts"] == 9
    assert page["matching"] == {"accounts": True, "contacts": False, "reason": "first"}


def test_petty_cash_and_the_bank_cant_become_one_xero_account(minty):
    """Daily closing moves cash from the float to the bank: combined, both would be one account
    (onboarding's rule too - accounting.SAME_BANK)."""
    assert _pick(minty, "petty", "x-pc").status_code == 200
    resp = _pick(minty, "bank", "x-pc")
    assert (resp.status_code, resp.json()["error"]) == (400, matching.SAME_BANK)
    assert not AccountMatchDraft.objects.filter(account_id=minty["bank"].id).exists()

    # a draft that got there anyway (an older page) is refused at Finish, nothing written
    AccountMatchDraft.objects.create(account_id=minty["bank"].id, entity_id=minty["entity"].id,
                                     xero_account_id=_copy("x-pc").id, picked_by=None)
    for row, guid in (("wages_ft", "x-wages"), ("wages_pt", "x-wages"), ("sales", "x-sales"), ("director", "x-dir")):
        _pick(minty, row, guid)
    resp = call("post", BASE + "/finish", minty["admin"], minty["entity"].id)
    assert (resp.status_code, resp.json()["error"]) == (409, matching.SAME_BANK)
    assert Entity.objects.get(id=minty["entity"].id).setup_mode == "minty"


def test_a_sync_while_matching_only_refreshes_the_copies(minty, xero):
    """Petty Cash Settings asks for a sync: it must not import Xero's chart as accounts beside the
    company's own (they would hold the Xero ids Finish links)."""
    from xero.services import sync

    sync.sync_company(minty["entity"].id, "tok", ORG)
    assert not AccountInfo.objects.filter(entity_id=minty["entity"].id, xero_account_id__isnull=False).exists()
    assert not XeroContactSync.objects.filter(entity_id=minty["entity"].id, xero_contact_id__isnull=False).exists()


def test_finish_folds_an_unused_xero_row_that_holds_a_picked_account(minty):
    """A sync from before this guard left Xero's chart as rows: an unused one holding a picked Xero
    account is removed, not a reason to refuse."""
    AccountInfo.objects.create(entity_id=minty["entity"].id, name="Petty Cash", type="BANK", xero_code="090",
                               xero_account_id="x-pc", status="ACTIVE", source="xero")
    _match_everything_used(minty)
    resp = call("post", BASE + "/finish", minty["admin"], minty["entity"].id)
    assert resp.status_code == 200, resp.content
    rows = AccountInfo.objects.filter(entity_id=minty["entity"].id, xero_account_id="x-pc")
    assert list(rows.values_list("id", flat=True)) == [minty["petty"].id], "the company's own row carries it"


def test_a_disconnect_after_matching_keeps_the_xero_codes(minty, xero):
    """Owner, 2026-11-12: disconnected, the company's accounts keep their Xero (matched) codes and
    names - nothing reverts to the Minty codes; only removing an account takes it away."""
    from xero.services.disconnect import disconnect_entity_from_xero

    _match_everything_used(minty)
    assert call("post", BASE + "/finish", minty["admin"], minty["entity"].id).status_code == 200
    rent_before = AccountInfo.objects.get(id=minty["rent"].id)

    disconnect_entity_from_xero(minty["entity"].id)

    entity = Entity.objects.get(id=minty["entity"].id)
    assert (entity.status, entity.setup_mode, entity.xero_org_id) == ("disconnected", "xero", ORG)
    for row, code, name in (("petty", "090", "Petty Cash"), ("sales", "200", "Sales"), ("rent", "469", "Rent")):
        account = AccountInfo.objects.get(id=minty[row].id)
        assert (account.xero_code, account.code, account.name, account.record_status) == (code, code, name,
                                                                                          "active"), row
    assert AccountInfo.objects.get(id=minty["rent"].id).xero_account_id == rent_before.xero_account_id
    assert not AccountInfo.objects.filter(entity_id=entity.id, code="1000").exists(), "no Minty code comes back"

# ---- Phase 3 (owner, 2026-11-12): a new organisation is matched from scratch, older records stay with
# the old one (xero_publish_from reset); changing the matches reuses the page, prefilled, and affects
# only what is published from then on; while any matching is open the sync only refreshes the copies.

TODAY = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
ORG2 = "org-two"


def _copy_in(guid, org=ORG):
    return XeroAccount.objects.get(xero_guid=guid, xero_org_id=org)


@pytest.fixture
def connected(minty, xero):
    """Phase 2 done: matched, connected, publishing from an earlier day."""
    _match_everything_used(minty)
    assert call("post", BASE + "/finish", minty["admin"], minty["entity"].id).status_code == 200
    Entity.objects.filter(id=minty["entity"].id).update(xero_publish_from=date(2026, 1, 1))
    minty["entity"].refresh_from_db()
    return minty


def _pick_in(m, row, guid, org=ORG):
    return call("patch", BASE, m["admin"], m["entity"].id,
                {"account_id": str(m[row].id), "xero_account_id": str(_copy_in(guid, org).id) if guid else None})


# ---- changing the matches ------------------------------------------------------------------------------


def test_changing_starts_prefilled_and_cancel_leaves_everything(connected):
    entity, admin = connected["entity"], connected["admin"]
    resp = call("post", BASE + "/change", admin, entity.id)
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["reason"] == "change" and body["matched"] == body["total"]
    assert _rows(body)["Rent"]["pick"]["xero_account_id"] == str(_copy_in("x-rent").id)
    assert Entity.objects.get(id=entity.id).status == "connected", "publishing goes on"

    page = call("get", "/api/me/company/integration", admin, entity.id).json()
    assert page["matching"]["reason"] == "change" and page["matching"]["accounts"] is True

    cancel = call("post", BASE + "/cancel", admin, entity.id)
    assert cancel.status_code == 200, cancel.content
    assert Entity.objects.get(id=entity.id).xero_matching is None
    assert not AccountMatchDraft.objects.filter(entity_id=entity.id).exists()


def test_cancel_is_only_for_changing(minty):
    resp = call("post", BASE + "/cancel", minty["admin"], minty["entity"].id)
    assert resp.status_code == 409


def test_a_change_can_swap_two_accounts_and_keeps_the_publish_date(connected):
    entity, admin = connected["entity"], connected["admin"]
    wages = AccountInfo.objects.get(entity_id=entity.id, xero_account_id="x-wages")
    connected["wages"] = wages
    assert call("post", BASE + "/change", admin, entity.id).status_code == 200
    assert _pick_in(connected, "rent", "x-wages").status_code == 200
    assert _pick_in(connected, "wages", "x-rent").status_code == 200

    resp = call("post", BASE + "/finish", admin, entity.id)
    assert resp.status_code == 200, resp.content
    assert AccountInfo.objects.get(id=connected["rent"].id).xero_account_id == "x-wages"
    assert AccountInfo.objects.get(id=wages.id).xero_account_id == "x-rent"
    company = Entity.objects.get(id=entity.id)
    assert (company.xero_matching, company.status) == (None, "connected")
    assert company.xero_publish_from == date(2026, 1, 1), "only what is published from now on"
    latest = AccountChangeLog.objects.filter(entity_id=entity.id).order_by("-changed_at").first()
    changed = AccountChangeLog.objects.filter(batch_id=latest.batch_id)
    assert sorted(changed.values_list("account_id", flat=True)) == sorted([connected["rent"].id, wages.id]), (
        "the unchanged matches write no log line")


def test_finishing_a_change_leaves_the_suppliers_alone(connected, xero):
    """A change is about the accounts: its Finish creates nothing in Xero, whatever suppliers of an
    older organisation the company still holds."""
    entity = connected["entity"]
    XeroContactSync.objects.create(entity_id=entity.id, name="Old Supplier", xero_contact_id="c-old",
                                   xero_org_id="org-old")
    assert call("post", BASE + "/change", connected["admin"], entity.id).status_code == 200
    resp = call("post", BASE + "/finish", connected["admin"], entity.id)
    assert resp.status_code == 200, resp.content
    assert resp.json()["connected"] is True and xero.created == []
    assert Entity.objects.get(id=entity.id).status == "connected"


def test_only_a_connected_company_can_change_its_matches(minty):
    resp = call("post", BASE + "/change", minty["admin"], minty["entity"].id)
    assert resp.status_code == 409


def test_a_cashier_cannot_change_the_matches(connected):
    resp = call("post", BASE + "/change", connected["cashier"], connected["entity"].id)
    assert resp.status_code == 403


# ---- a new organisation ------------------------------------------------------------------------------


def test_a_new_organisation_is_matched_from_scratch_and_older_records_stay_behind(connected, xero, monkeypatch):
    entity, admin = connected["entity"], connected["admin"]
    before = set(AccountInfo.objects.filter(entity_id=entity.id).values_list("id", "record_status"))
    xero.accounts = [{**a, "AccountID": a["AccountID"].replace("x-", "y-")} for a in xero.accounts]
    monkeypatch.setattr(xero_matching, "resolve_entity_access_token", lambda entity_id: ("tok", ORG2))
    with transaction.atomic():
        matching.begin(entity.id, "tok", ORG2, reason="new_org")
        Entity.objects.filter(id=entity.id).update(xero_org_id=ORG2, status="disconnected")

    assert set(AccountInfo.objects.filter(entity_id=entity.id).values_list("id", "record_status")) == before, (
        "nothing archived or deleted before Finish")
    body = call("get", BASE, admin, entity.id).json()
    assert body["reason"] == "new_org"
    assert _rows(body)["Rent"]["pick"]["xero_account_id"] == str(_copy_in("y-rent", ORG2).id), "same code and type"

    for row, guid in (("petty", "y-pc"), ("bank", "y-bank"), ("sales", "y-sales"), ("director", "y-dir")):
        assert _pick_in(connected, row, guid, ORG2).status_code == 200, row
    connected["survivor"] = AccountInfo.objects.get(entity_id=entity.id, xero_account_id="x-wages")
    assert _pick_in(connected, "survivor", "y-wages", ORG2).status_code == 200
    resp = call("post", BASE + "/finish", admin, entity.id)
    assert resp.status_code == 200, resp.content

    company = Entity.objects.get(id=entity.id)
    assert (company.xero_matching, company.xero_publish_from) == (None, TODAY), "older records stay behind"
    assert AccountInfo.objects.get(id=connected["petty"].id).xero_account_id == "y-pc"
    assert AccountInfo.objects.get(id=connected["rent"].id).xero_account_id == "y-rent"


def test_a_sync_while_any_matching_is_open_only_refreshes_the_copies(connected, xero):
    entity = connected["entity"]
    assert call("post", BASE + "/change", connected["admin"], entity.id).status_code == 200
    xero.accounts = [*xero.accounts, {"AccountID": "x-new", "Code": "499", "Name": "Sundries", "Type": "EXPENSE",
                                      "Status": "ACTIVE"}]
    sync.sync_company(entity.id, "tok", ORG)
    assert not AccountInfo.objects.filter(entity_id=entity.id, xero_account_id="x-new").exists()
    assert XeroAccount.objects.filter(entity_id=entity.id, xero_guid="x-new").exists()

