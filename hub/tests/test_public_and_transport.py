"""Phase 5A: the public routes (Flask's ``tests/test_terms_gate.py`` and ``test_char_access.py``
expectations for them, restated) and the hub's door (``hub/transport.py``: Flask's
``hub_api.guard`` and CORS)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from django.conf import settings
from django.http import HttpResponse
from django.test import Client, RequestFactory
from itsdangerous import URLSafeTimedSerializer
from minty_db.models import Entity, Invitation, TermsConsent, User

from hub import registry
from hub.transport import cors_origin, guard

WEB = "http://localhost:3000"


def make_user(email="owner@test.com", *, approved=True, xero_email=None) -> User:
    return User.objects.create(id=uuid.uuid4(), email=email, username=email, password="x", first_name="Olive",
                               last_name="Owner", approved=approved, xero_email=xero_email)


def token(user_id, **claims) -> str:
    payload = {"user_id": str(user_id), "exp": datetime.now(UTC) + timedelta(minutes=5), **claims}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm="HS256")


def signed(data, salt: str, secret=None) -> str:
    return URLSafeTimedSerializer(secret or settings.SECRET_KEY, salt=salt).dumps(data)


# ---- GET /auth/notices -----------------------------------------------------------------------


def test_notices_read_the_signed_hand_over_with_flasks_categories(db):
    flash = signed([{"category": "danger", "message": "That link expired."},
                    {"category": "success", "message": "Signed out."},
                    {"category": "info", "message": "  "}], "hub-flash")
    resp = Client().get("/auth/notices", {"flash": flash}, HTTP_ORIGIN=WEB)
    assert resp.content == (b'{"notices":[{"category":"error","message":"That link expired."},'
                            b'{"category":"success","message":"Signed out."}]}\n')
    assert resp["Access-Control-Allow-Origin"] == WEB


@pytest.mark.parametrize("flash", [None, "garbage", signed([{"message": "x"}], "hub-flash", secret="other-key"),
                                   signed([{"message": "x"}], "hub-xero-conflict")])
def test_no_forged_or_misused_hand_over_shows_nothing(db, flash):
    resp = Client().get("/auth/notices", {"flash": flash} if flash else {})
    assert resp.json() == {"notices": []}


# ---- GET /legal/current ----------------------------------------------------------------------


def test_the_live_versions_come_from_the_manifest(db):
    entry = registry.get_current(registry.TERMS)
    assert Client().get("/legal/current").json() == {
        "terms_version": registry.current_version(registry.TERMS),
        "privacy_version": registry.current_version(registry.PRIVACY),
        "effective_date": entry.effective_date if entry else None,
    }


# ---- POST /legal/invite-terms-status ---------------------------------------------------------


@pytest.fixture
def invited(db):
    owner = make_user()
    entity = Entity.objects.create(id=uuid.uuid4(), name="Acme", status="onboarding")
    invitation = Invitation.objects.create(id=uuid.uuid4(), entity_id=entity.id, email="New@Test.com", role="cashier",
                                           token="secret-token", status="pending", invited_by=owner.id)
    return invitation


def _status(body):
    return Client().post("/legal/invite-terms-status", body, content_type="application/json").json()


def test_an_invitee_who_agreed_to_this_version_is_not_asked_again(invited):
    person = make_user("new@test.com")
    TermsConsent.objects.create(user_id=person.id, terms_version=registry.current_version(registry.TERMS),
                                document_hash="h", source="hub")
    assert _status({"invite": "secret-token"}) == {"terms_required": False,
                                                   "terms_version": registry.current_version(registry.TERMS)}


def test_an_xero_email_counts_as_the_same_person(invited):
    person = make_user("someone@else.com", xero_email="NEW@test.com")
    TermsConsent.objects.create(user_id=person.id, terms_version=registry.current_version(registry.TERMS),
                                document_hash="h", source="hub")
    assert _status({"invite": "secret-token"})["terms_required"] is False


@pytest.mark.parametrize("body", [{}, {"invite": "unknown"}, ["x"], {"invite": "secret-token"}])
def test_anything_unclear_fails_safe_to_required(invited, body):
    make_user("new@test.com")  # exists, but never agreed
    assert _status(body)["terms_required"] is True


def test_an_accepted_invitation_answers_required(invited):
    Invitation.objects.filter(id=invited.id).update(status="accepted")
    assert _status({"invite": "secret-token"})["terms_required"] is True


# ---- the hub's door --------------------------------------------------------------------------


def _request(method="GET", origin=None, auth=None):
    headers = {}
    if origin:
        headers["HTTP_ORIGIN"] = origin
    if auth:
        headers["HTTP_AUTHORIZATION"] = auth
    return RequestFactory().generic(method, "/api/me/profile", **headers)


def test_the_door_lets_in_an_approved_person_with_any_scope(db):
    person = make_user()
    for claims in ({}, {"scope": "onboarding"}, {"module": "billing", "entity_id": str(uuid.uuid4())}):
        early, user = guard(_request(auth=f"Bearer {token(person.id, **claims)}"))
        assert early is None and user.id == person.id


@pytest.mark.parametrize("case", ["none", "bad", "expired", "unapproved", "gone", "not-uuid"])
def test_the_door_refuses_with_a_401_the_browser_can_read(db, case):
    person = make_user(approved=case != "unapproved")
    auth = {
        "none": None, "bad": "Bearer nope", "expired": f"Bearer {token(person.id, exp=datetime.now(UTC) - timedelta(minutes=5))}",
        "unapproved": f"Bearer {token(person.id)}", "gone": f"Bearer {token(uuid.uuid4())}",
        "not-uuid": f"Bearer {token('42')}",
    }[case]
    early, user = guard(_request(origin=WEB, auth=auth))
    assert user is None and early.status_code == 401
    assert early.content == b'{"error":"unauthorized"}\n'
    assert early["Access-Control-Allow-Origin"] == WEB and early["Vary"] == "Origin"


def test_a_preflight_is_204_with_the_hubs_methods(db):
    early, _ = guard(_request("OPTIONS", origin="http://localhost:3020"))
    assert early.status_code == 204
    assert (early["Access-Control-Allow-Origin"], early["Access-Control-Allow-Methods"],
            early["Access-Control-Allow-Headers"]) == ("http://localhost:3020", "GET, POST, PATCH, DELETE, OPTIONS",
                                                      "Authorization, Content-Type")


def test_cors_echoes_only_the_front_ends_and_their_sets(settings):
    import re

    configured = ("http://localhost:3000", "http://localhost:3020", "http://localhost:3010")
    apps = ("my", "payment-request", "pettycash")
    settings.PREVIEW_ORIGIN = None
    settings.EXTRA_FRONTEND_ORIGINS = {"my": "https://my.example.workers.dev"}
    ask = lambda origin: cors_origin(_request(origin=origin), configured, apps)  # noqa: E731
    assert ask("http://localhost:3010") == "http://localhost:3010"
    assert ask("https://my.example.workers.dev") == "https://my.example.workers.dev"
    assert ask("https://evil.test") == "http://localhost:3000"
    assert ask("https://pr-feat-my.dailyminty.com") == "http://localhost:3000"  # no preview regex set

    settings.PREVIEW_ORIGIN = re.compile(r"^https://pr-[a-z0-9-]+-(my|payment-request)\.dailyminty\.com$")
    assert ask("https://pr-feat-my.dailyminty.com") == "https://pr-feat-my.dailyminty.com"
    assert ask("https://pr-feat-onboarding.dailyminty.com") == "http://localhost:3000"  # not a hub app
    assert ask("https://pr-feat-pettycash.dailyminty.com") == "http://localhost:3000"  # not in the regex


def test_hub_responses_are_flasks_bytes(db):
    from hub.transport import respond

    resp = respond(_request(origin=WEB), {"b": 1, "a": [True, None]})
    assert isinstance(resp, HttpResponse) and resp.content == b'{"a":[true,null],"b":1}\n'
