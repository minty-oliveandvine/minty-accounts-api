"""My Profile, the Terms gate and Talk to Minty (``hub/profile.py``, ``terms.py``, ``contact.py``) -
Flask's ``tests/test_hub_profile.py``, ``test_hub_terms.py`` and ``test_hub_contact.py``,
restated (their session-route and CSRF tests stay Flask's). Mail goes to Django's in-memory outbox."""

from __future__ import annotations

import uuid

import pytest
from django.core import mail
from django.test import Client
from django.utils import timezone
from minty_db.models import Entity, TermsConsent, User, UserEntity

from hub import contact, registry
from hub.tests.test_public_and_transport import make_user, token

WEB = "http://localhost:3000"


def call(method, path, person, body=None, **query):
    kwargs = {"HTTP_ORIGIN": WEB}
    if person is not None:
        kwargs["HTTP_AUTHORIZATION"] = f"Bearer {token(person.id)}"
    if body is not None:
        kwargs["content_type"] = "application/json"
        return getattr(Client(), method)(path + (f"?{_qs(query)}" if query else ""), body, **kwargs)
    return getattr(Client(), method)(path, query, **kwargs)


def _qs(query):
    from urllib.parse import urlencode

    return urlencode(query)


def acme(member=None, role="admin", approved=True):
    entity = Entity.objects.create(id=uuid.uuid4(), name="Acme Shop", status="connected")
    if member is not None:
        UserEntity.objects.create(user_id=member.id, entity_id=entity.id, role=role, approved=approved)
    return entity


# ---- My Profile ------------------------------------------------------------------------------


def test_the_person_alone_when_opened_from_the_entity_list(db):
    me = make_user()
    resp = call("get", "/api/me/profile", me)
    assert resp.content.startswith(b'{"entity":null,"user":{')
    assert resp.json()["user"] == {"id": str(me.id), "first_name": "Olive", "last_name": "Owner",
                                   "name": "Olive Owner", "initials": "OO", "email": "owner@test.com",
                                   "can_view_subscriptions": False}


def _pays_for(person, entity):
    from minty_db.models import EntityModuleSubscription

    EntityModuleSubscription.objects.create(id=uuid.uuid4(), entity_id=entity.id, function_code="PETTY_CASH",
                                            payer_user_id=person.id, phase="active")


@pytest.mark.parametrize(("role", "approved", "pays", "expected"), [
    ("accountant", True, False, True),
    ("admin", True, False, True),
    ("super_admin", True, False, True),
    ("shop_manager", True, False, False),
    ("cashier", True, False, False),
    ("cashier", True, True, True),
    ("accountant", False, False, False),
    (None, True, True, True),
])
def test_manage_subscriptions_is_for_admins_accountants_and_subscribers(db, role, approved, pays, expected):
    """The sidebar's Manage subscriptions item (owner, 2026-11-12): every admin and accountant
    sees it, and every subscriber; nobody else."""
    me = make_user()
    entity = acme(me, role=role, approved=approved) if role else acme()
    if pays:
        _pays_for(me, entity)
    assert call("get", "/api/me/profile", me).json()["user"]["can_view_subscriptions"] is expected


def test_a_designated_subscriber_is_offered_manage_subscriptions(db):
    """Owner, 2026-11-13: a company's subscriber before billing is confirmed (its creator, or
    whoever it was handed to) is offered the item even as a cashier; an archived company's not."""
    me = make_user()
    entity = acme(me, role="cashier")
    assert call("get", "/api/me/profile", me).json()["user"]["can_view_subscriptions"] is False

    Entity.objects.filter(id=entity.id).update(subscriber_user_id=me.id)
    assert call("get", "/api/me/profile", me).json()["user"]["can_view_subscriptions"] is True

    Entity.objects.filter(id=entity.id).update(archived_at=timezone.now())
    assert call("get", "/api/me/profile", me).json()["user"]["can_view_subscriptions"] is False


def test_the_company_it_was_opened_from_with_the_persons_role(db):
    me = make_user()
    entity = acme(me, role="shop_manager")
    assert call("get", "/api/me/profile", me, entity=str(entity.id)).json()["entity"] == {
        "id": str(entity.id), "name": "Acme Shop", "role": "shop_manager", "role_label": "Shop Manager", "modules": []}


@pytest.mark.parametrize("which", ["stranger", "unapproved", "not-a-uuid", "missing"])
def test_a_company_the_person_may_not_see_is_refused(db, which):
    me = make_user()
    entity = acme(make_user("other@test.com")) if which == "stranger" else acme(me, approved=False)
    target = {"not-a-uuid": "nope", "missing": str(uuid.uuid4())}.get(which, str(entity.id))
    resp = call("get", "/api/me/profile", me, entity=target)
    assert (resp.status_code, resp.json()) == (403, {"error": "You don't have access to that company."})


def test_a_superuser_sees_any_company_without_a_role(db):
    boss = make_user("boss@test.com")
    User.objects.filter(id=boss.id).update(system_role="superadmin")
    entity = acme(make_user("other@test.com"))
    assert call("get", "/api/me/profile", boss, entity=str(entity.id)).json()["entity"]["role"] is None


def test_names_are_saved_trimmed_and_the_answer_is_the_fresh_profile(db):
    me = make_user()
    resp = call("patch", "/api/me/profile", me, {"first_name": "  Iris ", "last_name": " Lee  "})
    assert resp.status_code == 200 and resp.json()["user"]["name"] == "Iris Lee"
    me.refresh_from_db()
    assert (me.first_name, me.last_name) == ("Iris", "Lee")


def test_the_username_follows_the_email_when_it_was_the_email_and_not_otherwise(db):
    me = make_user()
    call("patch", "/api/me/profile", me, {"email": "olive@new.com"})
    me.refresh_from_db()
    assert (me.email, me.username) == ("olive@new.com", "olive@new.com")
    User.objects.filter(id=me.id).update(username="olive-handle")
    call("patch", "/api/me/profile", me, {"email": "third@new.com"})
    me.refresh_from_db()
    assert (me.email, me.username) == ("third@new.com", "olive-handle")


@pytest.mark.parametrize("email, sentence", [
    ("", "I'll need an email address here."),
    ("no-at-sign", "That doesn't look like an email address."),
    ("two@@test.com", "That doesn't look like an email address."),
    ("홍길동@example.com", "Email can only contain English letters, numbers and symbols."),
    ("Other@Test.com", "That email address is already in use."),
])
def test_an_email_that_cannot_be_used_is_refused_in_words(db, email, sentence):
    me = make_user()
    make_user("other@test.com")
    resp = call("patch", "/api/me/profile", me, {"email": email})
    assert (resp.status_code, resp.json()) == (422, {"error": sentence})


def test_an_address_taken_as_somebody_elses_username_is_refused(db):
    me = make_user()
    other = make_user("other@test.com")
    User.objects.filter(id=other.id).update(username="taken@test.com")
    assert call("patch", "/api/me/profile", me, {"email": "taken@test.com"}).json() == {
        "error": "That email address is already in use."}


@pytest.mark.parametrize("body", [{}, {"phone": "1"}, {"first_name": 3}, ["x"]])
def test_nothing_it_can_save_is_a_400(db, body):
    resp = call("patch", "/api/me/profile", make_user(), body)
    assert (resp.status_code, resp.json()) == (400, {"error": "That didn't quite save. Mind trying again?"})


# ---- Terms -----------------------------------------------------------------------------------

LIVE = registry.current_version(registry.TERMS)


def test_a_first_time_reader_is_told_which_version_is_owed(db):
    entry = registry.get_current(registry.TERMS)
    assert call("get", "/api/me/terms", make_user()).json() == {
        "owed": True, "document": {"version": LIVE, "effective_date": entry.effective_date, "sha256": entry.sha256,
                                   "show_draft_notice": entry.show_draft_notice},
        "is_update": False, "previous_version": None}


def test_nothing_is_owed_once_agreed_and_an_older_agreement_reads_as_an_update(db):
    me = make_user()
    TermsConsent.objects.create(user_id=me.id, terms_version="alpha-0", document_hash="h", source="gate")
    body = call("get", "/api/me/terms", me).json()
    assert (body["is_update"], body["previous_version"]) == (True, "alpha-0")
    TermsConsent.objects.create(user_id=me.id, terms_version=LIVE, document_hash="h", source="hub")
    assert call("get", "/api/me/terms", me).json() == {"owed": False}


def test_accepting_records_a_hub_consent_with_the_manifests_fingerprint_once(db):
    me = make_user()
    sha = registry.get_current(registry.TERMS).sha256
    for _ in range(2):
        resp = call("post", "/api/me/terms/accept", me, {"accepted": True, "terms_version": LIVE, "document_hash": sha})
        assert resp.json() == {"ok": True, "terms_version": LIVE}
    row, = TermsConsent.objects.filter(user_id=me.id)
    assert (row.terms_version, row.document_hash, row.source) == (LIVE, sha, "hub")


def test_a_client_too_old_to_send_a_hash_is_accepted_and_one_with_another_is_refused(db):
    me = make_user()
    wrong = call("post", "/api/me/terms/accept", me, {"accepted": True, "terms_version": LIVE, "document_hash": "x"})
    assert (wrong.status_code, wrong.json()) == (409, {"error": "document_mismatch"})
    assert not TermsConsent.objects.filter(user_id=me.id).exists()
    assert call("post", "/api/me/terms/accept", me, {"accepted": True, "terms_version": LIVE}).status_code == 200


@pytest.mark.parametrize("body, status, answer", [
    ({"accepted": False, "terms_version": LIVE}, 400, {"error": "Please tick the box to continue."}),
    ({"accepted": "yes", "terms_version": LIVE}, 400, {"error": "Please tick the box to continue."}),
    ({"accepted": True, "terms_version": "old"}, 409, {"error": "version_changed", "terms_version": LIVE}),
])
def test_a_refusal_records_nothing(db, body, status, answer):
    me = make_user()
    resp = call("post", "/api/me/terms/accept", me, body)
    assert (resp.status_code, resp.json()) == (status, answer)
    assert not TermsConsent.objects.filter(user_id=me.id).exists()


def test_no_token_no_terms(db):
    assert call("get", "/api/me/terms", None).status_code == 401


# ---- Talk to Minty ---------------------------------------------------------------------------

MESSAGE = {"name": "Olive", "email": "reply@test.com", "company": "Acme", "topic": "Billing & pricing",
           "message": "Hello\n<b>there</b>", "app": "minty-web"}


@pytest.fixture
def inbox(settings):
    settings.CONTACT_EMAIL, settings.CONTACT_FROM = "help@minty.test", "Minty Support <support@minty.test>"
    contact.reset_throttle()
    yield
    contact.reset_throttle()


def test_a_message_reaches_the_inbox_naming_the_account_whatever_is_typed(db, inbox):
    me = make_user()
    resp = call("post", "/api/me/contact", me, {**MESSAGE, "email": "Reply@Test.com"})
    assert (resp.status_code, resp.json()) == (200, {"sent": True, "reply_to": "Reply@Test.com"})
    sent, = mail.outbox
    assert (sent.to, sent.from_email, sent.reply_to) == (["help@minty.test"], "Minty Support <support@minty.test>",
                                                          ["Reply@Test.com"])
    assert sent.subject == "[Minty app] Billing & pricing — Olive"
    assert "owner@test.com" in sent.body and "&lt;b&gt;there&lt;/b&gt;" in sent.body and "Hello<br>" in sent.body


def test_the_subject_stays_one_line(db, inbox):
    call("post", "/api/me/contact", make_user(), {**MESSAGE, "name": "Olive\r\nBcc: x@evil.test"})
    assert "\n" not in mail.outbox[0].subject


def test_the_company_it_was_opened_from_is_named_and_another_refused(db, inbox):
    me = make_user()
    mine, theirs = acme(me), acme(make_user("other@test.com"))
    assert call("post", "/api/me/contact", me, {**MESSAGE, "entity": str(mine.id)}).status_code == 200
    assert "Acme Shop" in mail.outbox[0].body
    resp = call("post", "/api/me/contact", me, {**MESSAGE, "entity": str(theirs.id)})
    assert (resp.status_code, resp.json()) == (403, {"error": "You don't have access to that company."})


@pytest.mark.parametrize("change, field", [({"name": ""}, "name"), ({"email": "nope"}, "email"),
                                           ({"company": "x" * 151}, "company"), ({"topic": "Other"}, "topic"),
                                           ({"message": "x" * 5001}, "message"), ({"app": "minty-www"}, "app")])
def test_bad_input_is_refused_with_a_sentence_per_field(db, inbox, change, field):
    resp = call("post", "/api/me/contact", make_user(), {**MESSAGE, **change})
    assert resp.status_code == 422 and resp.json()["error"] == contact.REFUSED and field in resp.json()["fields"]
    assert mail.outbox == []


def test_too_many_messages_in_an_hour_are_refused(db, inbox):
    me = make_user()
    for _ in range(contact.HOURLY_LIMIT):
        assert call("post", "/api/me/contact", me, MESSAGE).status_code == 200
    assert call("post", "/api/me/contact", me, MESSAGE).status_code == 429


@pytest.mark.parametrize("unset", ["CONTACT_EMAIL", "CONTACT_FROM"])
def test_an_unset_inbox_or_sender_is_a_500_not_a_silent_drop(db, inbox, settings, unset):
    setattr(settings, unset, "")
    assert call("post", "/api/me/contact", make_user(), MESSAGE).status_code == 500
    assert mail.outbox == []


def test_a_failed_send_is_a_502(db, inbox, monkeypatch):
    def broken(self, *args, **kwargs):
        raise OSError("smtp down")

    monkeypatch.setattr("django.core.mail.EmailMessage.send", broken)
    resp = call("post", "/api/me/contact", make_user(), MESSAGE)
    assert resp.status_code == 502 and "help@minty.test" in resp.json()["error"]
