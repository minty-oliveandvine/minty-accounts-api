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
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import requests
from minty_db.models import (
    AccountChangeLog,
    AccountInfo,
    AccountMatchDraft,
    AccountModule,
    ContactChangeLog,
    Entity,
    EntityBillAccountXero,
    EntityPettycashSettings,
    EntitySaleSetting,
    SaleInfo,
    XeroAccount,
    XeroContactSync,
)

from hub import xero_matching
from hub.tests.test_company import call
from xero.services import matching

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
    """Olive & Vine keeping its books in Minty: its own chart, daily closing set, a sales method."""
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
                                           discrepancy_account_id=rows["wages_pt"].id)
    sale = SaleInfo.objects.create(sale_name="Online Sales Matching", value_name="online_m", type="electronic")
    EntitySaleSetting.objects.create(entity_id=entity.id, sale=sale, account_id=rows["sales"].id)
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
    return call("put", BASE, m["admin"], m["entity"].id,
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
    assert rows["Online Sales"]["used"] is True, "a sales method names it (owner, 2026-11-11)"
    assert rows["Salaries - Part Time"]["used"] is True, "daily closing's discrepancy account"
    assert rows["Licences"]["used"] is False
    assert body["will_archive"] == ["Cleaning Expense", "Licences", "Salaries - Full Time"]
    assert [g["title"] for g in body["groups"]] == ["Cash & bank", "Sales", "Running the business",
                                                     "Owed to the director"]


def test_a_cashier_can_read_but_not_pick(minty):
    assert call("get", BASE, minty["cashier"], minty["entity"].id).status_code in (200, 403)
    assert _pick({**minty, "admin": minty["cashier"]}, "bank", "x-bank").status_code == 403


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
    assert EntitySaleSetting.objects.get(entity_id=entity.id, account_id=sales.id)
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
    assert page["matching"] == {"accounts": False, "contacts": True}

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
    assert page["matching"] == {"accounts": True, "contacts": False}
