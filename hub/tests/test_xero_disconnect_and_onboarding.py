"""Xero disconnect and release (``xero/services/disconnect.py``, the hub's and onboarding's routes)
and onboarding's modules and invitations (``hub/onboarding.py``) - Flask's
``test_hub_company_settings.py`` (disconnect, conflict, move), ``test_char_entities.py`` (modules,
release) and ``test_onboarding_round2.py`` (invite list, resend), restated. Xero is faked at
``requests`` and the token service; mail goes to Django's in-memory outbox."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from django.core import mail
from django.db import connection
from django.test import Client
from minty_db.models import (
    AccountInfo,
    Entity,
    EntityBillAccountXero,
    EntityFunction,
    EntityFunctionMap,
    EntityPettycashSettings,
    Invitation,
    UserToken,
    XeroContactSync,
)

from hub import invitations
from hub.notices import CONFLICT_SALT
from hub.tests.test_company import call
from hub.tests.test_public_and_transport import make_user, signed, token
from xero.services import disconnect

ONBOARDING = "http://localhost:3030"


class Xero:
    """Xero's /connections, as the disconnect sees it."""

    def __init__(self, monkeypatch, tenant="t-1"):
        self.deleted, self.tenant = [], tenant
        monkeypatch.setattr(disconnect, "resolve_entity_access_token", lambda entity_id: ("access", self.tenant))
        monkeypatch.setattr(disconnect.requests, "get", self.get)
        monkeypatch.setattr(disconnect.requests, "delete", self.delete)

    def get(self, url, headers, timeout):
        assert headers == {"Authorization": "Bearer access"}
        return type("R", (), {"status_code": 200, "json": lambda _s: [{"id": "conn-1", "tenantId": self.tenant}]})()

    def delete(self, url, headers, timeout):
        self.deleted.append(url)
        return type("R", (), {"status_code": 204})()


def hold(entity, connector, tenant="t-1", name="Xero Org Ltd"):
    """``entity`` connected to ``tenant`` by ``connector``, with a token and a cache."""
    Entity.objects.filter(id=entity.id).update(xero_org_id=tenant, xero_tenant_name=name, status="connected",
                                               connected_by_user_id=connector.id,
                                               last_connected_at=datetime(2026, 9, 1, 2, 0, tzinfo=UTC))
    UserToken.objects.update_or_create(user_id=connector.id, defaults={"access_token": "a", "refresh_token": "r",
                                                                       "id_token": "i", "access_token_expires_in": 1800})
    account = AccountInfo.objects.create(entity_id=entity.id, type="BANK", name="Petty Cash", xero_account_id="acc-1")
    contact = XeroContactSync.objects.create(entity_id=entity.id, xero_contact_id="c-1", xero_org_id=tenant,
                                             name="Cash Sale")
    EntityPettycashSettings.objects.update_or_create(entity_id=entity.id, defaults={
        "pettycash_account_id": account.id, "cash_sale_contact_id": contact.id})
    EntityBillAccountXero.objects.create(entity_id=entity.id, account_code="400", account_name="Advertising")


def tokens_of(person):
    row = UserToken.objects.get(user_id=person.id)
    return row.access_token, row.refresh_token, row.id_token


# ---- the hub's Disconnect ---------------------------------------------------------------------


def test_disconnecting_revokes_at_xero_and_keeps_the_organisation_and_its_copy(shop, monkeypatch):
    """Xero optional (2026-11-10, design doc 4): the organisation, the synced chart and contacts and the
    Petty Cash mapping all stay - the company works on in Minty and a reconnect finds them again."""
    xero = Xero(monkeypatch)
    hold(shop["entity"], shop["admin"])
    done = call("post", "/api/me/company/xero/disconnect", shop["admin"], shop["entity"].id)
    assert done.status_code == 200, done.json()
    body = done.json()
    assert body["message"] == "You're disconnected from Xero." and body["xero"]["status"] == "disconnected"
    assert (body["xero"]["connected"], body["xero"]["needs_reconnect"]) == (False, True)
    assert body["xero"]["organisation"] == "Xero Org Ltd"
    assert datetime.fromisoformat(body["xero"]["last_connected_at"]) == datetime(2026, 9, 1, 2, 0, tzinfo=UTC)
    assert xero.deleted == [f"{disconnect.CONNECTIONS_URL}/conn-1"]
    org = Entity.objects.get(id=shop["entity"].id)
    assert (org.status, org.xero_org_id, org.connected_by_user_id) == ("disconnected", "t-1", None)
    if connection.vendor == "postgresql":  # the schema's trigger derives it
        assert org.xero_status == "disconnected"
    assert tokens_of(shop["admin"]) == (None, None, None)
    assert AccountInfo.objects.filter(entity_id=org.id).exists()
    assert XeroContactSync.objects.filter(entity_id=org.id).exists()
    assert EntityBillAccountXero.objects.filter(entity_id=org.id).exists()
    mapping = EntityPettycashSettings.objects.get(entity_id=org.id)
    assert mapping.pettycash_account_id is not None and mapping.cash_sale_contact_id is not None


def test_the_connectors_tokens_stay_while_another_company_of_theirs_is_connected(shop, monkeypatch):
    Xero(monkeypatch)
    hold(shop["entity"], shop["admin"])
    hold(shop["other"], shop["admin"], tenant="t-2")
    assert call("post", "/api/me/company/xero/disconnect", shop["admin"], shop["entity"].id).status_code == 200
    assert tokens_of(shop["admin"]) == ("a", "r", "i")
    assert XeroContactSync.objects.filter(entity_id=shop["other"].id).exists()


def test_a_company_disconnected_earlier_keeps_no_tokens_alive(shop, monkeypatch):
    """Its kept organisation is not a connection: the last connected company's disconnect clears them."""
    Xero(monkeypatch)
    hold(shop["entity"], shop["admin"])
    hold(shop["other"], shop["admin"], tenant="t-2")
    Entity.objects.filter(id=shop["other"].id).update(status="disconnected")  # older rows may still name the connector
    assert call("post", "/api/me/company/xero/disconnect", shop["admin"], shop["entity"].id).status_code == 200
    assert tokens_of(shop["admin"]) == (None, None, None)


def test_xero_not_answering_still_disconnects_here(shop, monkeypatch):
    hold(shop["entity"], shop["admin"])
    monkeypatch.setattr(disconnect, "resolve_entity_access_token", lambda entity_id: ("access", "t-1"))

    def down(*args, **kwargs):
        raise disconnect.requests.ConnectionError("xero down")

    monkeypatch.setattr(disconnect.requests, "get", down)
    assert call("post", "/api/me/company/xero/disconnect", shop["admin"], shop["entity"].id).status_code == 200
    org = Entity.objects.get(id=shop["entity"].id)
    assert (org.status, org.xero_org_id, org.connected_by_user_id) == ("disconnected", "t-1", None)


def test_a_failed_disconnect_says_so_and_a_cashier_may_not(shop, monkeypatch):
    def boom(entity_id):
        raise RuntimeError("db down")

    from hub import company

    monkeypatch.setattr(company, "disconnect_entity_from_xero", boom)
    failed = call("post", "/api/me/company/xero/disconnect", shop["admin"], shop["entity"].id)
    assert failed.status_code == 502 and "may still show as connected" in failed.json()["error"]
    assert call("post", "/api/me/company/xero/disconnect", shop["cashier"], shop["entity"].id).status_code == 403


def test_a_failed_local_save_changes_nothing(shop, monkeypatch):
    Xero(monkeypatch)
    hold(shop["entity"], shop["admin"])

    def boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(disconnect, "_mark", boom)
    with pytest.raises(RuntimeError):
        disconnect.disconnect_entity_from_xero(shop["entity"].id)
    org = Entity.objects.get(id=shop["entity"].id)
    assert (org.status, org.connected_by_user_id) == ("connected", shop["admin"].id)
    assert tokens_of(shop["admin"]) == ("a", "r", "i")


# ---- a refused connect and the move it offers -------------------------------------------------


def conflict(entity, name, can_move=True):
    return signed({"from": str(entity.id), "name": name, "can_move": can_move}, CONFLICT_SALT)


@pytest.fixture(autouse=True)
def no_live_xero_check(monkeypatch):
    """The Entity & Integration page checks a held organisation at Xero; not these tests' subject."""
    from hub import company

    monkeypatch.setattr(company, "resolve_entity_access_token", lambda entity_id: None)


def integration(person, entity, xero_conflict):
    from urllib.parse import urlencode

    return Client().get(f"/api/me/company/integration?{urlencode({'entity': entity.id, 'xero_conflict': xero_conflict})}",
                        HTTP_AUTHORIZATION=f"Bearer {token(person.id)}").json()["xero_conflict"]


def test_a_refused_connect_names_the_company_holding_the_organisation(shop, settings):
    settings.DEVELOPMENT = True
    hold(shop["other"], shop["admin"])
    assert integration(shop["admin"], shop["entity"], conflict(shop["other"], "Other Co")) == {
        "entity_id": str(shop["other"].id), "entity_name": "Other Co", "organisation": "Xero Org Ltd",
        "can_move": True}


def test_the_conflict_is_settled_against_the_database_not_the_token(shop, settings):
    settings.DEVELOPMENT = True
    assert integration(shop["admin"], shop["entity"], conflict(shop["other"], "Other Co")) is None  # freed since
    hold(shop["entity"], shop["admin"])
    assert integration(shop["admin"], shop["entity"], conflict(shop["entity"], "Olive & Vine")) is None  # itself
    hold(shop["other"], shop["admin"], tenant="t-2")
    assert integration(shop["cashier"], shop["entity"], conflict(shop["other"], "Other Co"))["can_move"] is False
    Entity.objects.filter(id=shop["other"].id).update(status="disconnected", connected_by_user_id=None)
    assert integration(shop["admin"], shop["entity"], conflict(shop["other"], "Other Co")) is None  # kept, not held


def test_a_forged_conflict_offers_nothing_and_outside_development_nothing_moves(shop, settings):
    hold(shop["other"], shop["admin"])
    settings.DEVELOPMENT = True
    assert integration(shop["admin"], shop["entity"], conflict(shop["other"], "Other Co") + "x") is None
    settings.DEVELOPMENT = False
    assert integration(shop["admin"], shop["entity"], conflict(shop["other"], "Other Co"))["can_move"] is False


def release(person, entity, body):
    return call("post", "/api/me/company/xero/release", person, entity.id, body)


def test_the_move_releases_the_other_company_and_nothing_else(shop, settings, monkeypatch):
    settings.DEVELOPMENT = True
    Xero(monkeypatch)
    hold(shop["other"], shop["admin"])
    done = release(shop["admin"], shop["entity"], {"entity_id": str(shop["other"].id)})
    assert (done.status_code, done.json()) == (200, {"message": "\"Other Co\" is disconnected from Xero."})
    freed = Entity.objects.get(id=shop["other"].id)
    assert (freed.xero_org_id, freed.status) == ("t-1", "disconnected")  # kept, no longer a claim
    assert Entity.objects.get(id=shop["entity"].id).xero_org_id is None
    again = release(shop["admin"], shop["entity"], {"entity_id": str(shop["other"].id)})
    assert again.status_code == 200 and "already disconnected" in again.json()["message"]


def test_the_move_is_authorized_on_the_company_being_freed(shop, settings):
    settings.DEVELOPMENT = True
    hold(shop["other"], shop["admin"])
    body = {"entity_id": str(shop["other"].id)}
    assert release(shop["cashier"], shop["entity"], body).status_code == 403
    refused = release(shop["accountant"], shop["entity"], body)  # may change this tab, not that company
    assert (refused.status_code, refused.json()) == (
        403, {"error": "Only an accountant or admin of \"Other Co\" can disconnect it from Xero."})
    assert Entity.objects.get(id=shop["other"].id).xero_org_id == "t-1"


@pytest.mark.parametrize("body, status", [({}, 422), ("self", 422), ({"entity_id": "not-a-uuid"}, 403),
                                          ({"entity_id": "11111111-1111-1111-1111-111111111111"}, 403)])
def test_the_move_refuses_nonsense_and_self_release(shop, settings, body, status):
    settings.DEVELOPMENT = True
    if body == "self":
        body = {"entity_id": str(shop["entity"].id)}
    assert release(shop["admin"], shop["entity"], body).status_code == status


def test_a_failed_release_says_the_organisation_is_still_in_use(shop, settings, monkeypatch):
    settings.DEVELOPMENT = True
    hold(shop["other"], shop["admin"])
    from hub import company

    monkeypatch.setattr(company, "disconnect_entity_from_xero", lambda entity_id: 1 / 0)
    failed = release(shop["admin"], shop["entity"], {"entity_id": str(shop["other"].id)})
    assert failed.status_code == 502 and "still in use" in failed.json()["error"]


def test_outside_development_the_release_is_refused_and_nothing_is_revoked(shop, settings, monkeypatch):
    settings.DEVELOPMENT = False
    xero = Xero(monkeypatch)
    hold(shop["other"], shop["admin"])
    refused = release(shop["admin"], shop["entity"], {"entity_id": str(shop["other"].id)})
    assert refused.status_code == 403 and "Disconnect it from \"Other Co\" first" in refused.json()["error"]
    assert Entity.objects.get(id=shop["other"].id).xero_org_id == "t-1" and xero.deleted == []


# ---- onboarding: the door ---------------------------------------------------------------------


def onboard(method, path, person=None, body=None, **query):
    kwargs = {"HTTP_ORIGIN": ONBOARDING}
    if person is not None:
        kwargs["HTTP_AUTHORIZATION"] = f"Bearer {token(person.id, scope='onboarding')}"
    if method == "get":
        return Client().get(path, query, **kwargs)
    return getattr(Client(), method)(path, body if body is not None else {}, content_type="application/json", **kwargs)


@pytest.mark.parametrize("path", ["/api/onboarding/modules", "/api/onboarding/invite", "/api/onboarding/invite/resend",
                                  "/api/onboarding/invite/cancel", "/api/onboarding/xero/disconnect",
                                  "/api/onboarding/xero/release"])
def test_every_onboarding_route_has_the_onboarding_door(db, path):
    preflight = Client().options(path, HTTP_ORIGIN=ONBOARDING)
    assert preflight.status_code == 204
    assert (preflight["Access-Control-Allow-Origin"], preflight["Access-Control-Allow-Methods"]) == (
        ONBOARDING, "GET, POST, PUT, OPTIONS")
    refused = onboard("post", path)
    assert (refused.status_code, refused.json()) == (401, {"error": "Unauthorized"})
    assert refused["Access-Control-Allow-Origin"] == ONBOARDING


# ---- onboarding: modules ----------------------------------------------------------------------


@pytest.fixture
def wizard(db):
    for code, name in (("PETTY_CASH", "Petty Cash"), ("PAYMENT_REQUEST", "Payment Request")):
        EntityFunction.objects.create(id=uuid.uuid4(), function_code=code, function_name=name)
    admin = make_user("admin@test.com")
    company = Entity.objects.create(id=uuid.uuid4(), name="New Co", status="onboarding")
    from minty_db.models import UserEntity

    UserEntity.objects.create(user_id=admin.id, entity_id=company.id, role="admin", approved=True)
    return admin, company


def modules(person, company, **body):
    return onboard("post", "/api/onboarding/modules", person, {"entity_id": str(company.id), **body})


def test_module_selection_turns_the_chosen_modules_on_the_rest_off_and_records_who(wizard):
    admin, company = wizard
    first = modules(admin, company, modules=["PAYMENT_REQUEST"])
    assert first.status_code == 200, first.json()
    assert first.content == b'{"modules":{"PAYMENT_REQUEST":true,"PETTY_CASH":false}}\n'
    assert modules(admin, company, modules=["petty_cash", "PAYMENT_REQUEST"]).json()["modules"] == {
        "PETTY_CASH": True, "PAYMENT_REQUEST": True}
    assert modules(admin, company, module="PETTY_CASH").json()["modules"] == {"PETTY_CASH": True,
                                                                              "PAYMENT_REQUEST": False}
    rows = EntityFunctionMap.objects.filter(entity_id=company.id)
    assert {r.created_by for r in rows} == {admin.id} and rows.count() == 2
    off = rows.get(is_enabled=False)
    assert off.disabled_at is not None


@pytest.mark.parametrize("body, sentence", [
    ({"modules": ["INVOICING"]}, "Unknown module code(s): ['INVOICING']. Expected one of ['PETTY_CASH', 'PAYMENT_REQUEST']."),
    ({"modules": []}, "at least one module is required"),
    ({"entity_id": ""}, "entity_id is required"),
])
def test_bad_module_choices_are_400s(wizard, body, sentence):
    admin, company = wizard
    resp = modules(admin, company, **{"modules": ["PETTY_CASH"], **body})
    assert (resp.status_code, resp.json()) == (400, {"error": sentence})


def test_a_stranger_or_an_unapproved_member_cannot_shape_someone_elses_onboarding(wizard):
    from minty_db.models import UserEntity

    _admin, company = wizard
    stranger = make_user("stranger@test.com")
    resp = modules(stranger, company, modules=["PETTY_CASH"])
    assert (resp.status_code, resp.json()) == (403, {"error": "You don't have access to this entity"})
    UserEntity.objects.create(user_id=stranger.id, entity_id=company.id, role="admin", approved=False)
    assert modules(stranger, company, modules=["PETTY_CASH"]).status_code == 403
    assert onboard("post", "/api/onboarding/modules", stranger, {"entity_id": "nope", "modules": ["PETTY_CASH"]}
                   ).status_code == 403
    assert not EntityFunctionMap.objects.exists()


# ---- onboarding: invitations ------------------------------------------------------------------


@pytest.fixture
def invited(wizard):
    invitations.reset_cooldowns()
    yield wizard
    invitations.reset_cooldowns()


def send(person, company, **body):
    payload = {"entity_id": str(company.id), "email": "New@Test.com", "role": "Cashier", "first_name": "New",
               "last_name": "Person", **body}
    return onboard("post", "/api/onboarding/invite", person, payload)


def test_an_invitation_is_sent_and_the_list_says_when_it_expires(invited):
    admin, company = invited
    resp = send(admin, company)
    assert resp.status_code == 201, resp.json()
    body = resp.json()
    assert (body["status"], body["email_sent"]) == ("success", True)
    assert (body["invitation"]["email"], body["invitation"]["role"], body["invitation"]["status"]) == (
        "new@test.com", "cashier", "pending")
    assert mail.outbox[0].to == ["new@test.com"]
    listed, = onboard("get", "/api/onboarding/invite", admin, entity_id=str(company.id)).json()["invitations"]
    assert listed["expires_at"] and listed["expired"] is False and listed["id"] == body["invitation"]["id"]


@pytest.mark.parametrize("body, status, sentence", [
    ({"email": ""}, 400, "email and role are required."),
    ({"email": "two@@x.co"}, 400, "That doesn't look like an email address."),
    ({"role": "owner"}, 400, "Invalid role."),
])
def test_bad_invitations_are_refused_in_flasks_words(invited, body, status, sentence):
    admin, company = invited
    resp = send(admin, company, **body)
    assert (resp.status_code, resp.json()) == (status, {"error": sentence})


def test_only_those_who_may_invite_do_and_never_above_their_own_role(invited):
    from minty_db.models import UserEntity

    _admin, company = invited
    cashier, manager = make_user("cash@test.com"), make_user("mgr@test.com")
    UserEntity.objects.create(user_id=cashier.id, entity_id=company.id, role="cashier", approved=True)
    UserEntity.objects.create(user_id=manager.id, entity_id=company.id, role="shop_manager", approved=True)
    assert send(cashier, company).json() == {"error": "You do not have permission to invite users to this entity."}
    above = send(manager, company, role="admin")
    assert (above.status_code, above.json()) == (403, {"error": "You cannot assign a role higher than your own."})
    listing = onboard("get", "/api/onboarding/invite", cashier, entity_id=str(company.id))
    assert (listing.status_code, listing.json()) == (403, {"error": "Access denied"})
    assert onboard("get", "/api/onboarding/invite", manager).json() == {"error": "entity_id is required"}


def test_an_invitation_can_be_resent_after_the_cooldown_and_cancelled(invited):
    admin, company = invited
    inv_id = send(admin, company).json()["invitation"]["id"]
    soon = onboard("post", "/api/onboarding/invite/resend", admin, {"invitation_id": inv_id})
    assert soon.status_code == 429 and soon.json()["retry_after"] > 0
    invitations.reset_cooldowns()
    resent = onboard("post", "/api/onboarding/invite/resend", admin, {"invitation_id": inv_id})
    assert resent.status_code == 200 and resent.json()["status"] == "success" and len(mail.outbox) == 2
    cancelled = onboard("post", "/api/onboarding/invite/cancel", admin, {"invitation_id": inv_id})
    assert (cancelled.status_code, cancelled.json()) == (200, {"status": "success"})
    assert str(Invitation.objects.get(id=inv_id).status) == "revoked"


def test_a_stranger_cannot_resend_or_cancel_and_an_unknown_invitation_is_a_404(invited):
    admin, company = invited
    inv_id = send(admin, company).json()["invitation"]["id"]
    stranger = make_user("stranger@test.com")
    for path in ("/api/onboarding/invite/resend", "/api/onboarding/invite/cancel"):
        assert onboard("post", path, stranger, {"invitation_id": inv_id}).json() == {"error": "Not authorized."}
        assert onboard("post", path, admin, {"invitation_id": "nope"}).status_code == 404
        assert onboard("post", path, admin, {}).json() == {"error": "invitation_id is required"}


# ---- onboarding: Xero -------------------------------------------------------------------------


def test_onboardings_disconnect_leaves_the_company_in_onboarding_and_keeps_its_lists(wizard, monkeypatch):
    admin, company = wizard
    xero = Xero(monkeypatch)
    hold(company, admin)
    resp = onboard("post", "/api/onboarding/xero/disconnect", admin, {"entity_id": str(company.id)})
    assert (resp.status_code, resp.json()) == (200, {"ok": True, "connected": False})
    org = Entity.objects.get(id=company.id)
    assert (org.status, org.xero_org_id, org.connected_by_user_id) == ("onboarding", None, None)
    assert xero.deleted and tokens_of(admin) == (None, None, None)
    assert AccountInfo.objects.filter(entity_id=company.id).exists()  # kept (owner, 2026-11-06)


def test_onboardings_disconnect_refuses_who_may_not_and_says_so_on_failure(wizard, monkeypatch):
    admin, company = wizard
    stranger = make_user("stranger@test.com")
    assert onboard("post", "/api/onboarding/xero/disconnect", stranger, {"entity_id": str(company.id)}).json() == {
        "error": "Access denied"}
    assert onboard("post", "/api/onboarding/xero/disconnect", admin, {}).json() == {"error": "entity_id is required"}
    from hub import onboarding

    monkeypatch.setattr(onboarding, "disconnect_onboarding_entity", lambda entity_id: 1 / 0)
    failed = onboard("post", "/api/onboarding/xero/disconnect", admin, {"entity_id": str(company.id)})
    assert (failed.status_code, failed.json()) == (500, {"error": "Failed to disconnect from Xero. Please try again."})


def test_the_wizard_can_free_another_companys_org_but_only_with_permission(wizard, settings, monkeypatch):
    settings.DEVELOPMENT = True
    admin, company = wizard
    Xero(monkeypatch)
    hold(company, admin)
    stranger = make_user("nobody@test.com")
    assert onboard("post", "/api/onboarding/xero/release", stranger, {"entity_id": str(company.id)}
                   ).status_code == 403
    assert Entity.objects.get(id=company.id).xero_org_id == "t-1"
    assert onboard("post", "/api/onboarding/xero/release", admin, {}).status_code == 400
    done = onboard("post", "/api/onboarding/xero/release", admin, {"entity_id": str(company.id)})
    assert done.json() == {"ok": True, "released": True, "entity_name": "New Co"}
    org = Entity.objects.get(id=company.id)
    assert (org.xero_org_id, org.status) == ("t-1", "disconnected")  # kept, no longer a claim
    assert AccountInfo.objects.filter(entity_id=company.id).exists()  # and its copy with it
    again = onboard("post", "/api/onboarding/xero/release", admin, {"entity_id": str(company.id)})
    assert again.json()["released"] is False


def test_outside_development_the_wizard_cannot_free_another_companys_org(wizard, settings, monkeypatch):
    settings.DEVELOPMENT = False
    admin, company = wizard
    xero = Xero(monkeypatch)
    hold(company, admin)
    refused = onboard("post", "/api/onboarding/xero/release", admin, {"entity_id": str(company.id)})
    assert refused.status_code == 403 and "Disconnect it from" in refused.json()["error"]
    assert Entity.objects.get(id=company.id).xero_org_id == "t-1" and xero.deleted == []
