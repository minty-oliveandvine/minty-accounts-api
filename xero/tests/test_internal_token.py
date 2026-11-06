"""``POST /api/internal/xero/token``: Flask's characterisation tests (``Minty/tests/test_char_xero_tokens.py``,
the five service tests), ported, plus the lock and the exact bytes.

Xero's identity server is stubbed at ``requests.post``, as Flask's tests stub it; nothing in this
service is patched. Postgres only: the refresh lock is a Postgres advisory lock.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import psycopg
import pytest
from django.conf import settings
from django.db import connection
from django.test import Client
from minty_db.enums import EntityStatus
from minty_db.models import CountryInfo, CurrencyInfo, Entity, User, UserToken

from xero.services import tokens

pytestmark = pytest.mark.skipif(
    not os.environ.get("MINTY_TEST_PG_URI"), reason="Postgres only (the refresh lock)"
)

TENANT = "11111111-2222-3333-4444-555555555555"
URL = "/api/internal/xero/token"


def make_user(email="owner@test.com") -> User:
    return User.objects.create(id=uuid.uuid4(), email=email, username=email, password="x",
                               first_name="Olive", last_name="Owner", approved=True)


def make_entity(*, connected_by=None, xero_org_id=TENANT) -> Entity:
    currency = CurrencyInfo.objects.filter(currency_code="HKD").first() or CurrencyInfo.objects.create(
        id=uuid.uuid4(), currency_code="HKD", currency_name="HKD", symbol="$", decimal_places=2)
    country = CountryInfo.objects.filter(country_code="HK").first() or CountryInfo.objects.create(
        country_code="HK", alpha3_code="HKG", country_name_en="Hong Kong", currency_id=currency.id)
    return Entity.objects.create(
        id=uuid.uuid4(), name="Acme Shop", status=EntityStatus.DISCONNECTED, currency_id=currency.id,
        country_code=country.country_code, xero_org_id=xero_org_id,
        connected_by_user_id=connected_by.id if connected_by else None,
    )


@pytest.fixture
def connected(db):
    """A company connected to Xero by its admin, holding a fresh token bundle."""
    owner = make_user()
    entity = make_entity(connected_by=owner)
    UserToken.objects.create(
        id=uuid.uuid4(), user_id=owner.id, access_token="access-1", refresh_token="refresh-1",
        id_token="id-1", access_token_expires_in=1800, access_token_obtained_at=datetime.now(UTC),
    )
    return owner, entity


def age_token(owner, seconds):
    UserToken.objects.filter(user_id=owner.id).update(
        access_token_obtained_at=datetime.now(UTC) - timedelta(seconds=seconds))


class XeroIdentity:
    """identity.xero.com at the ``requests.post`` boundary; records every call."""

    def __init__(self):
        self.calls: list[dict] = []
        self.refresh_response = {"status": 200, "json": {
            "access_token": "access-2", "refresh_token": "refresh-2", "id_token": "id-2",
            "expires_in": 1800}}

    class _Resp:
        def __init__(self, status, payload=None, text=""):
            self.status_code, self._payload, self.text = status, payload, text

        def json(self):
            return self._payload

    def post(self, url, data=None, auth=None, headers=None, timeout=None, **kw):
        self.calls.append({"url": url, "data": dict(data or {}), "auth": auth, "timeout": timeout})
        r = self.refresh_response
        return self._Resp(r["status"], r.get("json"), r.get("text", ""))

    def refreshes(self):
        return [c for c in self.calls if "connect/token" in c["url"]]


@pytest.fixture
def xero(monkeypatch):
    fake = XeroIdentity()
    import requests

    monkeypatch.setattr(requests, "post", fake.post)
    return fake


def service_jwt(entity_id, *, scope="xero-access-token", minutes=5, secret=None):
    payload = {"entity_id": str(entity_id), "scope": scope,
               "exp": datetime.now(UTC) + timedelta(minutes=minutes)}
    return jwt.encode(payload, secret or settings.SECRET_KEY, algorithm="HS256")


def internal_token(entity_id, **kw):
    return Client().post(URL, HTTP_AUTHORIZATION="Bearer " + service_jwt(entity_id, **kw))


# ---- Flask's five ----------------------------------------------------------------------------


def test_service_gets_the_connectors_live_token(connected, xero):
    owner, entity = connected
    resp = internal_token(entity.id)
    assert resp.status_code == 200, resp.content[:300]
    assert resp.json() == {"access_token": "access-1", "xero_org_id": TENANT}
    assert xero.refreshes() == [], "a fresh token is handed out without touching Xero"


def test_expired_token_is_refreshed_once_and_the_new_one_is_reused(connected, xero):
    owner, entity = connected
    age_token(owner, 3600)  # obtained an hour ago, 1800 s lifetime

    first = internal_token(entity.id)
    assert first.status_code == 200, first.content[:300]
    assert first.json()["access_token"] == "access-2"
    assert len(xero.refreshes()) == 1
    call = xero.refreshes()[0]
    assert call["data"] == {"grant_type": "refresh_token", "refresh_token": "refresh-1"}
    assert call["auth"] == ("test-client-id", "test-client-secret")
    assert call["timeout"] == (5, 10)

    second = internal_token(entity.id)
    assert second.json()["access_token"] == "access-2"
    assert len(xero.refreshes()) == 1, "the refreshed token is stored, not refreshed again"

    row = UserToken.objects.get(user_id=owner.id)
    assert (row.refresh_token, row.id_token, row.access_token_expires_in) == ("refresh-2", "id-2", 1800)
    assert row.refresh_token_last_used_at is not None
    assert row.refresh_token_last_used_at == row.access_token_obtained_at


def test_failed_refresh_answers_reconnect_required_and_keeps_the_old_refresh_token(connected, xero):
    owner, entity = connected
    age_token(owner, 3600)
    xero.refresh_response = {"status": 400, "json": {"error": "invalid_grant"}, "text": "invalid_grant"}

    resp = internal_token(entity.id)
    assert resp.status_code == 409, resp.content[:300]
    assert resp.json()["status"] == "reconnect_required"

    # nothing was overwritten: when Xero recovers, the SAME refresh token is presented
    xero.refresh_response = {"status": 200, "json": {
        "access_token": "access-3", "refresh_token": "refresh-3", "id_token": "id-3", "expires_in": 1800}}
    resp = internal_token(entity.id)
    assert resp.status_code == 200, resp.content[:300]
    assert xero.refreshes()[-1]["data"]["refresh_token"] == "refresh-1"


def test_entity_nobody_connected_answers_reconnect_required(db, xero):
    entity = make_entity()
    resp = internal_token(entity.id)
    assert resp.status_code == 409
    assert resp.json()["status"] == "reconnect_required"


def test_service_endpoint_rejects_bad_tokens(connected):
    owner, entity = connected
    assert Client().post(URL).status_code == 401
    assert internal_token(entity.id, scope="onboarding").status_code == 401
    assert internal_token(entity.id, minutes=-5).status_code == 401  # past the 60 s skew
    assert internal_token(entity.id, secret="not-the-secret").status_code == 401


# ---- the rest of the contract ----------------------------------------------------------------


def test_the_answers_are_flasks_bytes(connected, xero):
    owner, entity = connected
    ok = internal_token(entity.id)
    assert ok.content == b'{"access_token":"access-1","xero_org_id":"' + TENANT.encode() + b'"}\n'
    assert ok["Content-Type"] == "application/json"

    nobody = internal_token(make_entity().id)
    assert nobody.content == b'{"message":"No valid Xero token for this entity","status":"reconnect_required"}\n'

    refused = Client().post(URL)
    assert refused.content == b'{"message":"unauthorized","status":"error"}\n'


def test_the_company_comes_from_the_claims_not_the_body(connected, xero):
    owner, entity = connected
    other = make_entity()  # not connected: 409 if the body were read
    resp = Client().post(URL, data={"entity_id": str(other.id)}, content_type="application/json",
                         HTTP_AUTHORIZATION="Bearer " + service_jwt(entity.id))
    assert resp.status_code == 200


def test_a_connector_with_no_bundle_or_a_missing_xero_org_answers_reconnect_required(db, xero):
    owner = make_user()
    assert internal_token(make_entity(connected_by=owner).id).status_code == 409  # no user_token
    assert internal_token(make_entity(connected_by=owner, xero_org_id=None).id).status_code == 409
    assert xero.refreshes() == []


def test_an_entity_id_that_is_not_a_uuid_answers_reconnect_required(db):
    assert internal_token("not-a-uuid").status_code == 409


def test_only_post(connected):
    assert Client().get(URL).status_code == 405


def test_the_path_is_routed_here():
    from django.urls import resolve

    assert resolve(URL).url_name == "internal_xero_access_token"


# ---- the refresh lock: Flask's key, held by another process ----------------------------------


@pytest.fixture
def other_process():
    """A second database session, as Flask's service path would be."""
    params = connection.get_connection_params()
    params.pop("cursor_factory", None)
    params.pop("context", None)
    conn = psycopg.connect(**params, autocommit=True)
    yield conn
    conn.close()


def hold_lock(conn, user_id):
    # Flask's statement: pg_try_advisory_lock(:ns, hashtext('xero_refresh:' || user id))
    assert conn.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s))",
                        [1481594447, f"xero_refresh:{user_id}"]).fetchone()[0]


def test_a_stale_token_is_not_refreshed_while_another_refresh_holds_the_lock(
        connected, xero, other_process, monkeypatch):
    owner, entity = connected
    age_token(owner, 3600)
    monkeypatch.setattr(tokens, "LOCK_WAIT_SECONDS", 0.3)
    hold_lock(other_process, owner.id)

    resp = internal_token(entity.id)
    assert resp.status_code == 409
    assert xero.refreshes() == [], "refreshing now would spend the token the other refresh is spending"


def test_a_fresh_token_is_still_handed_out_while_the_lock_is_held(connected, xero, other_process,
                                                                 monkeypatch):
    owner, entity = connected
    monkeypatch.setattr(tokens, "LOCK_WAIT_SECONDS", 0.3)
    hold_lock(other_process, owner.id)
    resp = internal_token(entity.id)
    assert resp.status_code == 200
    assert resp.json()["access_token"] == "access-1"


def test_the_lock_is_released_after_a_request(connected, xero, other_process):
    owner, entity = connected
    assert internal_token(entity.id).status_code == 200
    hold_lock(other_process, owner.id)  # would fail if this service still held it
