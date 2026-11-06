"""The browser session and its helpers (``hub/session.py``, ``core/hardening.py``,
``hub/notices.sign_*``) - Flask's ``test_url_security.py`` redirect rule and the sign-in hooks'
behaviour, restated (Phase 6)."""

from __future__ import annotations

from datetime import timedelta
from importlib import import_module

import pytest
from django.conf import settings
from django.test import Client, RequestFactory
from django.utils import timezone
from minty_db.models import User

from core.hardening import loggable_path
from hub import notices, session
from hub.tests.test_public_and_transport import make_user


def request_with_session(store=None):
    request = RequestFactory().get("/")
    engine = import_module(settings.SESSION_ENGINE)
    request.session = store or engine.SessionStore()
    return request


@pytest.mark.parametrize("candidate, expected", [
    ("/entity/abc?x=1", "/entity/abc?x=1"), ("//evil.com", None), ("/\\evil.com", None),
    ("/\t/evil.com", None), ("/\n/evil.com", None), ("/\r/evil.com", None), ("https://evil.com", None),
    ("javascript:alert(1)", None), ("evil.com", None), ("/a/../b", None), ("", None), (None, None),
])
def test_safe_internal_path_keeps_only_paths_on_this_site(candidate, expected):
    assert session.safe_internal_path(candidate) == expected


def test_signing_in_starts_a_new_session_with_a_fresh_sign_in_id_and_stamps_presence(db):
    me = make_user()
    request = request_with_session()
    request.session["planted"] = "by someone else"
    request.session.save()
    before = request.session.session_key
    session.sign_in(request, me)
    assert request.session.session_key != before  # no fixation
    assert request.session[session.USER_KEY] == str(me.id) and len(session.login_sid(request)) == 32
    me.refresh_from_db()
    assert me.signed_in_at is not None and me.last_seen_at == me.signed_in_at
    assert session.signed_in_user(request) == me


def test_a_cookie_naming_a_switched_off_or_missing_person_is_emptied(db):
    me = make_user()
    request = request_with_session()
    session.sign_in(request, me)
    User.objects.filter(id=me.id).update(approved=False)
    assert session.signed_in_user(request) is None and session.USER_KEY not in request.session
    request.session[session.USER_KEY] = "00000000-0000-0000-0000-000000000000"
    assert session.signed_in_user(request) is None


def test_signing_out_ends_the_session_and_takes_the_person_off_the_list(db):
    me = make_user()
    request = request_with_session()
    session.sign_in(request, me)
    session.sign_out(request)
    me.refresh_from_db()
    assert me.signed_in_at is None and me.last_seen_at is not None
    assert session.signed_in_user(request) is None


def test_presence_is_refreshed_at_most_once_a_minute_and_never_revives_a_sign_out(db):
    me = make_user()
    request = request_with_session()
    session.sign_in(request, me)
    User.objects.filter(id=me.id).update(signed_in_at=None, last_seen_at=timezone.now() - timedelta(hours=1))
    session.refresh_presence(request, me)  # throttled: signed in seconds ago
    me.refresh_from_db()
    assert me.last_seen_at < timezone.now() - timedelta(minutes=59)
    request.session[session.PRESENCE_KEY] = 0
    session.refresh_presence(request, me)
    me.refresh_from_db()
    assert me.signed_in_at is None and me.last_seen_at > timezone.now() - timedelta(minutes=1)
    User.objects.filter(id=me.id).update(signed_in_at=None, last_seen_at=None)  # never stamped: adopted
    request.session[session.PRESENCE_KEY] = 0
    session.refresh_presence(request, me)
    me.refresh_from_db()
    assert me.signed_in_at is not None


def test_opening_a_company_puts_them_back_keeping_their_sign_in_time(db):
    me = make_user()
    then = timezone.now() - timedelta(hours=2)
    User.objects.filter(id=me.id).update(signed_in_at=then)
    session.resume_presence(me)
    me.refresh_from_db()
    assert me.signed_in_at == then
    User.objects.filter(id=me.id).update(signed_in_at=None)
    session.resume_presence(me)
    me.refresh_from_db()
    assert me.signed_in_at is not None


def test_flashes_wait_in_the_session_until_drained_and_sign_into_what_the_hub_reads(db):
    request = request_with_session()
    session.flash(request, "You're connected to Xero!", "success")
    session.flash(request, "Hmm.", "danger")
    drained = session.drain_flashes(request)
    assert drained == [("success", "You're connected to Xero!"), ("danger", "Hmm.")]
    assert session.drain_flashes(request) == []
    assert notices.read_notices(notices.sign_notices(drained)) == [
        {"category": "success", "message": "You're connected to Xero!"}, {"category": "error", "message": "Hmm."}]
    assert notices.sign_notices([("info", "  ")]) is None
    assert notices.read_conflict(notices.sign_conflict("e-1", "Other Co", True)) == {
        "entity_id": "e-1", "entity_name": "Other Co", "can_move": True}


def test_every_django_answer_carries_flasks_security_headers(db):
    resp = Client().get("/health")
    assert (resp["Referrer-Policy"], resp["X-Content-Type-Options"], resp["X-Frame-Options"]) == (
        "same-origin", "nosniff", "SAMEORIGIN")


def test_hsts_outside_development_only(db, settings):
    from django.http import HttpResponse

    from core.hardening import HardeningMiddleware

    settings.DEVELOPMENT = False
    assert HardeningMiddleware(lambda r: HttpResponse())(RequestFactory().get("/x"))[
        "Strict-Transport-Security"] == "max-age=31536000"
    settings.DEVELOPMENT = True
    assert "Strict-Transport-Security" not in HardeningMiddleware(lambda r: HttpResponse())(RequestFactory().get("/x"))


def test_a_secret_path_is_not_cached_and_never_logged():
    from django.http import HttpResponse

    from core.hardening import HardeningMiddleware

    resp = HardeningMiddleware(lambda r: HttpResponse())(RequestFactory().get("/invitation/accept/s3cret"))
    assert resp["Cache-Control"] == "no-store"
    assert loggable_path("/invitation/accept/s3cret") == "/invitation/accept/[redacted]"
    assert loggable_path("/api/me/entities") == "/api/me/entities"


def test_the_session_cookie_keeps_flasks_name_lifetime_and_flags():
    assert (settings.SESSION_COOKIE_NAME, settings.SESSION_COOKIE_AGE, settings.SESSION_COOKIE_HTTPONLY,
            settings.SESSION_COOKIE_SAMESITE) == ("session", 86400, True, "Lax")
