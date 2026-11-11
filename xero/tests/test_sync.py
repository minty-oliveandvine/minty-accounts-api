"""The one Xero account/contact sync (``xero/services/sync.py``) and ``POST /api/internal/xero/sync``.

Flask's rules (``entity/services/settings.py``'s account and contact syncs and the two mirrors),
restated against real rows; Xero stubbed at ``requests.get``. Postgres only (the sync's advisory lock).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
import requests
from django.conf import settings
from django.test import Client
from minty_db.models import (
    AccountInfo,
    EntityAccountXero,
    EntityBillAccountXero,
    UserToken,
    XeroContactSync,
)

from xero.services import sync
from xero.tests.test_internal_token import TENANT, make_entity, make_user

pytestmark = pytest.mark.skipif(not os.environ.get("MINTY_TEST_PG_URI"), reason="Postgres only (the sync lock)")


def acct(aid, code, name, kind="EXPENSE", **extra):
    return {"AccountID": aid, "Code": code, "Name": name, "Type": kind, "Status": "ACTIVE", **extra}


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


class FakeXero:
    def __init__(self):
        self.accounts: list | None = []
        self.contact_pages: list[list] = [[]]
        self.fail_contacts_page: int | None = None
        self.asked: list[tuple[str, dict]] = []

    def get(self, url, headers=None, params=None, timeout=None, **_):
        assert headers["Xero-Tenant-Id"] == TENANT and timeout == sync.TIMEOUT
        self.asked.append((url.rsplit("/", 1)[-1], params))
        if url.endswith("/Accounts"):
            assert params == {"where": 'Status=="ACTIVE"'}
            return _Resp(500, {}) if self.accounts is None else _Resp(200, {"Accounts": self.accounts})
        page = params["page"]
        if page == self.fail_contacts_page:
            return _Resp(503, {})
        found = self.contact_pages[page - 1] if page <= len(self.contact_pages) else []
        return _Resp(200, {"Contacts": found})


@pytest.fixture
def xero(monkeypatch) -> FakeXero:
    fake = FakeXero()
    monkeypatch.setattr(requests, "get", fake.get)
    return fake


@pytest.fixture
def company(db):
    return make_entity()


def _accounts(entity):
    return {a.xero_account_id: a for a in AccountInfo.objects.filter(entity_id=entity.id)}


def _run(entity):
    return sync.sync_company(entity.id, "tok", TENANT)


# ---- accounts --------------------------------------------------------------------------------


def test_new_accounts_are_added_and_mirrored_into_both_lists(company, xero):
    xero.accounts = [acct("a-exp", "429", "General"), acct("a-fix", "710", "Equipment", "FIXED"),
                     acct("a-bank", "090", "Bank", "BANK", BankAccountNumber="123456789")]

    result = _run(company)

    assert result["accounts"] == {"added": 3, "activated": 0, "refreshed": 0, "archived": 0, "deleted": 0}
    rows = _accounts(company)
    assert (rows["a-bank"].bank_account_number, rows["a-bank"].status) == ("123456789", "ACTIVE")
    assert set(EntityAccountXero.objects.filter(account__entity_id=company.id)
               .values_list("xero_account_id", flat=True)) == {"a-exp"}  # Petty Cash's types only
    assert set(EntityBillAccountXero.objects.filter(entity_id=company.id)
               .values_list("xero_account_id", flat=True)) == {"a-exp", "a-fix"}


def test_an_account_xero_no_longer_has_active_is_deleted_with_its_rows(company, xero):
    xero.accounts = [acct("a-1", "429", "General"), acct("a-2", "430", "Travel")]
    _run(company)
    xero.accounts = [acct("a-1", "429", "General")]

    result = _run(company)

    assert result["accounts"]["deleted"] == 1
    assert set(_accounts(company)) == {"a-1"}
    assert not EntityAccountXero.objects.filter(xero_account_id="a-2").exists()
    assert not EntityBillAccountXero.objects.filter(entity_id=company.id, xero_account_id="a-2").exists()


def test_a_stale_archived_row_always_goes_and_comes_back_if_xero_has_it_active(company, xero):
    AccountInfo.objects.create(entity_id=company.id, xero_account_id="a-1", xero_code="429", name="Old",
                               type="EXPENSE", status="ARCHIVED")
    xero.accounts = [acct("a-1", "429", "General")]
    _run(company)
    assert _accounts(company)["a-1"].status == "ACTIVE"


def test_renamed_or_recoded_accounts_are_refreshed_but_not_an_excluded_type(company, xero):
    xero.accounts = [acct("a-1", "429", "General"), acct("a-bank", "090", "Bank", "BANK")]
    _run(company)
    xero.accounts = [acct("a-1", "431", "General Costs"), acct("a-bank", "091", "Main Bank", "BANK")]

    result = _run(company)

    rows = _accounts(company)
    assert (rows["a-1"].xero_code, rows["a-1"].name) == ("431", "General Costs")
    assert (rows["a-bank"].xero_code, rows["a-bank"].name) == ("090", "Bank")  # Flask: BANK isn't refreshed
    assert result["accounts"]["refreshed"] == 1
    assert EntityBillAccountXero.objects.get(entity_id=company.id, xero_account_id="a-1").account_code == "431"


def test_an_inactive_row_is_re_activated_and_the_petty_cash_tick_is_kept(company, xero):
    xero.accounts = [acct("a-1", "429", "General")]
    _run(company)
    AccountInfo.objects.filter(xero_account_id="a-1").update(status="INACTIVE")
    EntityAccountXero.objects.filter(xero_account_id="a-1").update(is_active=False)

    assert _run(company)["accounts"]["activated"] == 1
    assert _accounts(company)["a-1"].status == "ACTIVE"
    assert EntityAccountXero.objects.get(xero_account_id="a-1").is_active is False  # the person's tick


@pytest.mark.parametrize("answer", [None, []])
def test_a_failed_or_empty_account_fetch_changes_nothing(company, xero, answer):
    AccountInfo.objects.create(entity_id=company.id, xero_account_id="a-1", xero_code="429", name="General",
                               type="EXPENSE", status="ACTIVE")
    xero.accounts = answer
    assert _run(company)["accounts"] is None
    assert set(_accounts(company)) == {"a-1"}


# ---- contacts --------------------------------------------------------------------------------


def test_contacts_are_added_and_renamed_never_removed(company, xero):
    XeroContactSync.objects.create(entity_id=company.id, xero_contact_id="c-1", name="Old Name")
    XeroContactSync.objects.create(entity_id=company.id, xero_contact_id="c-gone", name="Archived In Xero")
    xero.contact_pages = [[{"ContactID": "c-1", "Name": "New Name"}, {"ContactID": "c-2", "Name": "ABC Supplies"}]]

    assert _run(company)["contacts"] == {"added": 1, "renamed": 1}
    names = dict(XeroContactSync.objects.filter(entity_id=company.id).values_list("xero_contact_id", "name"))
    assert names == {"c-1": "New Name", "c-2": "ABC Supplies", "c-gone": "Archived In Xero"}
    assert XeroContactSync.objects.get(xero_contact_id="c-2").xero_org_id == TENANT


def test_contacts_come_page_by_page_and_a_failed_page_changes_nothing(company, xero, monkeypatch):
    monkeypatch.setattr(sync, "CONTACTS_PAGE", 2)
    xero.contact_pages = [[{"ContactID": "c-1", "Name": "A"}, {"ContactID": "c-2", "Name": "B"}],
                          [{"ContactID": "c-3", "Name": "C"}]]
    xero.fail_contacts_page = 2
    assert _run(company)["contacts"] is None
    assert not XeroContactSync.objects.filter(entity_id=company.id).exists()

    xero.fail_contacts_page = None
    assert _run(company)["contacts"]["added"] == 3
    assert [p["page"] for name, p in xero.asked if name == "Contacts"][-2:] == [1, 2]


# ---- the route -------------------------------------------------------------------------------


def _assertion(entity_id, scope="xero-sync", minutes=1):
    return jwt.encode({"entity_id": str(entity_id), "scope": scope,
                       "exp": datetime.now(UTC) + timedelta(minutes=minutes)}, settings.SECRET_KEY, algorithm="HS256")


def _post(entity_id, **kw):
    return Client().post("/api/internal/xero/sync", HTTP_AUTHORIZATION="Bearer " + _assertion(entity_id, **kw))


def test_the_route_syncs_the_assertions_company(db, xero):
    owner = make_user()
    entity = make_entity(connected_by=owner)
    UserToken.objects.create(id=uuid.uuid4(), user_id=owner.id, access_token="tok", refresh_token="r",
                             access_token_expires_in=1800, access_token_obtained_at=datetime.now(UTC))
    xero.accounts = [acct("a-1", "429", "General")]

    resp = _post(entity.id)

    assert resp.status_code == 200
    assert resp.json() == {"accounts": {"added": 1, "activated": 0, "refreshed": 0, "archived": 0, "deleted": 0},
                           "contacts": {"added": 0, "renamed": 0}}


def test_the_route_refuses_a_token_for_another_route_and_a_company_with_no_connection(db, xero):
    entity = make_entity()
    assert _post(entity.id, scope="xero-access-token").status_code == 401
    resp = _post(entity.id)
    assert (resp.status_code, resp.json()) == (409, {"error": "Xero needs to be connected again.", "reconnect": True})
    assert xero.asked == []


# ---- the Xero copies and the links (Xero optional, EXPAND, 2026-11-10) -------------------------


def _copies(entity):
    from minty_db.models import XeroAccount

    return {m.xero_guid: m for m in XeroAccount.objects.filter(entity_id=entity.id, xero_org_id=TENANT)}


def test_each_account_gets_its_xero_copy_and_is_linked_to_it(company, xero):
    xero.accounts = [acct("a-exp", "429", "General", Class="EXPENSE"),
                     acct("a-bank", "Business Bank Account", "Business Bank Account", "BANK")]

    _run(company)

    copies, rows = _copies(company), _accounts(company)
    assert set(copies) == {"a-exp", "a-bank"}
    exp = copies["a-exp"]
    assert (exp.code, exp.name, exp.xero_type, exp.xero_class, exp.status) == ("429", "General", "EXPENSE", "EXPENSE",
                                                                                  "ACTIVE")
    assert copies["a-bank"].code is None, "a bank account's name in the code field is not a code"
    assert rows["a-exp"].xero_account_link_id == exp.id
    assert rows["a-bank"].xero_account_link_id == copies["a-bank"].id


def test_a_recode_follows_and_an_account_xero_dropped_is_marked_deleted_in_the_copy(company, xero):
    xero.accounts = [acct("a-1", "429", "General"), acct("a-2", "430", "Travel")]
    _run(company)
    first = _copies(company)
    xero.accounts = [acct("a-1", "469", "Rent")]

    _run(company)

    copies = _copies(company)
    assert copies["a-1"].id == first["a-1"].id, "the same copy, upserted by Xero id"
    assert (copies["a-1"].code, copies["a-1"].name, copies["a-1"].status) == ("469", "Rent", "ACTIVE")
    assert copies["a-2"].status == "DELETED", "kept, as the sync marks what Xero no longer sends"


def test_each_contact_gets_its_copy_and_a_minty_supplier_is_left_alone(company, xero):
    from minty_db.models import XeroContact

    XeroContactSync.objects.create(entity_id=company.id, name="Kept In Minty", source="user")
    xero.contact_pages = [[{"ContactID": "c-1", "Name": "ABC Supplies"}]]
    _run(company)
    xero.contact_pages = [[{"ContactID": "c-1", "Name": "ABC Supplies Ltd"}]]
    _run(company)

    copy = XeroContact.objects.get(entity_id=company.id, xero_guid="c-1")
    assert (copy.name, copy.xero_org_id) == ("ABC Supplies Ltd", TENANT)
    assert XeroContactSync.objects.get(xero_contact_id="c-1").xero_contact_link_id == copy.id
    minty = XeroContactSync.objects.get(entity_id=company.id, name="Kept In Minty")
    assert (minty.xero_contact_id, minty.xero_contact_link_id) == (None, None)


def test_a_disconnect_keeps_every_account_and_the_copies(company, xero):
    """Xero's accounts and the company's own stay (a disconnect keeps the organisation, 2026-11-10)."""
    from xero.services.disconnect import disconnect_entity_from_xero

    xero.accounts = [acct("a-1", "429", "General")]
    _run(company)
    own = AccountInfo.objects.create(entity_id=company.id, name="Petty Cash", type="BANK", code="090",
                                     xero_code="090", account_type="bank", status="ACTIVE", source="user")

    disconnect_entity_from_xero(company.id)

    kept = set(AccountInfo.objects.filter(entity_id=company.id).values_list("xero_account_id", "id"))
    assert {x for x, _ in kept} == {"a-1", None} and own.id in {i for _, i in kept}
    assert set(_copies(company)) == {"a-1"}, "the Xero copies stay, keyed by the organisation"


# ---- nothing used, and nothing Minty made, is deleted (2026-11-11, Settings Phase 2) ----------------


def test_a_used_account_xero_dropped_is_archived_not_deleted(company, xero):
    from minty_db.models import EntityPettycashSettings

    xero.accounts = [acct("a-1", "429", "General"), acct("a-2", "430", "Travel")]
    _run(company)
    travel = _accounts(company)["a-2"]
    EntityPettycashSettings.objects.create(entity_id=company.id, discrepancy_account_id=travel.id)
    xero.accounts = [acct("a-1", "429", "General")]

    result = _run(company)

    assert (result["accounts"]["archived"], result["accounts"]["deleted"]) == (1, 0)
    travel.refresh_from_db()
    assert travel.status == "ARCHIVED"
    assert EntityPettycashSettings.objects.get(entity_id=company.id).discrepancy_account_id == travel.id

    xero.accounts = [acct("a-1", "429", "General"), acct("a-2", "430", "Travel")]
    _run(company)
    travel.refresh_from_db()
    assert travel.status == "ACTIVE", "Xero has it again: the same row comes back"


def test_a_minty_account_and_a_hand_added_bill_code_are_never_touched(company, xero):
    own = AccountInfo.objects.create(entity_id=company.id, name="Rent", code="420", xero_code="420",
                                     account_type="expense", type="EXPENSE", status="ACTIVE", source="user")
    EntityBillAccountXero.objects.create(entity_id=company.id, account_code="420", account_name="Rent",
                                         account_type="EXPENSE", xero_account_id="", is_active=True)
    xero.accounts = [acct("a-1", "429", "General")]

    _run(company)

    own.refresh_from_db()
    assert (own.status, own.xero_account_id) == ("ACTIVE", None)
    assert EntityBillAccountXero.objects.filter(entity_id=company.id, account_code="420").exists()


def test_the_connect_sync_keeps_a_minty_supplier(company, xero):
    from xero.services.connect import background_sync

    XeroContactSync.objects.create(entity_id=company.id, name="Kept In Minty", source="user")
    XeroContactSync.objects.create(entity_id=company.id, xero_contact_id="c-old", name="Gone From Xero")
    xero.contact_pages = [[{"ContactID": "c-1", "Name": "ABC Supplies"}]]
    xero.accounts = [acct("a-1", "429", "General")]

    background_sync(company.id, "tok", TENANT)

    names = set(XeroContactSync.objects.filter(entity_id=company.id).values_list("name", flat=True))
    assert names == {"Kept In Minty", "ABC Supplies"}, "an unused Xero contact Xero dropped still goes"
