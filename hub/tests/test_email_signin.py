"""Signing in by code and the front door (``hub/email_signin.py``, ``hub/front_door.py``) - Flask's
``test_hub_sign_in.py``, ``test_otp_identity_gate.py``, ``test_terms_signup_consent.py`` and the
code rules of ``email_auth.py``, restated; plus the single-use hand-off (gap 1, 2026-11-06). The
code is read from the email in Django's outbox."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.contrib.sessions.models import Session
from django.core import mail
from django.test import Client
from minty_db.models import EmailOtp, Entity, Invitation, TermsConsent, User, UserEntity

from hub import email_signin, invitations, registry
from hub.tests.test_public_and_transport import make_user

HUB = "http://localhost:3000"
LIVE = registry.current_version(registry.TERMS)


@pytest.fixture(autouse=True)
def _fresh():
    invitations.reset_cooldowns()


def ask(client, email, **extra):
    return client.post("/auth/email/request-code", {"email": email, **extra}, content_type="application/json")


def last_code():
    return re.search(r">(\d{6})<", mail.outbox[-1].body).group(1)


def verify(client, email, code=None, **extra):
    return client.post("/auth/email/verify-code", {"email": email, "code": code or last_code(), **extra},
                       content_type="application/json")


def follow_handoff(client, verified):
    url = urlsplit(verified.json()["redirect_url"])
    return client.get(f"{url.path}?{url.query}")


def sign_in_by_code(client, email, **extra):
    assert ask(client, email, mode="login").status_code == 200
    verified = verify(client, email, **extra)
    assert verified.status_code == 200, verified.json()
    return verified, follow_handoff(client, verified)


def sign_in_page(resp, path="/login"):
    location = urlsplit(resp["Location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == HUB + path, resp["Location"]
    return parse_qs(location.query)


# ---- the front door -----------------------------------------------------------------------------


def test_the_front_door_is_the_hubs_sign_in_page_with_next(db):
    assert sign_in_page(Client().get("/?next=/profile")) == {"next": ["/profile"]}


def test_a_next_that_leaves_the_site_is_dropped(db):
    assert sign_in_page(Client().get("/?next=//evil.example/x")) == {}


def test_signed_in_the_front_door_is_the_entity_list(db):
    client = Client()
    make_user("in@test.com")
    sign_in_by_code(client, "in@test.com")
    assert client.get("/")["Location"] == "/entity"
    assert client.get("/index")["Location"] == "/entity"


def test_a_protected_page_sends_its_message_and_its_address_along(db):
    client = Client()
    door = client.get("/index?x=1")
    assert door["Location"] == "/?next=%2Findex%3Fx%3D1"
    query = sign_in_page(client.get(door["Location"]))
    assert query["next"] == ["/index?x=1"]
    notices = client.get("/auth/notices", {"flash": query["flash"][0]}).json()["notices"]
    assert notices == [{"category": "info", "message": "Please log in to access this page."}]


def test_register_forwards_to_the_hubs_sign_up(db):
    assert sign_in_page(Client().get("/register"), "/signup") == {}
    assert not User.objects.exists()


# ---- asking for a code --------------------------------------------------------------------------


def test_a_code_is_sent_from_invite_from_and_stored_hashed(db, settings):
    make_user("known@test.com")
    resp = ask(Client(), "Known@Test.com", mode="login")
    assert (resp.status_code, resp.json()) == (200, {"status": "success", "message": "A code has been sent to your email."})
    sent, = mail.outbox
    assert (sent.to, sent.from_email, sent.subject) == (["known@test.com"], settings.INVITE_FROM, "Your Minty sign-in code")
    row = EmailOtp.objects.get(email="known@test.com")
    assert row.code_hash.startswith("pbkdf2:sha256:") and last_code() not in row.code_hash
    assert email_signin.check_code(row.code_hash, last_code())


def test_a_flask_issued_code_verifies_here():
    # werkzeug.security.generate_password_hash("123456", method="pbkdf2:sha256") with salt "abcdefghijklmnop"
    stored = ("pbkdf2:sha256:600000$abcdefghijklmnop$"
              + __import__("hashlib").pbkdf2_hmac("sha256", b"123456", b"abcdefghijklmnop", 600000).hex())
    assert email_signin.check_code(stored, "123456") and not email_signin.check_code(stored, "654321")


@pytest.mark.parametrize("column", ["email", "xero_email"])
def test_login_mode_sends_a_code_to_either_identity_and_refuses_an_unknown_address(db, column):
    person = make_user("someone@other.test")
    User.objects.filter(id=person.id).update(**{column: "Owner@Test.com"})
    assert ask(Client(), "owner@test.com", mode="login").status_code == 200
    resp = ask(Client(), "stranger@test.com", mode="login")
    assert (resp.status_code, resp.json()["message"]) == (404, "Please sign up first")
    assert ask(Client(), "stranger@test.com").status_code == 200  # sign-up mode is open


def test_a_username_alone_does_not_open_login_mode(db):
    person = make_user("x@other.test")
    User.objects.filter(id=person.id).update(username="handle@test.com")
    assert ask(Client(), "handle@test.com", mode="login").status_code == 404


def test_a_non_english_address_is_refused_before_anything(db):
    resp = ask(Client(), "홍길동@example.com", mode="login")
    assert resp.status_code == 400 and "English" in resp.json()["message"] and mail.outbox == []


def test_a_resend_waits_out_the_cooldown(db):
    make_user()
    client = Client()
    ask(client, "owner@test.com")
    resp = ask(client, "owner@test.com")
    assert (resp.status_code, resp.json()["message"]) == (400, "Please wait a moment before requesting another code.")


def test_a_failed_send_leaves_nothing_behind_so_a_retry_works_at_once(db, monkeypatch):
    monkeypatch.setattr("django.core.mail.EmailMessage.send", lambda self, *a, **k: (_ for _ in ()).throw(OSError()))
    resp = ask(Client(), "new@test.com")
    assert resp.status_code == 400 and "couldn't send" in resp.json()["message"]
    assert not EmailOtp.objects.filter(email="new@test.com").exists()


def test_the_answers_are_open_cors_for_the_sign_in_page(db):
    resp = Client().options("/auth/email/request-code", HTTP_ORIGIN=HUB, HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type")
    assert (resp.status_code, resp["Access-Control-Allow-Origin"], resp["Access-Control-Allow-Headers"]) == (
        200, HUB, "content-type")
    assert Client().get("/auth/email/request-code").status_code == 405


# ---- checking it --------------------------------------------------------------------------------


def test_wrong_codes_lock_the_address_for_15_minutes_across_resends(db):
    make_user()
    client = Client()
    ask(client, "owner@test.com")
    for _ in range(email_signin.MAX_OTP_ATTEMPTS - 1):
        assert verify(client, "owner@test.com", "000000").json()["message"] == "Invalid code."
    locked = verify(client, "owner@test.com", "000000")
    assert locked.status_code == 429 and "Too many attempts" in locked.json()["message"]
    EmailOtp.objects.update(created_at=datetime.now(UTC) - timedelta(minutes=2))  # past the cooldown, still locked
    refused = ask(client, "owner@test.com")
    assert refused.status_code == 400 and "Too many attempts" in refused.json()["message"]


def test_failures_carry_across_a_resend(db):
    make_user()
    client = Client()
    ask(client, "owner@test.com")
    verify(client, "owner@test.com", "000000")
    EmailOtp.objects.update(created_at=datetime.now(UTC) - timedelta(minutes=2))
    ask(client, "owner@test.com")
    assert EmailOtp.objects.get(email="owner@test.com").attempts == 1


def test_an_expired_or_used_code_is_refused(db):
    make_user()
    client = Client()
    ask(client, "owner@test.com")
    code = last_code()
    assert verify(client, "owner@test.com", code).status_code == 200
    again = verify(client, "owner@test.com", code)
    assert again.json()["message"] == "This code has already been used. Please request a new one."
    EmailOtp.objects.update(verified_at=None, expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert verify(client, "owner@test.com", code).json()["message"] == "This code has expired. Please request a new one."


def test_an_unapproved_account_is_held(db):
    make_user(approved=False)
    client = Client()
    ask(client, "owner@test.com")
    resp = verify(client, "owner@test.com")
    assert resp.status_code == 403 and "approved" in resp.json()["message"]


def test_a_new_address_with_no_name_is_told_to_sign_up(db):
    client = Client()
    ask(client, "new@test.com")
    resp = verify(client, "new@test.com")
    assert (resp.status_code, resp.json()["message"]) == (404, "No account found for this email — please sign up.")


def test_signing_up_makes_the_account_with_its_terms_agreement(db):
    client = Client()
    ask(client, "new@test.com")
    verified = verify(client, "new@test.com", first_name="Nia", last_name="New", terms_accepted=True,
                      terms_version=LIVE)
    assert verified.status_code == 200
    user = User.objects.get(email="new@test.com")
    assert (user.username, user.first_name, user.approved, str(user.system_role)) == ("new@test.com", "Nia", True, "normal")
    consent, = TermsConsent.objects.filter(user_id=user.id)
    assert (consent.terms_version, consent.source) == (LIVE, "signup_otp")
    landed = follow_handoff(client, verified)
    assert landed["Location"] == "/index"


def test_a_stale_terms_version_is_refused_when_terms_are_required(db, monkeypatch):
    monkeypatch.setattr(registry, "REQUIRE_TERMS_AT_SIGNUP", True)
    client = Client()
    ask(client, "new@test.com")
    resp = verify(client, "new@test.com", first_name="N", last_name="N", terms_accepted=True, terms_version="old")
    assert resp.status_code == 400 and "updated while you were signing up" in resp.json()["message"]
    assert not User.objects.filter(email="new@test.com").exists()


# ---- the hand-off -------------------------------------------------------------------------------


def test_a_code_sign_in_returns_to_where_it_was_headed_with_a_session_cookie(db):
    client = Client()
    me = make_user("headed@test.com")
    _verified, landed = sign_in_by_code(client, "headed@test.com", next="/profile")
    assert (landed.status_code, landed["Location"]) == (302, "/profile")
    cookie = landed.cookies["session"]
    assert cookie["httponly"] and cookie["samesite"] == "Lax"
    stored = Session.objects.get(session_key=cookie.value).get_decoded()
    assert stored["user_id"] == str(me.id) and stored["login_sid"]


def test_a_code_sign_in_never_follows_an_outside_next(db):
    make_user("careful@test.com")
    _verified, landed = sign_in_by_code(Client(), "careful@test.com", next="//evil.example/x")
    assert landed["Location"] == "/index"


def test_the_hand_off_works_once(db):
    """Gap 1: a replayed, copied or second-tab hand-off signs nobody in."""
    make_user()
    client = Client()
    verified, landed = sign_in_by_code(client, "owner@test.com")
    assert landed["Location"] == "/index"
    other = Client()
    replay = follow_handoff(other, verified)
    assert replay["Location"] == "/" and "session" not in replay.cookies
    assert other.get("/")["Location"].startswith(HUB + "/login")


@pytest.mark.parametrize("token", ["", "not-a-token", "x" * 80])
def test_a_missing_or_forged_hand_off_goes_back_to_the_front_door(db, token):
    resp = Client().get("/auth/email/handoff", {"h": token})
    assert resp["Location"] == "/" and "session" not in resp.cookies


def test_an_expired_hand_off_is_refused(db, monkeypatch):
    make_user()
    client = Client()
    ask(client, "owner@test.com")
    verified = verify(client, "owner@test.com")
    monkeypatch.setattr(email_signin, "HANDOFF_MAX_AGE", -1)
    assert follow_handoff(client, verified)["Location"] == "/"


def test_the_hand_off_url_is_on_the_public_address(db, settings):
    make_user()
    client = Client()
    ask(client, "owner@test.com")
    assert verify(client, "owner@test.com").json()["redirect_url"].startswith(
        settings.ACCOUNTS_API_URL + "/auth/email/handoff?h=")


# ---- invitations --------------------------------------------------------------------------------


@pytest.fixture
def invited(db):
    owner = make_user("boss@test.com")
    company = Entity.objects.create(id=uuid.uuid4(), name="Olive & Vine", status="connected")
    UserEntity.objects.create(user_id=owner.id, entity_id=company.id, role="admin", approved=True)
    invitation, error = invitations.create_invitation(company.id, "invitee@test.com", "cashier", owner.id, "Ivy", "Lee")
    assert error is None
    return company, invitation


def test_an_invitation_link_opens_the_hubs_sign_in_for_that_address(invited):
    _company, invitation = invited
    assert sign_in_page(Client().get(f"/invitation/accept/{invitation.token}?fn=Ivy&ln=Lee")) == {
        "invite": [invitation.token], "email": ["invitee@test.com"], "fn": ["Ivy"], "ln": ["Lee"]}


def test_a_used_invitation_says_so_on_the_sign_in_page(db):
    client = Client()
    first = client.get("/invitation/accept/not-a-real-token")
    assert first["Location"] == "/" and first["Cache-Control"] == "no-store"
    query = sign_in_page(client.get("/"))
    notice, = client.get("/auth/notices", {"flash": query["flash"][0]}).json()["notices"]
    assert notice["category"] == "warning" and "doesn't work anymore" in notice["message"]


def test_an_invitee_signs_up_by_code_and_lands_in_the_company(invited):
    company, invitation = invited
    client = Client()
    assert ask(client, "invitee@test.com", invite=invitation.token).status_code == 200
    verified = verify(client, "invitee@test.com", invite=invitation.token, first_name="Ivy", last_name="Lee",
                      terms_accepted=True, terms_version=LIVE)
    assert verified.status_code == 200, verified.json()
    landed = follow_handoff(client, verified)
    assert landed["Location"] == f"/entity/{str(company.id)[:8]}/olive-and-vine/petty-cash"
    user = User.objects.get(email="invitee@test.com")
    member = UserEntity.objects.get(user_id=user.id, entity_id=company.id)
    assert (str(member.role), member.approved) == ("cashier", True)
    assert str(Invitation.objects.get(id=invitation.id).status) == "accepted"
    assert TermsConsent.objects.get(user_id=user.id).source == "signup_invite"


def test_an_invitation_for_another_address_is_refused_before_a_code_goes_out(invited):
    _company, invitation = invited
    make_user("someone@test.com")
    resp = ask(Client(), "someone@test.com", invite=invitation.token)
    assert resp.status_code == 400 and "different account" in resp.json()["message"] and mail.outbox[1:] == []


def test_someone_else_signed_in_is_signed_out_before_the_invitation(invited):
    _company, invitation = invited
    client = Client()
    make_user("other@test.com")
    sign_in_by_code(client, "other@test.com")
    query = sign_in_page(client.get(f"/invitation/accept/{invitation.token}"))
    assert query["invite"] == [invitation.token]
    assert client.get("/")["Location"].startswith(HUB + "/login")  # signed out
