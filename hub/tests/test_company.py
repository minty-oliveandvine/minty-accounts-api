"""A company's Users and Entity & Integration tabs (``hub/company.py``, ``invitations.py``) - Flask's
``tests/test_hub_company_settings.py``, ``test_membership_removal_guards.py`` and
``test_invitation.py`` restated for the routes (their session-page and accept-link tests stay
Flask's, Phase 6). minty-subscription-api's answer is faked at ``core.subscription_client``; mail goes
to Django's in-memory outbox."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.core import mail
from django.test import Client
from minty_db.models import (
    CashInfo,
    CountryInfo,
    CurrencyInfo,
    Entity,
    EntityCashSetting,
    Invitation,
    User,
    UserEntity,
)

from core import subscription_client
from hub import company, invitations
from hub.tests.test_public_and_transport import make_user, token

WEB = "http://localhost:3000"


def call(method, path, person, entity=None, body=None):
    query = f"?entity={entity}" if entity else ""
    kwargs = {"HTTP_ORIGIN": WEB}
    if person is not None:
        kwargs["HTTP_AUTHORIZATION"] = f"Bearer {token(person.id)}"
    if body is not None:
        return getattr(Client(), method)(path + query, body, content_type="application/json", **kwargs)
    return getattr(Client(), method)(path + query, **kwargs)


def currency(code="HKD", notes=(100, 50, 10, 1, 0.5)):
    row = CurrencyInfo.objects.create(id=uuid.uuid4(), currency_code=code, currency_name=code, symbol="$")
    for order, value in enumerate(notes, start=1):
        CashInfo.objects.create(currency_id=row.id, cash_value=value, type="note" if value >= 10 else "coin",
                                cash_name=str(value), display_order=order, is_active=True)
    return row


def member(entity, email, role, **names):
    person = make_user(email)
    User.objects.filter(id=person.id).update(first_name=email.split("@")[0].title(), last_name="Member", **names)
    person.refresh_from_db()
    UserEntity.objects.create(user_id=person.id, entity_id=entity.id, role=role, approved=True)
    return person


def users_page(person, entity):
    return call("get", "/api/me/company/users", person, entity.id).json()


# ---- the door ---------------------------------------------------------------------------------


def test_the_preflight_names_delete_too(db):
    resp = Client().options("/api/me/company/users/x", HTTP_ORIGIN=WEB, HTTP_ACCESS_CONTROL_REQUEST_METHOD="DELETE")
    assert resp.status_code == 204
    assert "DELETE" in resp["Access-Control-Allow-Methods"] and resp["Access-Control-Allow-Origin"] == WEB


def test_no_token_no_company_and_someone_elses_company_are_refused(shop):
    assert call("get", "/api/me/company/users", None, shop["entity"].id).status_code == 401
    assert call("get", "/api/me/company/users", shop["admin"]).status_code == 403
    assert call("get", "/api/me/company/users", shop["admin"], "not-a-uuid").status_code == 403
    stranger = make_user("stranger@test.com")
    resp = call("get", "/api/me/company/users", stranger, shop["entity"].id)
    assert (resp.status_code, resp.json()) == (403, {"error": "You don't have access to that company."})


# ---- Users ------------------------------------------------------------------------------------


def test_the_users_tab_reads_the_members_their_rights_and_the_roles_one_may_give(shop):
    resp = call("get", "/api/me/company/users", shop["manager"], shop["entity"].id)
    assert resp.status_code == 200, resp.json()
    page = resp.json()
    assert page["company"] == {"id": str(shop["entity"].id), "name": "Olive & Vine"}
    assert page["modules"] == ["PETTY_CASH"] and page["notices"] == []
    rows = {m["email"]: m for m in page["members"]}
    assert set(rows) == {"admin@test.com", "acc@test.com", "mgr@test.com", "cash@test.com"}
    # a shop manager may change roles at or below their own, and remove nobody (accountant+)
    assert rows["cash@test.com"]["can_change_role"] is True
    assert rows["mgr@test.com"]["can_change_role"] is True and rows["mgr@test.com"]["is_you"] is True
    assert rows["admin@test.com"]["can_change_role"] is False
    assert all(m["can_remove"] is False for m in page["members"])
    assert [r["value"] for r in page["roles"]] == ["shop_manager", "cashier"]
    assert page["can_invite"] is True and page["invitations"] == []
    assert rows["admin@test.com"]["role_label"] == "Admin" and rows["admin@test.com"]["initials"] == "AA"


def test_the_subscriber_is_the_payer_not_a_rank_and_unknown_marks_nobody(shop, facts):
    facts["value"] = {**facts["value"], "payer": str(shop["accountant"].id)}
    assert [m["email"] for m in users_page(shop["admin"], shop["entity"])["members"] if m["subscriber"]] == [
        "acc@test.com"]
    facts["value"] = None  # display only: the page still opens
    page = users_page(shop["admin"], shop["entity"])
    assert not any(m["subscriber"] for m in page["members"])


def test_a_cashier_may_not_open_the_tab(shop):
    resp = call("get", "/api/me/company/users", shop["cashier"], shop["entity"].id)
    assert (resp.status_code, resp.json()) == (403, {"error": "You don't have permission to see this company's users."})


def test_a_role_change_is_the_role_only_and_checked_against_rank(shop):
    entity, cashier = shop["entity"].id, shop["cashier"]
    changed = call("patch", f"/api/me/company/users/{cashier.id}", shop["manager"], entity,
                   {"role": "shop_manager", "first_name": "Renamed"})
    assert changed.status_code == 200, changed.json()
    assert changed.json() == {"message": "Role saved.", "role": "shop_manager", "role_label": "Shop Manager"}
    row = next(m for m in users_page(shop["admin"], shop["entity"])["members"] if m["id"] == str(cashier.id))
    assert row["role"] == "shop_manager" and row["first_name"] == "Cash"  # the name is not this tab's

    assert call("patch", f"/api/me/company/users/{cashier.id}", shop["manager"], entity,
                {"role": "admin"}).status_code == 403
    assert call("patch", f"/api/me/company/users/{shop['admin'].id}", shop["manager"], entity,
                {"role": "cashier"}).status_code == 403


@pytest.mark.parametrize("role", ["entity_base", "super_admin", "owner", "Shop Manager", ""])
def test_only_the_four_assignable_roles_are_taken(shop, role):
    resp = call("patch", f"/api/me/company/users/{shop['cashier'].id}", shop["admin"], shop["entity"].id,
                {"role": role})
    assert (resp.status_code, resp.json()) == (400, {"error": "Pick one of the roles offered."})


def test_the_company_is_the_querys_never_the_bodys(shop):
    resp = call("patch", f"/api/me/company/users/{shop['cashier'].id}", shop["admin"], shop["other"].id,
                {"role": "accountant", "entity_id": str(shop["entity"].id)})
    assert resp.status_code == 404  # the cashier is not in the query's company
    assert UserEntity.objects.get(user_id=shop["cashier"].id).role == "cashier"


def test_removal_runs_the_guards_in_order_and_removes(shop):
    entity = shop["entity"].id
    assert call("delete", f"/api/me/company/users/{shop['admin'].id}", shop["accountant"], entity).status_code == 403
    last = call("delete", f"/api/me/company/users/{shop['admin'].id}", shop["admin"], entity)
    assert last.status_code == 409 and "only admin" in last.json()["error"]
    assert call("delete", f"/api/me/company/users/{shop['cashier'].id}", shop["manager"], entity).status_code == 403

    gone = call("delete", f"/api/me/company/users/{shop['cashier'].id}", shop["accountant"], entity)
    assert gone.status_code == 200, gone.json()
    assert "cash@test.com" not in {m["email"] for m in users_page(shop["admin"], shop["entity"])["members"]}


def test_nobody_on_the_company_is_a_404(shop):
    for target in (uuid.uuid4(), "not-a-uuid"):
        resp = call("delete", f"/api/me/company/users/{target}", shop["admin"], shop["entity"].id)
        assert (resp.status_code, resp.json()) == (404, {"error": "I couldn't find that person on this entity."})


# ---- the guards (Flask's test_membership_removal_guards.py) ----------------------------------


def test_any_row_naming_them_as_payer_refuses_the_removal(shop, facts):
    facts["value"] = {**facts["value"], "payers": [str(shop["accountant"].id)]}
    resp = call("delete", f"/api/me/company/users/{shop['accountant'].id}", shop["admin"], shop["entity"].id)
    assert resp.status_code == 409 and resp.json()["error"].startswith("I can't remove the person who pays")
    assert UserEntity.objects.filter(user_id=shop["accountant"].id).exists()


def test_the_payer_guards_fail_closed_when_the_subscription_api_cannot_answer(shop, facts):
    second = member(shop["entity"], "second@test.com", "admin")
    facts["value"] = None
    for method, body in (("delete", None), ("patch", {"role": "cashier"})):
        resp = call(method, f"/api/me/company/users/{second.id}", shop["admin"], shop["entity"].id, body)
        assert (resp.status_code, resp.json()) == (503, {"error": company.NO_SUBSCRIPTION_ANSWER})
    assert UserEntity.objects.get(user_id=second.id).role == "admin"


def test_a_change_below_admin_never_asks_the_subscription_api(shop, facts):
    facts["value"] = None
    resp = call("patch", f"/api/me/company/users/{shop['cashier'].id}", shop["admin"], shop["entity"].id,
                {"role": "shop_manager"})
    assert resp.status_code == 200


def test_the_payer_keeps_admin(shop, facts):
    second = member(shop["entity"], "second@test.com", "admin")
    facts["value"] = {**facts["value"], "payer": str(second.id), "payers": [str(second.id)]}
    resp = call("patch", f"/api/me/company/users/{second.id}", shop["admin"], shop["entity"].id, {"role": "cashier"})
    assert resp.status_code == 409 and resp.json()["error"].startswith("I can't take admin away")
    # admin to admin is no step down
    assert call("patch", f"/api/me/company/users/{second.id}", shop["admin"], shop["entity"].id,
                {"role": "admin"}).status_code == 200


def test_the_only_admin_is_neither_demoted_nor_removed_and_a_deactivated_admin_does_not_count(shop):
    entity = shop["entity"]
    sleeping = member(entity, "sleeping@test.com", "admin")
    User.objects.filter(id=sleeping.id).update(approved=False)
    demote = call("patch", f"/api/me/company/users/{shop['admin'].id}", shop["admin"], entity.id, {"role": "cashier"})
    assert demote.status_code == 409 and demote.json()["error"].startswith("I can't demote the only admin")
    User.objects.filter(id=sleeping.id).update(approved=True)
    assert call("patch", f"/api/me/company/users/{shop['admin'].id}", shop["admin"], entity.id,
                {"role": "accountant"}).status_code == 200


def test_a_handover_offered_to_them_refuses_and_unknown_lets_it_through(shop, facts, monkeypatch):
    target = shop["accountant"]
    facts["value"] = {**facts["value"], "pending_transfer_to": str(target.id)}
    resp = call("delete", f"/api/me/company/users/{target.id}", shop["admin"], shop["entity"].id)
    assert resp.status_code == 409 and "handed over to them" in resp.json()["error"]

    calls = {"n": 0}

    def flaky(entity_id):  # answers the payer guard, not the handover check after it
        calls["n"] += 1
        if calls["n"] > 1:
            raise subscription_client.SubscriptionUnavailable("down")
        return {"payer": None, "payers": [], "pending_transfer_to": None}

    monkeypatch.setattr(subscription_client, "entity_facts", flaky)
    assert call("delete", f"/api/me/company/users/{target.id}", shop["admin"], shop["entity"].id).status_code == 200


# ---- invitations ------------------------------------------------------------------------------


def invite(person, entity, **body):
    payload = {"email": "new@test.com", "role": "cashier", "first_name": "New", "last_name": "Person", **body}
    return call("post", "/api/me/company/invitations", person, entity.id, payload)


def test_an_invitation_is_sent_and_listed(shop, settings):
    sent = invite(shop["manager"], shop["entity"])
    assert sent.status_code == 201, sent.json()
    assert sent.json()["email_sent"] is True and sent.json()["message"] == "Invitation sent."
    message, = mail.outbox
    inv = Invitation.objects.get(id=sent.json()["invitation_id"])
    assert (message.to, message.from_email, message.content_subtype) == (["new@test.com"], settings.INVITE_FROM, "html")
    assert message.subject == "You've been invited to Olive & Vine on Minty"
    assert f"{settings.ACCOUNTS_API_URL}/invitation/accept/{inv.token}?fn=New&amp;ln=Person" in message.body
    assert f"{settings.ACCOUNTS_API_URL}/static/img/new_logo.png?v=" in message.body
    assert (inv.first_name, inv.last_name, str(inv.role), inv.invited_by) == ("New", "Person", "cashier",
                                                                             shop["manager"].id)
    assert inv.expires_at > datetime.now(UTC) + timedelta(days=6)
    assert [(i["email"], i["role"], i["can_manage"]) for i in users_page(shop["manager"], shop["entity"])[
        "invitations"]] == [("new@test.com", "cashier", True)]


@pytest.mark.parametrize("email, sentence", [
    ("<svg/onload=alert(1)>@x.co", "That doesn't look like an email address."),
    ("a'b@x.co", "That doesn't look like an email address."),
    ("no-at-sign.example", "That doesn't look like an email address."),
    ("two@@x.co", "That doesn't look like an email address."),
    ("홍길동@example.com", "Email can only contain English letters, numbers and symbols."),
    ("x" * 140 + "@example.com", "That doesn't look like an email address."),
])
def test_an_invitation_goes_only_to_an_address(shop, email, sentence):
    resp = invite(shop["admin"], shop["entity"], email=email)
    assert (resp.status_code, resp.json()) == (400, {"error": sentence})
    assert mail.outbox == []


@pytest.mark.parametrize("names", [{"first_name": ""}, {"last_name": "  "}, {"first_name": "", "last_name": ""}])
def test_an_invitation_needs_both_names(shop, names):
    resp = invite(shop["admin"], shop["entity"], **names)
    assert (resp.status_code, resp.json()) == (400, {"error": "I need their first and last name."})


def test_an_invitations_role_is_assignable_and_not_above_the_inviter(shop):
    assert invite(shop["admin"], shop["entity"], role="entity_base").status_code == 400
    assert invite(shop["manager"], shop["entity"], role="admin").status_code == 403
    assert invite(shop["cashier"], shop["entity"]).status_code == 403  # no USER_INVITE
    assert not Invitation.objects.exists()


def test_a_member_or_a_live_pending_invitation_is_refused_and_an_expired_one_replaced(shop):
    assert invite(shop["admin"], shop["entity"], email="acc@test.com").json() == {
        "error": "This user is already a member of this entity."}
    first = invite(shop["admin"], shop["entity"])
    again = invite(shop["admin"], shop["entity"])
    assert (again.status_code, again.json()) == (409, {"error": "An invitation is already pending for this email."})
    Invitation.objects.filter(id=first.json()["invitation_id"]).update(expires_at=datetime.now(UTC) - timedelta(days=1))
    assert invite(shop["admin"], shop["entity"]).status_code == 201
    assert str(Invitation.objects.get(id=first.json()["invitation_id"]).status) == "expired"


def test_a_failed_send_keeps_the_invitation_and_says_so(shop, monkeypatch):
    def broken(self, *args, **kwargs):
        raise OSError("smtp down")

    monkeypatch.setattr("django.core.mail.EmailMessage.send", broken)
    resp = invite(shop["admin"], shop["entity"])
    assert resp.status_code == 201 and resp.json()["email_sent"] is False
    assert "Resend" in resp.json()["message"] and Invitation.objects.filter(status="pending").count() == 1


def test_an_invitation_is_managed_only_through_its_own_company(shop):
    invite(shop["admin"], shop["entity"])
    inv_id = users_page(shop["admin"], shop["entity"])["invitations"][0]["id"]
    elsewhere = call("post", f"/api/me/company/invitations/{inv_id}/cancel", shop["admin"], shop["other"].id)
    assert (elsewhere.status_code, elsewhere.json()) == (404, {"error": "Hmm, I couldn't find that invitation."})

    soon = call("post", f"/api/me/company/invitations/{inv_id}/resend", shop["admin"], shop["entity"].id)
    assert soon.status_code == 429 and soon.json()["retry_after"] > 0

    cancelled = call("post", f"/api/me/company/invitations/{inv_id}/cancel", shop["admin"], shop["entity"].id)
    assert (cancelled.status_code, cancelled.json()) == (200, {"message": "Invitation cancelled."})
    assert users_page(shop["admin"], shop["entity"])["invitations"] == []
    assert str(Invitation.objects.get(id=inv_id).status) == "revoked"
    again = call("post", f"/api/me/company/invitations/{inv_id}/cancel", shop["admin"], shop["entity"].id)
    assert again.status_code == 400


def test_an_invitation_for_a_role_above_yours_is_not_yours_to_manage(shop):
    invite(shop["admin"], shop["entity"], role="accountant")
    inv_id = Invitation.objects.get().id
    resp = call("post", f"/api/me/company/invitations/{inv_id}/cancel", shop["manager"], shop["entity"].id)
    assert resp.status_code == 403
    assert users_page(shop["manager"], shop["entity"])["invitations"][0]["can_manage"] is False


def test_a_resend_after_the_cooldown_kills_the_old_link(shop):
    invite(shop["admin"], shop["entity"])
    inv = Invitation.objects.get()
    invitations.reset_cooldowns()
    resp = call("post", f"/api/me/company/invitations/{inv.id}/resend", shop["admin"], shop["entity"].id)
    assert resp.status_code == 200 and resp.json()["message"] == "Invitation sent again."
    assert resp.json()["resend_cooldown"] > 0
    fresh = Invitation.objects.get()
    assert fresh.token != inv.token and fresh.token in mail.outbox[-1].body and len(mail.outbox) == 2


def test_a_resend_whose_email_fails_is_a_502(shop, monkeypatch):
    invite(shop["admin"], shop["entity"])
    invitations.reset_cooldowns()
    monkeypatch.setattr("django.core.mail.EmailMessage.send", lambda self, *a, **k: (_ for _ in ()).throw(OSError()))
    resp = call("post", f"/api/me/company/invitations/{Invitation.objects.get().id}/resend", shop["admin"],
                shop["entity"].id)
    assert resp.status_code == 502 and "retry_after" in resp.json()


def test_the_view_all_account_is_promoted_when_invited(shop):
    viewall = make_user("viewall@dailyminty.com", approved=False)
    assert invite(shop["admin"], shop["entity"], email="viewall@dailyminty.com").status_code == 201
    viewall.refresh_from_db()
    assert (str(viewall.system_role), viewall.approved) == ("superadmin", True)


# ---- Entity & Integration ---------------------------------------------------------------------


def integration(person, entity, body=None):
    return call("patch" if body is not None else "get", "/api/me/company/integration", person, entity.id, body)


def test_the_integration_tab_reads_the_company_its_lists_and_its_xero_state(shop):
    resp = integration(shop["cashier"], shop["entity"])
    assert resp.status_code == 200, resp.json()
    page = resp.json()
    assert page["company"] == {"id": str(shop["entity"].id), "name": "Olive & Vine", "country_code": "HK",
                               "currency_id": str(shop["currency"].id)}
    assert page["countries"] == [{"code": "HK", "name": "Hong Kong"}]
    assert page["currencies"] == [{"id": str(shop["currency"].id), "name": "HKD"}]
    assert page["xero"] == {"status": "disconnected", "connected": False, "needs_reconnect": False,
                            "organisation": None, "last_connected_at": None}
    assert (page["can_edit"], page["can_rename"], page["xero_conflict"], page["notices"]) == (False, False, None, [])


def test_a_connected_company_is_checked_against_xero_and_its_organisation_named(shop, monkeypatch):
    org = shop["entity"]
    tenant = uuid.uuid4()
    Entity.objects.filter(id=org.id).update(xero_org_id=tenant, status="disconnected")
    monkeypatch.setattr(company, "resolve_entity_access_token", lambda entity_id: ("access", None))
    asked = []

    class Answer:
        def __init__(self, payload):
            self.status_code, self._payload = 200, payload

        def json(self):
            return self._payload

    def get(url, headers, timeout):
        asked.append((url, headers.get("Xero-Tenant-Id")))
        if url == company.CONNECTIONS_URL:
            return Answer([{"tenantId": str(tenant)}])
        return Answer({"Organisations": [{"Name": "Demo Company (HK)"}]})

    monkeypatch.setattr(company.requests, "get", get)
    xero = integration(shop["admin"], org).json()["xero"]
    assert (xero["status"], xero["connected"], xero["needs_reconnect"], xero["organisation"]) == (
        "connected", True, False, "Demo Company (HK)")
    assert xero["last_connected_at"] is not None
    assert asked[0] == (company.CONNECTIONS_URL, None) and asked[1][1] == str(tenant)
    assert Entity.objects.get(id=org.id).xero_tenant_name == "Demo Company (HK)"


def test_an_organisation_with_no_token_needs_a_reconnect(shop, monkeypatch):
    Entity.objects.filter(id=shop["entity"].id).update(xero_org_id=uuid.uuid4(), status="connected")
    monkeypatch.setattr(company, "resolve_entity_access_token", lambda entity_id: None)
    xero = integration(shop["admin"], shop["entity"]).json()["xero"]
    assert (xero["connected"], xero["needs_reconnect"]) == (False, True)


def test_saving_the_companys_settings(shop):
    entity = shop["entity"]
    assert integration(shop["accountant"], entity, {"name": "New Name"}).status_code == 403
    assert integration(shop["cashier"], entity, {"country_code": "HK"}).status_code == 403
    for body, sentence in (
        ({"name": "   "}, "I need a name for this entity before I can save it."),
        ({"name": "x" * 101}, "That name goes on a bit! Please keep it to 100 characters or fewer."),
        ({"name": "Other Co"}, "You already have a company called “Other Co”. Choose another name."),
        ({"country_code": "ZZ"}, "Pick one of the countries offered."),
        ({"currency_id": "not-a-uuid"}, "Pick one of the currencies offered."),
        ({"currency_id": str(uuid.uuid4())}, "Pick one of the currencies offered."),
    ):
        resp = integration(shop["admin"], entity, body)
        assert (resp.status_code, resp.json()) == (422, {"error": sentence}), body
    saved = integration(shop["admin"], entity, {"name": "Olive and Vine Ltd", "country_code": "hk"})
    assert saved.status_code == 200, saved.json()
    assert saved.json()["company"]["name"] == "Olive and Vine Ltd" and saved.json()["message"] == "Settings saved!"


def test_a_name_is_unique_per_person_not_across_customers(shop):
    stranger = make_user("someone@else.test")
    elsewhere = Entity.objects.create(id=uuid.uuid4(), name="Elsewhere Ltd", status="disconnected")
    UserEntity.objects.create(user_id=stranger.id, entity_id=elsewhere.id, role="admin", approved=True)
    theirs = integration(shop["admin"], shop["entity"], {"name": "Elsewhere Ltd"})
    assert theirs.status_code == 200 and theirs.json()["company"]["name"] == "Elsewhere Ltd"


def company_money(entity):
    org = Entity.objects.get(id=entity.id)
    cash = CashInfo.objects.filter(id__in=EntityCashSetting.objects.filter(entity_id=entity.id).values("cash_id"))
    return org.country_code, str(org.currency_id), org.currency_format, {str(c.currency_id) for c in cash}, cash.count()


@pytest.fixture
def singapore(shop):
    sgd = currency("SGD", notes=(10, 1))
    CountryInfo.objects.create(country_code="SG", alpha3_code="SGP", country_name_en="Singapore",
                               currency_id=sgd.id, is_active=True, display_order=2)
    return sgd


def test_a_country_alone_brings_its_currency_and_its_notes_and_coins(shop, singapore):
    saved = integration(shop["accountant"], shop["entity"], {"country_code": "SG"})
    assert saved.status_code == 200, saved.json()
    assert saved.json()["company"]["currency_id"] == str(singapore.id)
    assert company_money(shop["entity"]) == ("SG", str(singapore.id), "$", {str(singapore.id)}, 2)


def test_a_currency_picked_by_hand_beats_the_countrys_own(shop, singapore):
    hkd = str(shop["currency"].id)
    saved = integration(shop["accountant"], shop["entity"], {"country_code": "SG", "currency_id": hkd})
    assert saved.status_code == 200, saved.json()
    assert company_money(shop["entity"]) == ("SG", hkd, "$", {hkd}, 5)


def test_a_failed_save_is_a_500_and_changes_nothing(shop, singapore, monkeypatch):
    def broken(entity_id, currency_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(company, "sync_cash_settings", broken)
    resp = integration(shop["admin"], shop["entity"], {"country_code": "SG"})
    assert (resp.status_code, resp.json()) == (500, {"error": company.SAVE_FAILED})
    assert Entity.objects.get(id=shop["entity"].id).country_code == "HK"


def test_the_emails_logo_is_served_here_byte_for_byte(db):
    resp = Client().get("/static/img/new_logo.png")
    assert (resp.status_code, resp["Content-Type"]) == (200, "image/png")
    assert b"".join(resp.streaming_content) == invitations.LOGO.read_bytes()
    assert Client().post("/static/img/new_logo.png").status_code == 405
