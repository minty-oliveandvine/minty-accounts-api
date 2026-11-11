"""Signing in with Xero and connecting or reconnecting a company (``hub/xero_oauth.py``,
``xero/services/connect.py``) - Flask's ``test_xero_scopes.py`` (the route contract),
``test_url_security.py`` (``next``), ``test_xero_login_flash_drain.py``, ``test_xero_company_writes.py``,
``test_xero_entity_connect_race.py``, ``test_xero_conflict_guard.py`` and
``test_xero_connect_mirror.py``, restated against the database, plus the state's nonce (gap 2).

Xero is faked at ``requests`` (the token endpoint, /connections, Organisation, Accounts, Contacts)
and the token service (another company's connector token). The post-connect sync's thread is
recorded, not started; ``background_sync`` is run inline where its effect is the point.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
import requests
from django.conf import settings
from django.db import connection
from django.test import Client
from minty_db.models import (
    AccountInfo,
    Entity,
    EntityAccountXero,
    EntityBillAccountXero,
    Invitation,
    TermsConsent,
    User,
    UserEntity,
    UserToken,
    XeroContactSync,
)

from hub import registry, xero_oauth
from hub.notices import read_conflict
from hub.tests.test_handoffs import login
from hub.tests.test_public_and_transport import make_user
from xero.services import connect, sync

JWT_KEY = "a-test-only-key-that-is-long-enough-for-hs256"
EXPECTED_SCOPES = {"openid", "profile", "email", "offline_access", "accounting.settings", "accounting.contacts",
                   "accounting.invoices", "accounting.banktransactions", "files"}
ONB = settings.ONBOARDING_WEB_URL


def _acc(aid, code, name, typ="EXPENSE"):
    return {"AccountID": aid, "Code": code, "Name": name, "Type": typ, "Class": "EXPENSE", "Status": "ACTIVE"}


NEW_ACCOUNTS = [_acc("new-400", "400", "Advertising"), _acc("new-420", "420", "Entertainment")]


class Answer:
    def __init__(self, status_code=200, body=None):
        self.status_code, self._body = status_code, body
        self.text = "" if body is None else str(body)

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


class Xero:
    """Xero as the callback meets it. ``granted`` is what this auth event granted; ``visible`` what
    the new token sees on /connections (default: ``granted``); ``tokens`` the token service's
    answer per company (another connector's token, or None = dead); ``sees`` what such a token
    sees."""

    def __init__(self, monkeypatch, email="owner@test.com"):
        self.email, self.claims, self.exchange = email, {}, 200
        self.granted = [{"id": "conn-1", "tenantId": "t-1", "tenantName": "Acme Ltd"}]
        self.visible = None
        self.year_end = {"FinancialYearEndDay": 31, "FinancialYearEndMonth": 3}
        self.accounts = list(NEW_ACCOUNTS)
        self.contacts = [{"ContactID": "new-c1", "Name": "New Supplier"}]
        self.tokens, self.sees, self.probe_status = {}, {}, {}
        self.posts, self.deleted, self.syncs, self.probes = [], [], [], []
        self.grant_token = None
        monkeypatch.setattr(requests, "post", self.post)
        monkeypatch.setattr(requests, "get", self.get)
        monkeypatch.setattr(requests, "delete", self.delete)
        monkeypatch.setattr(connect, "resolve_entity_access_token", self.token_for)
        monkeypatch.setattr(xero_oauth, "resolve_entity_access_token", self.token_for)
        monkeypatch.setattr(xero_oauth, "start_background_sync", lambda *args: self.syncs.append(args))

    def token_for(self, entity_id):
        """``"GRANT"`` stands for the token this callback was just given."""
        token = self.tokens.get(str(entity_id))
        return (self.grant_token, "t-1") if token == "GRANT" else token

    def post(self, url, data=None, auth=None, headers=None, timeout=None):
        assert url == "https://identity.xero.com/connect/token"
        assert auth == (settings.XERO_CLIENT_ID, settings.XERO_CLIENT_SECRET) and timeout
        self.posts.append(dict(data))
        if self.exchange != 200:
            return Answer(self.exchange, {"error": "invalid_grant"})
        self.grant_token = jwt.encode({"authentication_event_id": f"event-{len(self.posts)}"}, JWT_KEY,
                                      algorithm="HS256")
        id_claims = {**({"email": self.email} if self.email else {}), **self.claims}
        return Answer(200, {"access_token": self.grant_token, "refresh_token": f"refresh-{len(self.posts)}",
                            "id_token": jwt.encode(id_claims, JWT_KEY, algorithm="HS256"), "expires_in": 1800})

    def get(self, url, headers=None, params=None, timeout=None):
        assert timeout, url
        bearer = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        if url.startswith(connect.CONNECTIONS_URL + "?authEventId="):
            assert bearer == self.grant_token
            return Answer(200, self.granted)
        if url == connect.CONNECTIONS_URL:
            if bearer == self.grant_token:
                return Answer(200, self.granted if self.visible is None else self.visible)
            self.probes.append(bearer)
            status = self.probe_status.get(bearer, 200)
            if status == "raise":
                raise requests.ConnectionError("xero unreachable")
            return Answer(status, [{"id": f"c-{t}", "tenantId": t} for t in self.sees.get(bearer, [])])
        if url.endswith("/Organisation"):
            if self.year_end is None:
                return Answer(500, {"Title": "error"})
            return Answer(200, {"Organisations": [{"OrganisationID": headers["Xero-Tenant-Id"], **self.year_end}]})
        if url.endswith("/Accounts"):
            return Answer(200, {"Accounts": self.accounts})
        if url.endswith("/Contacts"):
            if self.contacts is None:
                return Answer(500, {"Title": "error"})
            return Answer(200, {"Contacts": self.contacts if params["page"] == 1 else []})
        raise AssertionError(f"unexpected Xero GET {url}")

    def delete(self, url, headers=None, timeout=None):
        self.deleted.append(url)
        return Answer(204)


@contextmanager
def _no_lock(user_id, wait_seconds=None):
    yield True


@pytest.fixture(autouse=True)
def _sqlite_has_no_advisory_locks(monkeypatch):
    """SQLite has no ``pg_*advisory*`` functions; on Postgres the real locks run."""
    if connection.vendor != "postgresql":
        monkeypatch.setattr(sync, "lock", lambda entity_id: None)
        monkeypatch.setattr(connect, "refresh_lock", _no_lock)


@pytest.fixture
def xero(monkeypatch):
    return Xero(monkeypatch)


def agree(user) -> None:
    """The live Terms agreed, so the Terms gate lets a Xero-signed-in browser through."""
    TermsConsent.objects.get_or_create(user_id=user.id, terms_version=registry.current_version(registry.TERMS),
                                       defaults={"document_hash": "h", "source": "hub"})


@pytest.fixture
def shop(db):
    """Olive & Vine (no Xero yet) with its admin, and OldCo held by someone else."""
    owner = make_user("owner@test.com")
    agree(owner)
    entity = Entity.objects.create(id=uuid.uuid4(), name="Olive & Vine", status="disconnected")
    UserEntity.objects.create(user_id=owner.id, entity_id=entity.id, role="admin", approved=True)
    rival = make_user("rival@test.com")
    old = Entity.objects.create(id=uuid.uuid4(), name="OldCo", status="connected")
    UserEntity.objects.create(user_id=rival.id, entity_id=old.id, role="admin", approved=True)
    return {"owner": owner, "entity": entity, "rival": rival, "old": old}


def signed_in(user) -> Client:
    return login(Client(), user)


def state_of(response) -> str:
    assert response.status_code == 302, response.content
    return parse_qs(urlsplit(response["Location"]).query)["state"][0]


def callback(client: Client, state: str, **params):
    return client.get("/callback", {"state": state, "code": "code-1", **params})


def flashes(client: Client) -> list[tuple[str, str]]:
    return [tuple(pair) for pair in client.session.get("_flashes", [])]


def said(client: Client) -> str:
    return " | ".join(message for _category, message in flashes(client))


def settings_tab(entity) -> str:
    return f"/entity/{str(entity.id)[:8]}/olive-and-vine/settings/integration"


def onboarding_answer(response) -> dict:
    assert response.status_code == 302
    parts = urlsplit(response["Location"])
    assert f"{parts.scheme}://{parts.netloc}" == ONB, response["Location"]
    return {k: v[0] for k, v in parse_qs(parts.query).items()}


def hold(entity, connector, tenant="t-1"):
    Entity.objects.filter(id=entity.id).update(xero_org_id=tenant, status="connected",
                                               connected_by_user_id=connector.id)
    entity.refresh_from_db()


def fresh(entity) -> Entity:
    return Entity.objects.get(id=entity.id)


# ---- the start routes: Flask's contract ---------------------------------------------------------


def test_xero_sign_in_goes_to_xeros_login_screen_with_the_identity_scopes(db):
    client = Client()
    resp = client.get("/xero_auth")
    query = parse_qs(urlsplit(resp["Location"]).query)
    assert resp["Location"].startswith("https://login.xero.com/identity/connect/authorize?response_type=code")
    assert query["client_id"] == [settings.XERO_CLIENT_ID]
    assert query["redirect_uri"] == ["https://accounts-api.test/callback"]
    assert set(query["scope"][0].split()) == {"openid", "profile", "email", "offline_access"}
    assert query["prompt"] == ["login"]
    base, _, nonce = query["state"][0].rpartition(":")
    assert base == "auth" and len(nonce) >= 32 and client.session["xero_oauth_nonce"] == nonce


def test_an_invitation_rides_in_the_state(db):
    assert state_of(Client().get("/xero_auth", {"invite": "tok-1"})).rsplit(":", 1)[0] == "auth:invite:tok-1"


def test_xero_sign_in_does_not_remember_an_outside_next(db):
    client = Client()
    client.get("/xero_auth", {"next": "https://evil.com/steal"})
    assert "next_after_login" not in client.session
    client.get("/xero_auth", {"next": "/entity"})
    assert client.session["next_after_login"] == "/entity"


def test_connect_requests_exact_minimal_scopes(shop):
    resp = signed_in(shop["owner"]).get("/xero_connect")
    assert set(parse_qs(urlsplit(resp["Location"]).query)["scope"][0].split()) == EXPECTED_SCOPES


def test_reconnect_requests_same_scopes_as_connect(shop):
    resp = signed_in(shop["owner"]).get("/xero_reconnect", {"entity_id": str(shop["entity"].id)})
    query = parse_qs(urlsplit(resp["Location"]).query)
    assert set(query["scope"][0].split()) == EXPECTED_SCOPES
    assert query["entity_id"] == [str(shop["entity"].id)]


def test_connect_states_carry_the_company_its_name_and_who_clicked(shop):
    owner, entity = shop["owner"], shop["entity"]
    client = signed_in(owner)
    assert state_of(client.get("/xero_connect")).rsplit(":", 1)[0] == "entity_connect"
    state = state_of(client.get("/xero_connect", {"from": "onboarding", "entity_id": str(entity.id),
                                                  "entity_name": "Olive & Vine"}))
    # The name is quoted inside the state; Xero hands the state back decoded once.
    assert state.rsplit(":", 1)[0] == f"entity_connect_onboarding:{entity.id}:Olive & Vine:{owner.id}"


def test_connecting_needs_a_sign_in(db):
    resp = Client().get("/xero_connect", {"entity_id": "x"})
    assert resp.status_code == 302 and resp["Location"].startswith("/?next=")


def test_reconnect_needs_a_company_membership_and_xero_rights(shop):
    owner, entity = shop["owner"], shop["entity"]
    client = signed_in(owner)
    resp = client.get("/xero_reconnect")
    assert resp["Location"] == "/index" and "I need to know which entity" in said(client)

    client = signed_in(owner)
    resp = client.get("/xero_reconnect", {"entity_id": str(shop["old"].id)})
    assert resp["Location"] == "/entity" and xero_oauth.NO_ACCESS in said(client)

    cashier = make_user("cash@test.com")
    UserEntity.objects.create(user_id=cashier.id, entity_id=entity.id, role="cashier", approved=True)
    client = signed_in(cashier)
    resp = client.get("/xero_reconnect", {"entity_id": str(entity.id)})
    assert resp["Location"] == "/entity" and "permission to reconnect Xero" in said(client)

    client = signed_in(owner)
    state = state_of(client.get("/xero_reconnect", {"entity_id": str(entity.id)}))
    assert state.rsplit(":", 1)[0] == f"entity_reconnect:{owner.id}"
    assert client.session["reconnect_entity_id"] == str(entity.id)


def test_a_json_caller_is_refused_with_flasks_json(shop):
    cashier = make_user("cash@test.com")
    UserEntity.objects.create(user_id=cashier.id, entity_id=shop["entity"].id, role="cashier", approved=True)
    resp = signed_in(cashier).get("/xero_reconnect", {"entity_id": str(shop["entity"].id)},
                                  HTTP_ACCEPT="application/json")
    assert resp.status_code == 403
    assert resp.content == b'{"message":"You do not have permission to reconnect Xero for this entity.","status":"error"}\n'


def test_the_start_routes_and_the_callback_are_get_only(db):
    for path in ("/xero_auth", "/xero_connect", "/xero_reconnect", "/callback"):
        assert Client().post(path).status_code == 405


# ---- the nonce (gap 2) --------------------------------------------------------------------------


def test_a_callback_nobody_here_started_is_refused_before_any_exchange(shop, xero):
    """A forged sign-in link: the victim's browser has no nonce, so nobody is signed in."""
    client = Client()
    resp = callback(client, "auth")
    assert resp["Location"] == "/" and xero.posts == []
    assert flashes(client) == [("danger", xero_oauth.NOT_FROM_HERE_SIGN_IN)]
    assert "user_id" not in client.session


def test_a_nonce_that_is_not_this_sessions_is_refused(shop, xero):
    client = Client()
    state = state_of(client.get("/xero_auth"))
    resp = callback(client, state.rsplit(":", 1)[0] + ":" + "x" * 43)
    assert resp["Location"] == "/" and xero.posts == [] and "user_id" not in client.session


def test_a_state_started_in_another_browser_is_refused(shop, xero):
    state = state_of(Client().get("/xero_auth"))
    attacker_link = callback(Client(), state)
    assert attacker_link["Location"] == "/" and xero.posts == []


def test_a_callback_replayed_with_the_same_state_is_refused(shop, xero):
    client = Client()
    state = state_of(client.get("/xero_auth"))
    assert callback(client, state)["Location"] == "/entity"
    assert len(xero.posts) == 1
    again = callback(client, state)
    assert again["Location"] == "/" and len(xero.posts) == 1
    assert flashes(client)[-1] == ("danger", xero_oauth.NOT_FROM_HERE_SIGN_IN)


def test_a_refused_connect_lands_where_a_failed_connect_lands(shop, xero):
    owner, entity = shop["owner"], shop["entity"]
    client = signed_in(owner)
    resp = callback(client, f"entity_connect:{entity.id}::{owner.id}:forged")
    assert resp["Location"] == "/entity" and xero_oauth.NOT_FROM_HERE_CONNECT in said(client)

    resp = callback(client, f"entity_connect_onboarding:{entity.id}::{owner.id}")
    assert onboarding_answer(resp) == {"xero": "cancelled", "step": "3"}

    client.get("/xero_reconnect", {"entity_id": str(entity.id)})
    resp = callback(client, f"entity_reconnect:{owner.id}:forged")
    assert resp["Location"] == settings_tab(entity)
    assert xero.posts == [] and fresh(entity).xero_org_id is None


def test_a_second_start_replaces_the_first_nonce(shop, xero):
    client = Client()
    first = state_of(client.get("/xero_auth"))
    second = state_of(client.get("/xero_auth"))
    assert callback(client, first)["Location"] == "/"
    assert xero.posts == []
    assert "xero_oauth_nonce" not in client.session  # spent by the refused one
    assert callback(client, second)["Location"] == "/"


# ---- the sign-in --------------------------------------------------------------------------------


def test_signing_in_with_xero_signs_in_the_owner_of_that_address_and_keeps_the_tokens(shop, xero):
    owner = shop["owner"]
    client = Client()
    resp = callback(client, state_of(client.get("/xero_auth")))
    assert resp["Location"] == "/entity"
    assert client.session["user_id"] == str(owner.id)
    assert flashes(client) == [("success", "You're signed in with Xero.")]
    assert xero.posts[0] == {"grant_type": "authorization_code", "code": "code-1",
                             "redirect_uri": "https://accounts-api.test/callback"}
    row = UserToken.objects.get(user_id=owner.id)
    assert row.access_token == xero.grant_token and row.refresh_token == "refresh-1" and row.access_token_expires_in == 1800
    assert row.refresh_token_last_used_at is None
    assert User.objects.get(id=owner.id).xero_email == "owner@test.com"


def test_a_stale_flash_is_drained_and_next_after_login_kept(shop, xero):
    client = Client()
    state = state_of(client.get("/xero_auth", {"next": "/entity/abc/dashboard"}))
    session = client.session
    session["_flashes"] = [["danger", "Hmm, I looked everywhere but couldn't find that one."]]
    session.save()
    resp = callback(client, state)
    assert resp["Location"] == "/entity/abc/dashboard"
    assert flashes(client) == [("success", "You're signed in with Xero.")]


def test_a_xero_login_nobody_holds_gets_an_account(db, xero):
    xero.email, xero.claims = "New.Person@Test.com", {"given_name": "Nia", "family_name": "New"}
    client = Client()
    callback(client, state_of(client.get("/xero_auth")))
    person = User.objects.get(email="new.person@test.com")
    assert (person.first_name, person.last_name, person.xero_email, person.username) == (
        "Nia", "New", "new.person@test.com", "new.person@test.com")
    assert person.approved and client.session["user_id"] == str(person.id)
    assert person.password.startswith("pbkdf2:sha256:")


def test_a_xero_login_without_an_email_is_refused(db, xero):
    xero.email = None
    client = Client()
    resp = callback(client, state_of(client.get("/xero_auth")))
    assert resp["Location"] == "/" and xero_oauth.NO_EMAIL in said(client) and User.objects.count() == 0


def test_xero_refusing_the_code_signs_nobody_in(shop, xero):
    xero.exchange = 400
    client = Client()
    resp = callback(client, state_of(client.get("/xero_auth")))
    assert resp["Location"] == "/" and xero_oauth.SIGN_IN_FAILED in said(client) and "user_id" not in client.session


def test_a_switched_off_account_is_not_signed_in(shop, xero):
    User.objects.filter(id=shop["owner"].id).update(is_active=False)
    client = Client()
    callback(client, state_of(client.get("/xero_auth")))
    assert "user_id" not in client.session


def invitation(entity, email, token="inv-1"):
    return Invitation.objects.create(id=uuid.uuid4(), entity_id=entity.id, email=email, role="cashier", token=token,
                                     status="pending", expires_at=datetime.now(UTC) + timedelta(days=3))


def test_an_invitee_with_no_account_is_made_one_and_joins_the_company(shop, xero):
    entity = shop["entity"]
    invitation(entity, "Invitee@Test.com")
    xero.email = "invitee@test.com"
    client = Client()
    resp = callback(client, state_of(client.get("/xero_auth", {"invite": "inv-1"})))
    person = User.objects.get(email="invitee@test.com")
    assert resp["Location"] == f"/entity/{str(entity.id)[:8]}/olive-and-vine/petty-cash"
    assert UserEntity.objects.filter(user_id=person.id, entity_id=entity.id, role="cashier").exists()
    assert client.session["user_id"] == str(person.id)
    assert flashes(client) == [("success", "Invitation accepted — welcome aboard!")]


def test_an_invitation_for_another_address_makes_nobody(shop, xero):
    invitation(shop["entity"], "someone@test.com")
    xero.email = "invitee@test.com"
    client = Client()
    resp = callback(client, state_of(client.get("/xero_auth", {"invite": "inv-1"})))
    assert resp["Location"] == "/" and "This invite went to a different account" in said(client)
    assert not User.objects.filter(email="invitee@test.com").exists()


def test_a_used_invitation_makes_nobody(shop, xero):
    xero.email = "invitee@test.com"
    client = Client()
    resp = callback(client, state_of(client.get("/xero_auth", {"invite": "gone"})))
    assert resp["Location"] == "/" and "This invitation doesn't work anymore" in said(client)


def test_an_existing_person_accepting_someone_elses_invitation_is_not_signed_in(shop, xero):
    invitation(shop["entity"], "someone@test.com")
    client = Client()
    resp = callback(client, state_of(client.get("/xero_auth", {"invite": "inv-1"})))
    query = parse_qs(urlsplit(resp["Location"]).query)
    assert resp["Location"].startswith(f"{settings.MINTY_WEB_URL}/login?")
    assert query["invite"] == ["inv-1"] and query["email"] == ["someone@test.com"] and "flash" in query
    assert "user_id" not in client.session


# ---- connect --------------------------------------------------------------------------------------


def connect_flow(client, entity=None, **params):
    if entity is not None:
        params.setdefault("entity_id", str(entity.id))
    return state_of(client.get("/xero_connect", params))


def test_a_connect_writes_the_organisation_to_the_company_in_the_state(shop, xero):
    owner, entity = shop["owner"], shop["entity"]
    client = signed_in(owner)
    resp = callback(client, connect_flow(client, entity))
    assert resp["Location"] == "/entity"
    org = fresh(entity)
    assert (org.xero_org_id, org.xero_tenant_name, org.status, org.connected_by_user_id) == (
        "t-1", "Acme Ltd", "connected", owner.id)
    assert (org.financial_year_end_day, org.financial_year_end_month) == (31, 3) and org.last_connected_at
    assert UserToken.objects.get(user_id=owner.id).access_token == xero.grant_token
    assert xero.syncs == [(entity.id, xero.grant_token, "t-1", "Entity Connect")]
    assert flashes(client) == [("success", "Connected to Xero! I'm pulling in your accounts and contacts now.")]
    assert sorted(AccountInfo.objects.filter(entity_id=entity.id).values_list("xero_account_id", flat=True)) == [
        "new-400", "new-420"]
    assert xero.deleted == []


def test_an_onboarding_connect_keeps_the_company_onboarding_and_goes_back_to_the_wizard(shop, xero):
    owner, entity = shop["owner"], shop["entity"]
    Entity.objects.filter(id=entity.id).update(status="onboarding")
    client = signed_in(owner)
    resp = callback(client, connect_flow(client, entity, **{"from": "onboarding", "entity_name": "Olive & Vine"}))
    assert onboarding_answer(resp) == {"xero": "connected", "step": "3", "org": "Acme Ltd"}
    assert fresh(entity).status == "onboarding" and fresh(entity).xero_org_id == "t-1"
    assert "_flashes" not in client.session or flashes(client) == []


def test_each_persons_callback_connects_their_own_company(shop, xero):
    """The race the state's id fixed: the newest company is never taken for someone else's."""
    alice, bob = shop["owner"], make_user("bob@test.com")
    mine, theirs = shop["entity"], Entity.objects.create(id=uuid.uuid4(), name="Bob Ltd", status="disconnected")
    UserEntity.objects.create(user_id=bob.id, entity_id=theirs.id, role="admin", approved=True)
    a, b = signed_in(alice), signed_in(bob)
    state_a, state_b = connect_flow(a, mine), connect_flow(b, theirs)
    callback(a, state_a)
    xero.email, xero.granted = "bob@test.com", [{"id": "conn-2", "tenantId": "t-2", "tenantName": "Bob Corp"}]
    callback(b, state_b)
    assert (fresh(mine).xero_org_id, fresh(mine).xero_tenant_name) == ("t-1", "Acme Ltd")
    assert (fresh(theirs).xero_org_id, fresh(theirs).xero_tenant_name) == ("t-2", "Bob Corp")


def test_without_a_company_in_the_state_nothing_is_connected(shop, xero):
    """Flask fell back to the newest company of ANYONE; closed in the move (2026-11-06)."""
    client = signed_in(shop["owner"])
    resp = callback(client, connect_flow(client))
    assert resp["Location"] == "/entity" and "I couldn't find that entity to connect" in said(client)
    assert not Entity.objects.filter(xero_org_id="t-1").exists()


def test_someone_elses_company_cannot_be_connected_to_your_xero(shop, xero):
    """Flask asked only ENTITY_CREATE (everyone) and wrote onto whatever id the state named."""
    client = signed_in(shop["owner"])
    refused = client.get("/xero_connect", {"entity_id": str(shop["old"].id)})
    assert refused["Location"] == "/entity" and "permission to connect this entity" in said(client)
    assert "xero.com" not in refused["Location"]


def test_a_cashier_cannot_connect_their_company(shop, xero):
    cashier = make_user("cash@test.com")
    agree(cashier)
    UserEntity.objects.create(user_id=cashier.id, entity_id=shop["entity"].id, role="cashier", approved=True)
    client = signed_in(cashier)
    assert "xero.com" not in client.get("/xero_connect", {"entity_id": str(shop["entity"].id)})["Location"]
    assert fresh(shop["entity"]).xero_org_id is None


def test_onboarding_without_an_id_finds_the_persons_own_company_by_name(shop, xero):
    owner = shop["owner"]
    mine = Entity.objects.create(id=uuid.uuid4(), name="Vine Two", status="onboarding",
                                 created_at=datetime(2026, 1, 1, tzinfo=UTC))
    newer = Entity.objects.create(id=uuid.uuid4(), name="Vine Three", status="onboarding",
                                  created_at=datetime(2026, 2, 1, tzinfo=UTC))
    for org in (mine, newer):
        UserEntity.objects.create(user_id=owner.id, entity_id=org.id, role="admin", approved=True)
    client = signed_in(owner)
    callback(client, connect_flow(client, **{"from": "onboarding", "entity_name": "Vine Two"}))
    assert fresh(mine).xero_org_id == "t-1" and fresh(newer).xero_org_id is None


def test_a_company_that_is_not_there_is_not_connected(shop, xero):
    client = signed_in(shop["owner"])
    resp = client.get("/xero_connect", {"entity_id": str(uuid.uuid4())})
    assert resp["Location"] == "/entity" and "xero.com" not in resp["Location"]
    assert not Entity.objects.filter(xero_org_id="t-1").exists()


@pytest.mark.parametrize("onboarding", [False, True])
def test_no_financial_year_end_fails_the_connect_and_hands_the_grant_back(shop, xero, onboarding):
    xero.year_end = None
    client = signed_in(shop["owner"])
    params = {"from": "onboarding"} if onboarding else {}
    resp = callback(client, connect_flow(client, shop["entity"], **params))
    if onboarding:
        assert onboarding_answer(resp)["xero"] == "cancelled"
    else:
        assert resp["Location"] == "/entity" and xero_oauth.CONNECT_FAILED in said(client)
    assert fresh(shop["entity"]).xero_org_id is None
    assert xero.deleted == [f"{connect.CONNECTIONS_URL}/conn-1"]
    assert not UserToken.objects.filter(user_id=shop["owner"].id).exists()


def test_no_chart_from_xero_refuses_the_connect_changes_nothing_and_hands_the_grant_back(shop, xero):
    entity = shop["entity"]
    AccountInfo.objects.create(entity_id=entity.id, type="EXPENSE", name="Old", xero_account_id="old-400",
                               status="ACTIVE")
    xero.accounts = []
    client = signed_in(shop["owner"])
    callback(client, connect_flow(client, entity))
    assert fresh(entity).xero_org_id is None and xero_oauth.CONNECT_FAILED in said(client)
    assert list(AccountInfo.objects.filter(entity_id=entity.id).values_list("xero_account_id", flat=True)) == [
        "old-400"]
    assert xero.deleted == [f"{connect.CONNECTIONS_URL}/conn-1"]


def test_a_connect_after_the_onboarding_disconnect_keeps_only_the_new_orgs_data(shop, xero):
    """QA #2 (2026-11-03): the company records no organisation, yet its old rows go."""
    entity = shop["entity"]
    for aid, code in (("old-400", "400"), ("old-404", "404")):
        AccountInfo.objects.create(entity_id=entity.id, type="EXPENSE", name=code, xero_account_id=aid,
                                   xero_code=code, status="ACTIVE")
    AccountInfo.objects.create(entity_id=entity.id, type="EXPENSE", name="No id", status="ACTIVE")
    XeroContactSync.objects.create(entity_id=entity.id, xero_contact_id="old-c1", xero_org_id="org-old", name="Old")
    XeroContactSync.objects.create(entity_id=entity.id, xero_contact_id="stray", xero_org_id=None, name="No org")
    XeroContactSync.objects.create(entity_id=entity.id, xero_contact_id="keep", xero_org_id="t-1", name="Same org")
    client = signed_in(shop["owner"])
    callback(client, connect_flow(client, entity))
    assert sorted(AccountInfo.objects.filter(entity_id=entity.id).values_list("xero_account_id", flat=True)) == [
        "new-400", "new-420"]
    assert sorted(EntityAccountXero.objects.filter(account__entity_id=entity.id)
                  .values_list("xero_account_id", flat=True)) == ["new-400", "new-420"]
    assert sorted(EntityBillAccountXero.objects.filter(entity_id=entity.id)
                  .values_list("xero_account_id", flat=True)) == ["new-400", "new-420"]
    assert list(XeroContactSync.objects.filter(entity_id=entity.id).values_list("xero_contact_id", flat=True)) == [
        "keep"]


def test_the_wrong_xero_login_is_refused_and_the_whole_auth_event_handed_back(shop, xero):
    xero.email = "intruder@test.com"
    make_user("intruder@test.com")
    xero.granted = [{"id": "conn-1", "tenantId": "t-1"}, {"id": "conn-2", "tenantId": "t-2"}]
    client = signed_in(shop["owner"])
    resp = callback(client, connect_flow(client, shop["entity"]))
    assert resp["Location"] == "/entity"
    assert ("danger", "You must connect with the Xero account for owner@test.com. You logged in as "
                      "intruder@test.com.") in flashes(client)
    assert xero.deleted == [f"{connect.CONNECTIONS_URL}/conn-1", f"{connect.CONNECTIONS_URL}/conn-2"]
    assert fresh(shop["entity"]).xero_org_id is None and client.session["user_id"] == str(shop["owner"].id)


def test_the_wrong_xero_login_from_onboarding_says_mismatch(shop, xero):
    xero.email = "intruder@test.com"
    client = signed_in(shop["owner"])
    resp = callback(client, connect_flow(client, shop["entity"], **{"from": "onboarding"}))
    assert onboarding_answer(resp) == {"xero": "mismatch", "step": "3", "expected": "owner@test.com"}


def _onboarding_company(owner, name):
    company = Entity.objects.create(id=uuid.uuid4(), name=name, status="onboarding")
    UserEntity.objects.create(user_id=owner.id, entity_id=company.id, role="admin", approved=True)
    return company


def test_a_company_name_with_a_colon_keeps_its_name_and_the_login_check(shop, xero):
    """Flask split the state from the left: a ":" in the name moved the initiator, and the
    wrong-login check was skipped (fixed 2026-11-06)."""
    company = _onboarding_company(shop["owner"], "A:B Trading: HK")
    client = signed_in(shop["owner"])
    flow = {"from": "onboarding", "entity_name": "A:B Trading: HK"}
    xero.email = "intruder@test.com"
    refused = callback(client, connect_flow(client, **flow))
    assert onboarding_answer(refused)["xero"] == "mismatch" and fresh(company).xero_org_id is None
    xero.email = "owner@test.com"
    callback(client, connect_flow(client, **flow))
    assert fresh(company).xero_org_id == "t-1"


@pytest.mark.parametrize("onboarding", [False, True])
def test_a_xero_login_no_account_holds_is_told_and_its_grant_handed_back(shop, xero, onboarding):
    """Flask said nothing and left the grant live at Xero (fixed 2026-11-06)."""
    xero.email = "nobody@test.com"
    client = signed_in(shop["owner"])
    resp = callback(client, connect_flow(client, **({"from": "onboarding"} if onboarding else {})))
    if onboarding:  # the wizard reads its own answer; flashes never cross to it (Flask's rule)
        assert onboarding_answer(resp)["xero"] == "cancelled"
    else:
        assert "nobody@test.com isn't linked to a Minty account" in said(client)
    assert xero.deleted == [f"{connect.CONNECTIONS_URL}/conn-1"]
    assert not Entity.objects.filter(xero_org_id="t-1").exists()


def test_a_terms_change_mid_connect_does_not_lose_the_connect(shop, xero):
    """The start is gated; Xero's return is not, so a connect already granted at Xero lands."""
    client = signed_in(shop["owner"])
    state = connect_flow(client, shop["entity"])
    TermsConsent.objects.filter(user_id=shop["owner"].id).delete()
    assert callback(client, state)["Location"] == "/entity"
    assert fresh(shop["entity"]).xero_org_id == "t-1"
    assert "xero.com" not in client.get("/xero_connect", {"entity_id": str(shop["entity"].id)})["Location"]


def test_cancelling_on_xeros_screen(shop, xero):
    client = signed_in(shop["owner"])
    resp = callback(client, connect_flow(client, shop["entity"]), error="access_denied")
    assert resp["Location"] == "/entity" and flashes(client) == [("success", "Your new petty cash entity is ready!")]
    resp = callback(client, connect_flow(client, shop["entity"], **{"from": "onboarding"}), error="access_denied")
    assert onboarding_answer(resp) == {"xero": "cancelled", "step": "3"}
    assert xero.posts == []


# ---- one Xero organisation = one company ----------------------------------------------------------


def test_an_organisation_another_company_really_holds_is_refused(shop, xero):
    old, rival = shop["old"], shop["rival"]
    hold(old, rival)
    xero.tokens[str(old.id)], xero.sees["their-token"] = ("their-token", "t-1"), ["t-1"]
    client = signed_in(shop["owner"])
    resp = callback(client, connect_flow(client, shop["entity"]))
    assert resp["Location"] == "/entity"
    assert fresh(shop["entity"]).xero_org_id is None and fresh(old).xero_org_id == "t-1"
    assert xero.deleted == [f"{connect.CONNECTIONS_URL}/conn-1"]
    assert 'already connected to "OldCo"' in said(client) and 'Disconnect it from "OldCo" first' in said(client)


def test_the_onboarding_refusal_names_the_holder_and_offers_the_move_only_in_development(shop, xero, settings):
    old, owner = shop["old"], shop["owner"]
    hold(old, owner)  # the same person's other company: believed without asking Xero
    UserEntity.objects.create(user_id=owner.id, entity_id=old.id, role="admin", approved=True)
    for development, can_move in ((True, "1"), (False, None)):
        settings.DEVELOPMENT = development
        client = signed_in(owner)
        answer = onboarding_answer(callback(client, connect_flow(client, shop["entity"], **{"from": "onboarding"})))
        assert answer["xero"] == "conflict" and answer["conflict_entity"] == "OldCo"
        assert answer["conflict_entity_id"] == str(old.id) and answer.get("conflict_can_move") == can_move
    assert xero.probes == []


def test_a_ghost_claim_is_cleared_and_the_connect_goes_through(shop, xero):
    old = shop["old"]
    hold(old, shop["rival"])  # its connector's token is dead
    client = signed_in(shop["owner"])
    callback(client, connect_flow(client, shop["entity"]))
    assert fresh(shop["entity"]).xero_org_id == "t-1"
    # the ghost is disconnected and keeps its organisation (Xero optional): no longer a claim
    assert (fresh(old).xero_org_id, fresh(old).status, fresh(old).connected_by_user_id) == ("t-1", "disconnected", None)


@pytest.mark.parametrize("probe", [500, "raise", "gone"])
def test_a_claim_xero_cannot_confirm_is_a_ghost(shop, xero, probe):
    old = shop["old"]
    hold(old, shop["rival"])
    xero.tokens[str(old.id)] = ("their-token", "t-1")
    if probe == "gone":
        xero.sees["their-token"] = ["t-other"]
    else:
        xero.probe_status["their-token"] = probe
    assert connect.live_org_claimant("t-1", shop["entity"].id, shop["owner"].id) is None
    assert fresh(old).xero_org_id == "t-1" and fresh(old).status == "disconnected"


def test_a_disconnected_claimant_never_blocks(shop, xero):
    old = shop["old"]
    hold(old, shop["rival"])
    Entity.objects.filter(id=old.id).update(status="disconnected")
    assert connect.live_org_claimant("t-1", shop["entity"].id, shop["owner"].id) is None
    assert fresh(old).xero_org_id == "t-1" and xero.probes == []  # kept, untouched, never asked about


def test_every_duplicate_claimant_is_examined(shop, xero):
    first, second = shop["old"], Entity.objects.create(id=uuid.uuid4(), name="Dup", status="connected")
    hold(first, shop["rival"])
    hold(second, shop["rival"])
    xero.tokens[str(second.id)], xero.sees["dup-token"] = ("dup-token", "t-1"), ["t-1"]
    assert connect.live_org_claimant("t-1", shop["entity"].id, shop["owner"].id) == second
    assert (fresh(first).xero_org_id, fresh(first).status) == ("t-1", "disconnected")


def test_an_empty_auth_event_names_the_company_holding_the_org_and_revokes_nothing(shop, xero):
    """THE REPORTED BUG (2026-10-09): Xero grants nothing when the organisation is already connected
    to the app, so the event is empty; the holder is named, never blamed on the person."""
    old, owner = shop["old"], shop["owner"]
    hold(old, owner)
    xero.granted, xero.visible = [], [{"id": "conn-9", "tenantId": "t-1"}]
    client = signed_in(owner)
    callback(client, connect_flow(client, shop["entity"]))
    assert 'already connected to "OldCo"' in said(client) and "not connected to Xero yet" not in said(client)
    assert xero.deleted == [] and fresh(old).xero_org_id == "t-1" and fresh(shop["entity"]).xero_org_id is None


def test_an_empty_auth_event_with_nothing_visible_says_nothing_was_picked(shop, xero):
    xero.granted, xero.visible = [], []
    client = signed_in(shop["owner"])
    callback(client, connect_flow(client, shop["entity"]))
    assert "not connected to Xero yet" in said(client) and fresh(shop["entity"]).xero_org_id is None


def test_an_empty_auth_event_connects_the_one_organisation_nobody_holds(shop, xero):
    xero.granted, xero.visible = [], [{"id": "conn-9", "tenantId": "t-9", "tenantName": "Demo Company"}]
    client = signed_in(shop["owner"])
    callback(client, connect_flow(client, shop["entity"]))
    assert (fresh(shop["entity"]).xero_org_id, fresh(shop["entity"]).xero_tenant_name) == ("t-9", "Demo Company")
    assert "Connected to Xero" in said(client) and xero.deleted == []


def test_several_unclaimed_organisations_connect_none_and_ask_to_choose(shop, xero):
    xero.granted, xero.visible = [], [{"id": "a", "tenantId": "t-8"}, {"id": "b", "tenantId": "t-9"}]
    client = signed_in(shop["owner"])
    resp = callback(client, connect_flow(client, shop["entity"], **{"from": "onboarding"}))
    assert onboarding_answer(resp)["xero"] == "choose"
    assert fresh(shop["entity"]).xero_org_id is None and xero.deleted == []


# ---- reconnect ------------------------------------------------------------------------------------


def reconnect_flow(client, entity):
    return state_of(client.get("/xero_reconnect", {"entity_id": str(entity.id)}))


def test_a_reconnect_reconnects_and_drains_an_earlier_refusal(shop, xero):
    owner, entity = shop["owner"], shop["entity"]
    xero.tokens[str(entity.id)] = "GRANT"  # the status sync then sees t-1 with the new token
    client = signed_in(owner)
    state = reconnect_flow(client, entity)
    session = client.session
    session["_flashes"] = [["danger", "already connected to another entity"]]
    session.save()
    resp = callback(client, state)
    assert resp["Location"] == settings_tab(entity)
    org = fresh(entity)
    assert (org.xero_org_id, org.status, org.connected_by_user_id) == ("t-1", "connected", owner.id)
    assert (org.financial_year_end_day, org.financial_year_end_month) == (31, 3)
    assert flashes(client) == [("success", "Reconnected to Xero! I'm pulling in your accounts and contacts now.")]
    assert xero.syncs == [(entity.id, xero.grant_token, "t-1", "Entity Reconnect")]
    assert UserToken.objects.get(user_id=owner.id).access_token == xero.grant_token


def test_the_status_sync_after_a_reconnect_reads_xeros_connections(shop, xero):
    entity = shop["entity"]
    xero.tokens[str(entity.id)] = ("someone-elses", "t-1")  # this token no longer sees t-1
    client = signed_in(shop["owner"])
    callback(client, reconnect_flow(client, entity))
    assert xero.probes == ["someone-elses"] and fresh(entity).status == "disconnected"


def test_a_reconnect_refused_by_a_holder_carries_it_to_the_tab(shop, xero):
    old, rival, entity = shop["old"], shop["rival"], shop["entity"]
    hold(old, rival)
    xero.tokens[str(old.id)], xero.sees["their-token"] = ("their-token", "t-1"), ["t-1"]
    client = signed_in(shop["owner"])
    resp = callback(client, reconnect_flow(client, entity))
    parts = urlsplit(resp["Location"])
    assert parts.path == settings_tab(entity)
    assert read_conflict(parse_qs(parts.query)["xero_conflict"][0]) == {
        "entity_id": str(old.id), "entity_name": "OldCo", "can_move": False}
    assert xero.deleted == [f"{connect.CONNECTIONS_URL}/conn-1"] and fresh(entity).xero_org_id is None


def test_a_reconnect_with_an_empty_auth_event_names_the_holder_and_revokes_nothing(shop, xero):
    old = shop["old"]
    hold(old, shop["owner"])
    xero.granted, xero.visible = [], [{"id": "conn-9", "tenantId": "t-1"}]
    client = signed_in(shop["owner"])
    resp = callback(client, reconnect_flow(client, shop["entity"]))
    assert "xero_conflict=" in resp["Location"] and 'already connected to "OldCo"' in said(client)
    assert xero.deleted == [] and "No organization was picked" not in said(client)


@pytest.mark.parametrize("visible, connected", [
    ([{"id": "a", "tenantId": "t-9"}], True),
    ([{"id": "a", "tenantId": "t-8"}, {"id": "b", "tenantId": "t-9"}], False),
    ([], False),
])
def test_a_reconnect_with_an_empty_auth_event_and_unclaimed_organisations(shop, xero, visible, connected):
    xero.granted, xero.visible = [], visible
    client = signed_in(shop["owner"])
    resp = callback(client, reconnect_flow(client, shop["entity"]))
    assert resp["Location"] == settings_tab(shop["entity"])
    assert (fresh(shop["entity"]).xero_org_id == "t-9") is connected
    if len(visible) > 1:
        assert "more than one" in said(client)
    if not visible:
        assert "No organization was picked" in said(client)


def test_a_reconnect_without_a_year_end_or_a_chart_stays_disconnected(shop, xero):
    entity = shop["entity"]
    for broken in ("year_end", "accounts"):
        setattr(xero, broken, None if broken == "year_end" else [])
        client = signed_in(shop["owner"])
        resp = callback(client, reconnect_flow(client, entity))
        assert resp["Location"] == settings_tab(entity) and xero_oauth.RECONNECT_FAILED in said(client)
        assert fresh(entity).xero_org_id is None and fresh(entity).status == "disconnected"
        xero.year_end, xero.accounts = {"FinancialYearEndDay": 31, "FinancialYearEndMonth": 3}, list(NEW_ACCOUNTS)
    assert xero.deleted == [f"{connect.CONNECTIONS_URL}/conn-1"] * 2


def test_a_reconnect_with_the_wrong_xero_login_or_declined(shop, xero):
    entity = shop["entity"]
    xero.email = "intruder@test.com"
    client = signed_in(shop["owner"])
    resp = callback(client, reconnect_flow(client, entity))
    assert resp["Location"] == settings_tab(entity) and "You must reconnect with the Xero account for" in said(client)
    client = signed_in(shop["owner"])
    resp = callback(client, reconnect_flow(client, entity), error="access_denied")
    assert resp["Location"] == settings_tab(entity) and "Xero didn't approve that" in said(client)
    assert fresh(entity).xero_org_id is None


def test_a_reconnect_that_does_not_know_its_company(shop, xero):
    client = signed_in(shop["owner"])
    state = state_of(client.get("/xero_auth"))  # a nonce for this session...
    nonce = state.rsplit(":", 1)[1]
    resp = callback(client, f"entity_reconnect:{shop['owner'].id}:{nonce}")  # ...on a reconnect state
    assert resp["Location"] == "/entity" and "I need to know which entity to reconnect." in said(client)


# ---- the connect-time sync ------------------------------------------------------------------------


def test_the_connect_sync_mirrors_contacts_and_accounts(shop, xero):
    entity = shop["entity"]
    XeroContactSync.objects.create(entity_id=entity.id, xero_contact_id="gone", xero_org_id="t-1", name="Gone")
    XeroContactSync.objects.create(entity_id=entity.id, xero_contact_id="new-c1", xero_org_id="t-1", name="Old name")
    connect.background_sync(entity.id, "tok", "t-1")
    assert list(XeroContactSync.objects.filter(entity_id=entity.id).values_list("xero_contact_id", "name")) == [
        ("new-c1", "New Supplier")]
    assert sorted(AccountInfo.objects.filter(entity_id=entity.id).values_list("xero_account_id", flat=True)) == [
        "new-400", "new-420"]


def test_a_failed_contact_fetch_drops_nothing(shop, xero):
    entity = shop["entity"]
    XeroContactSync.objects.create(entity_id=entity.id, xero_contact_id="kept", xero_org_id="t-1", name="Kept")
    xero.contacts = None
    connect.background_sync(entity.id, "tok", "t-1")
    assert list(XeroContactSync.objects.filter(entity_id=entity.id).values_list("xero_contact_id", flat=True)) == [
        "kept"]


def test_the_sync_thread_carries_the_log_context_and_closes_its_connection(monkeypatch):
    started = []

    class Thread:
        def __init__(self, target, args, daemon):
            started.append((target, args, daemon))

        def start(self):
            pass

        def is_alive(self):
            return True

    monkeypatch.setattr(xero_oauth.threading, "Thread", Thread)
    monkeypatch.setattr(xero_oauth.time, "sleep", lambda _s: None)
    xero_oauth.start_background_sync("e-1", "tok", "t-1", "Entity Connect")
    (target, args, daemon), = started
    assert args == ("e-1", "tok", "t-1") and daemon is True and target.__name__ == "run"

    ran = []
    monkeypatch.setattr(connect, "background_sync", lambda *a: ran.append(a))
    closed = []
    monkeypatch.setattr(connect.connection, "close", lambda: closed.append(True))
    target(*args)
    assert ran == [("e-1", "tok", "t-1")] and closed == [True]


# ---- a Minty company's connect matches instead of mirroring (Settings Phase 2, 2026-11-11) -----------


def test_a_minty_companys_connect_keeps_every_account_and_goes_to_matching(shop, xero):
    """It used to delete every account with no Xero id - all of a Minty company's own - and every
    supplier with no organisation. Now nothing is deleted: the organisation is recorded, the company
    stays disconnected, and the person lands on the matching page."""
    from minty_db.models import AccountMatchDraft

    entity = shop["entity"]
    Entity.objects.filter(id=entity.id).update(setup_mode="minty", status="disconnected")
    own = AccountInfo.objects.create(entity_id=entity.id, type="EXPENSE", account_type="expense", name="Rent",
                                     code="400", xero_code="400", status="ACTIVE", source="template")
    supplier = XeroContactSync.objects.create(entity_id=entity.id, name="ABC Supplies", source="user")
    client = signed_in(shop["owner"])

    resp = callback(client, connect_flow(client, entity))

    assert resp["Location"].endswith("/settings/integration/match")
    company = fresh(entity)
    assert (company.xero_org_id, company.status, company.setup_mode) == ("t-1", "disconnected", "minty")
    assert AccountInfo.objects.filter(id=own.id, xero_account_id__isnull=True).exists()
    assert XeroContactSync.objects.filter(id=supplier.id).exists()
    assert AccountMatchDraft.objects.filter(account_id=own.id).exists(), "400 = 400, same type: picked"
