"""Into the apps and out (``hub/handoffs.py``) - Flask's ``test_petty_cash_web_handoff.py``,
``test_minty_web_handoff.py``, ``test_module_selector.py``, the ``/enter`` cases of
``test_url_security.py`` and the logout tests, restated; plus POST-only sign-out (gap 3)."""

from __future__ import annotations

import json
import uuid
from urllib.parse import parse_qs, quote, urlsplit

import jwt
import pytest
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client
from minty_db.models import Entity, EntityFunction, EntityFunctionMap, User, UserEntity

from hub import handoffs
from hub.tests.test_public_and_transport import make_user

HUB, PAY, PETTY, ONB = (settings.MINTY_WEB_URL, settings.PAYMENT_REQUEST_WEB_URL, settings.PETTY_CASH_WEB_URL,
                        settings.ONBOARDING_WEB_URL)


def login(client: Client, user: User, sid: str = "sid-1", agreed: bool = True) -> Client:
    """A signed-in browser (a session row and its cookie); ``agreed`` records the live Terms."""
    if agreed:
        from minty_db.models import TermsConsent

        from hub import registry

        TermsConsent.objects.get_or_create(user_id=user.id, terms_version=registry.current_version(registry.TERMS),
                                           defaults={"document_hash": "h", "source": "hub"})
    store = SessionStore()
    store.update({"user_id": str(user.id), "login_sid": sid})
    store.create()
    client.cookies["session"] = store.session_key
    return client


@pytest.fixture
def shop(db):
    codes = {code: EntityFunction.objects.create(id=uuid.uuid4(), function_code=code, function_name=code)
             for code in ("PETTY_CASH", "PAYMENT_REQUEST")}
    owner, stranger = make_user("owner@test.com"), make_user("stranger@test.com")
    entity = Entity.objects.create(id=uuid.uuid4(), name="Vine Café Central", status="connected", xero_org_id="t-1")
    UserEntity.objects.create(user_id=owner.id, entity_id=entity.id, role="admin", approved=True)

    def modules(*on):
        for code, fn in codes.items():
            EntityFunctionMap.objects.update_or_create(entity_id=entity.id, entity_function_id=fn.id,
                                                       defaults={"is_enabled": code in on})

    modules("PETTY_CASH")
    return {"owner": owner, "stranger": stranger, "entity": entity, "modules": modules}


def ref(entity):
    return f"{str(entity.id)[:8]}/vine-café-central"


def iri(path):
    """A Location as Django (and Werkzeug) send it: non-ASCII percent-encoded."""
    return quote(path, safe="/?=&%:")


def landing(resp, origin):
    """The app's ``/landing`` query - with a one-time code, never a token (gap 4)."""
    assert resp.status_code == 302, resp.content
    parts = urlsplit(resp["Location"])
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == origin + "/landing", resp["Location"]
    query = parse_qs(parts.query)
    assert "token" not in query and query["code"], resp["Location"]
    return {**query, "_origin": [origin]}


def swap(code, origin):
    """What the app's landing does: the code, swapped once, from the app's origin."""
    resp = redeem(code, origin)
    assert resp.status_code == 200, resp.content
    return resp.json()["token"]


def claims(query):
    return jwt.decode(swap(query["code"][0], query["_origin"][0]), settings.SECRET_KEY, algorithms=["HS256"])


# ---- tokens -------------------------------------------------------------------------------------


def test_the_company_slug_keeps_unicode_letters():
    from hub.addresses import slugify_name

    assert slugify_name("Vine Café Central") == "vine-café-central"
    assert slugify_name("Olive & Vine") == "olive-and-vine" and slugify_name("!!") == "company"


def test_a_member_lands_on_the_companys_petty_cash_with_a_module_token(shop):
    entity = shop["entity"]
    query = landing(login(Client(), shop["owner"]).get("/handoff/petty-cash-web", {"entity_id": str(entity.id)}), PETTY)
    assert query["next"] == [f"/entity/{str(entity.id)[:8]}/vine-café-central/petty-cash"]
    assert (query["entity_id"], query["entity_name"]) == ([str(entity.id)], ["Vine Café Central"])
    token = claims(query)
    assert (token["module"], token["user_id"], token["entity_id"], token["role"], token["xero_org_id"]) == (
        "billing", str(shop["owner"].id), str(entity.id), "admin", "t-1")
    assert (token["petty_cash_enabled"], token["billing_enabled"], token["is_view_only"], token["sid"]) == (
        True, False, False, "sid-1")
    assert token["exp"] - token["iat"] == 30 * 60 and token["system_role"] == "normal"


def test_the_apps_own_address_form_names_the_company_too(shop):
    short = f"{str(shop['entity'].id)[:8]}/vine-café-central"
    query = landing(login(Client(), shop["owner"]).get("/handoff/petty-cash-web", {"entity_id": short}), PETTY)
    assert query["entity_id"] == [str(shop["entity"].id)]


@pytest.mark.parametrize("bad", ["//evil.example/phish", "https://evil.example", "javascript:alert(1)"])
def test_next_is_never_an_open_redirect(shop, bad):
    query = landing(login(Client(), shop["owner"]).get(
        "/handoff/petty-cash-web", {"entity_id": str(shop["entity"].id), "next": bad}), PETTY)
    assert query["next"] == [f"/entity/{str(shop['entity'].id)[:8]}/vine-café-central/petty-cash"]


def test_every_handoff_needs_a_sign_in(shop):
    for path in ("/handoff/petty-cash-web", "/handoff/minty-web", "/profile", "/entity", "/entity/create"):
        resp = Client().get(path, {"entity_id": str(shop["entity"].id)})
        assert resp.status_code == 302 and resp["Location"].startswith("/?next="), path


def test_no_company_goes_to_the_hubs_company_list(shop):
    query = landing(login(Client(), shop["owner"]).get("/handoff/petty-cash-web"), HUB)
    assert query["next"] == ["/entities"] and claims(query)["entity_id"] == ""


@pytest.mark.parametrize("which", ["stranger", "no such company"])
def test_a_company_you_cannot_see_gets_no_token(shop, which):
    person = shop["stranger"] if which == "stranger" else shop["owner"]
    target = str(shop["entity"].id) if which == "stranger" else str(uuid.uuid4())
    resp = login(Client(), person).get("/handoff/petty-cash-web", {"entity_id": target})
    assert resp["Location"] == "/entity" and "token=" not in resp["Location"]


def test_petty_cash_switched_off_gets_no_token_and_onboarding_goes_to_the_wizard(shop):
    shop["modules"]()
    client = login(Client(), shop["owner"])
    assert client.get("/handoff/petty-cash-web", {"entity_id": str(shop["entity"].id)})["Location"] == "/entity"
    Entity.objects.filter(id=shop["entity"].id).update(status="onboarding")
    wizard = urlsplit(client.get("/handoff/petty-cash-web", {"entity_id": str(shop["entity"].id)})["Location"])
    assert f"{wizard.scheme}://{wizard.netloc}" == ONB
    query = parse_qs(wizard.query)
    assert "token" not in query
    assert jwt.decode(swap(query["code"][0], ONB), settings.SECRET_KEY, algorithms=["HS256"])["scope"] == "onboarding"
    assert query["entity_id"] == [str(shop["entity"].id)]


def test_the_minty_web_handoff_scoped_unscoped_and_never_an_open_redirect(shop):
    client = login(Client(), shop["owner"])
    scoped = landing(client.get("/handoff/minty-web", {"next": "/x", "entity_id": str(shop["entity"].id)}), HUB)
    assert scoped["next"] == ["/x"] and claims(scoped)["entity_id"] == str(shop["entity"].id)
    unscoped = landing(client.get("/handoff/minty-web", {"next": "//evil.example"}), HUB)
    assert unscoped["next"] == ["/subscription"] and claims(unscoped)["entity_id"] == ""
    assert login(Client(), shop["stranger"]).get(
        "/handoff/minty-web", {"entity_id": str(shop["entity"].id)})["Location"] == "/entity"


def test_profile_lands_on_the_hubs_company_list(shop):
    query = landing(login(Client(), shop["owner"]).get("/profile", {"entity_id": str(shop["entity"].id)}), HUB)
    assert query["next"] == ["/entities"] and claims(query)["entity_id"] == str(shop["entity"].id)


def test_the_company_list_carries_what_was_flashed(shop):
    client = login(Client(), shop["owner"])
    client.get("/handoff/petty-cash-web", {"entity_id": str(uuid.uuid4())})  # flashes "no permission"
    query = landing(client.get("/entity"), HUB)
    flashed = parse_qs(urlsplit(query["next"][0]).query)["flash"][0]
    from hub.notices import read_notices

    assert read_notices(flashed) == [{"category": "error", "message": handoffs.NO_PERMISSION}]


def test_a_new_company_starts_a_fresh_wizard(shop):
    wizard = parse_qs(urlsplit(login(Client(), shop["owner"]).get("/entity/create")["Location"]).query)
    assert wizard["fresh"] == ["1"] and wizard["first"] == ["Olive"] and "entity_id" not in wizard


# ---- a company's way in -------------------------------------------------------------------------


def test_both_modules_open_minty_webs_module_choice(shop):
    shop["modules"]("PETTY_CASH", "PAYMENT_REQUEST")
    query = landing(login(Client(), shop["owner"]).get(f"/entity/{ref(shop['entity'])}/modules"), HUB)
    assert query["next"] == [f"/entity/{str(shop['entity'].id)[:8]}/vine-café-central"]
    assert Entity.objects.get(id=shop["entity"].id).last_accessed_by_user_id == shop["owner"].id


def test_one_module_goes_straight_in(shop):
    client = login(Client(), shop["owner"])
    path = f"/entity/{str(shop['entity'].id)[:8]}/vine-café-central"
    assert client.get(f"{path}/modules")["Location"] == iri(f"{path}/petty-cash")
    shop["modules"]("PAYMENT_REQUEST")
    query = landing(client.get(f"{path}/modules"), PAY)
    assert query["next"] == [f"{path}/payment-request"]


def test_a_full_uuid_address_is_308d_to_the_canonical_one(shop):
    resp = login(Client(), shop["owner"]).get(f"/entity/{shop['entity'].id}/modules?x=1")
    assert resp.status_code == 308
    assert resp["Location"] == f"/entity/{str(shop['entity'].id)[:8]}/vine-caf%C3%A9-central/modules?x=1"


def test_someone_outside_the_company_is_sent_back_to_the_list(shop):
    resp = login(Client(), shop["stranger"]).get(f"/entity/{shop['entity'].id}/modules", follow=False)
    assert resp.status_code == 308  # canonical first
    assert login(Client(), shop["stranger"]).get(resp["Location"])["Location"] == "/entity"


def test_the_payment_request_hand_off_and_its_request_segment(shop):
    shop["modules"]("PAYMENT_REQUEST")
    client = login(Client(), shop["owner"])
    path = f"/entity/{str(shop['entity'].id)[:8]}/vine-café-central"
    query = landing(client.get(f"{path}/payment-request", {"request": "PR-0001/A"}), PAY)
    assert query["next"] == [f"{path}/payment-request/PR-0001%2FA"]
    plain = landing(client.get(f"{path}/payment-request", {"request": "../x"}), PAY)
    assert plain["next"] == [f"{path}/payment-request"]


def test_the_dashboard_hands_over_to_the_apps_petty_cash(shop):
    path = f"/entity/{str(shop['entity'].id)[:8]}/vine-café-central"
    resp = login(Client(), shop["owner"]).get(f"{path}/petty-cash")
    target = urlsplit(resp["Location"])
    assert target.path == "/handoff/petty-cash-web"
    assert parse_qs(target.query) == {"entity_id": [str(shop["entity"].id)], "next": [f"{path}/petty-cash"]}


# ---- back in: /enter (on the session alone, gap 4) ----------------------------------------------


def enter(shop, nxt=None, client=None, **extra):
    query = {**({"next": nxt} if nxt else {}), **extra}
    return (client or Client()).get(f"/entity/{str(shop['entity'].id)[:8]}/vine-café-central/enter", query)


def test_back_with_the_session_goes_straight_on(shop):
    assert enter(shop, "/entity", login(Client(), shop["owner"]))["Location"] == "/entity"


@pytest.mark.parametrize("evil", ["/%09/evil.com", "/%5Cevil.com", "//evil.com", "https://evil.com"])
def test_enter_never_redirects_off_site(shop, evil):
    client = login(Client(), shop["owner"])
    resp = client.get(f"/entity/{ref(shop['entity'])}/enter?next={evil}")  # raw, as a browser sends it
    assert resp["Location"].endswith(iri("/vine-café-central/petty-cash")) and "evil" not in resp["Location"]


def test_a_token_in_the_address_signs_nobody_in(shop):
    """Gap 4: ``/enter?token=`` once made a session from any module token for 30 minutes."""
    token = handoffs.module_token(None, shop["owner"].id, shop["entity"].id, "")
    client = Client()
    resp = enter(shop, "/entity", client, token=token)
    assert resp["Location"].startswith("/?next=")  # the front door, not the company
    assert client.get("/")["Location"].startswith(HUB + "/login")  # nobody signed in


def test_back_with_no_session_signs_in_then_carries_on_there(shop):
    path = f"/entity/{ref(shop['entity'])}/enter"
    resp = Client().get(path, {"next": "/entity"})
    target = urlsplit(resp["Location"])
    assert resp.status_code == 302 and target.path == "/"
    assert parse_qs(target.query)["next"] == [iri(path) + "?next=%2Fentity"]


# ---- one-time codes: POST /api/handoff/redeem (gap 4) -------------------------------------------


def redeem(code, origin=PAY, client=None):
    headers = {"HTTP_ORIGIN": origin} if origin else {}
    return (client or Client()).post("/api/handoff/redeem", data=json.dumps({"code": code}),
                                     content_type="application/json", **headers)


def test_a_code_gives_its_token_once_to_the_app_it_was_handed_to(db):
    code = handoffs.park_token("the.module.jwt", PAY + "/")
    first = redeem(code)
    assert first.status_code == 200 and first.json() == {"token": "the.module.jwt"}
    assert first["Access-Control-Allow-Origin"] == PAY and first["Cache-Control"] == "no-store"
    again = redeem(code)
    assert again.status_code == 400 and again.json() == {"error": handoffs.CODE_REFUSED}


def test_a_code_from_another_origin_is_refused_and_spent(db):
    code = handoffs.park_token("the.module.jwt", PAY)
    for origin in (HUB, "https://evil.example", ""):
        assert redeem(handoffs.park_token("t", PAY), origin).status_code == 403
    assert redeem(code, HUB).json() == {"error": handoffs.CODE_REFUSED}
    assert redeem(code).status_code == 400  # the wrong hands spent it


def test_the_onboarding_app_redeems_its_own_code(db):
    resp = redeem(handoffs.park_token("onboarding.jwt", ONB), ONB)
    assert resp.status_code == 200 and resp["Access-Control-Allow-Origin"] == ONB


def test_an_expired_forged_or_malformed_code_is_refused(db, monkeypatch):
    stale = handoffs.park_token("t", PAY)
    monkeypatch.setattr(handoffs, "CODE_MAX_AGE", -1)
    assert redeem(stale).status_code == 400
    monkeypatch.undo()
    for code in ("", "forged", "x" * 600, 42, None):
        resp = redeem(code)
        assert resp.status_code == 400 and resp.json() == {"error": handoffs.CODE_REFUSED}
    bad = Client().post("/api/handoff/redeem", data="not json", content_type="application/json", HTTP_ORIGIN=PAY)
    assert bad.status_code == 400


def test_redeem_is_a_post_with_a_preflight(db):
    pre = Client().options("/api/handoff/redeem", HTTP_ORIGIN=PETTY)
    assert pre.status_code == 204 and pre["Access-Control-Allow-Origin"] == PETTY
    assert pre["Access-Control-Allow-Methods"] == "POST, OPTIONS" and "Content-Type" in pre["Access-Control-Allow-Headers"]
    assert Client().get("/api/handoff/redeem").status_code == 405


def test_neither_code_nor_token_is_ever_logged(db, caplog):
    code = handoffs.park_token("the.module.jwt", PAY)
    redeem(code, HUB)
    redeem(code)
    logged = " | ".join(r.getMessage() for r in caplog.records)
    assert code not in logged and "the.module.jwt" not in logged


# ---- out: POST only (gap 3) ---------------------------------------------------------------------


def test_signing_out_is_a_post_from_a_front_end(shop):
    client = login(Client(), shop["owner"])
    assert client.get("/logout").status_code == 405
    assert client.get("/")["Location"] == "/entity"  # still signed in
    assert client.post("/logout", HTTP_ORIGIN="https://evil.example").status_code == 403
    assert client.post("/logout").status_code == 403  # no Origin
    out = client.post("/logout", HTTP_ORIGIN=HUB)
    assert out["Location"] == "/"
    assert client.get("/")["Location"].startswith(HUB + "/login")
    owner = User.objects.get(id=shop["owner"].id)
    assert owner.signed_in_at is None and owner.last_seen_at is not None


def test_leaving_a_company_keeps_the_session_and_drops_presence(shop):
    client = login(Client(), shop["owner"])
    assert client.get("/leave-entity").status_code == 405
    assert client.post("/leave-entity", HTTP_ORIGIN=PETTY)["Location"] == "/entity"
    assert client.get("/")["Location"] == "/entity"
    assert User.objects.get(id=shop["owner"].id).signed_in_at is None


# ---- the Terms gate -----------------------------------------------------------------------------


def test_someone_who_owes_the_terms_is_sent_to_the_company_list_and_json_gets_a_403(shop):
    client = login(Client(), shop["owner"], agreed=False)
    path = f"/entity/{ref(shop['entity'])}/modules"
    assert client.get(path)["Location"] == "/entity"
    refused = client.get("/handoff/minty-web", HTTP_ACCEPT="application/json")
    assert (refused.status_code, refused.json()["code"]) == (403, "terms_acceptance_required")
    landing(client.get("/entity"), HUB)  # the list is open: minty-web draws the panel there
    assert client.post("/logout", HTTP_ORIGIN=HUB)["Location"] == "/"  # and the way out


def test_the_agreement_is_remembered_in_the_session_for_this_version(shop):
    from minty_db.models import TermsConsent

    client = login(Client(), shop["owner"])
    path = f"/entity/{ref(shop['entity'])}/modules"
    client.get(path)

    TermsConsent.objects.filter(user_id=shop["owner"].id).delete()
    assert client.get(path)["Location"] != "/entity"  # cached for this version


def test_a_broken_gate_lets_the_request_through(shop, monkeypatch):
    from hub import terms_gate

    monkeypatch.setattr(terms_gate, "terms_owed", lambda user_id: 1 / 0)
    client = login(Client(), shop["owner"], agreed=False)
    assert client.get(f"/entity/{ref(shop['entity'])}/modules")["Location"] != "/entity"
