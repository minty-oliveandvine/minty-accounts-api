"""``GET /api/me/entities`` (``hub/entities.py``) - Flask's ``tests/test_hub_entity_list.py``
expectations for the list itself, restated (its stamping and redirect tests are Flask's session
routes, Phase 6). minty-subscription-api's answer is faked at ``core.subscription_client``."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from django.test import Client
from itsdangerous import URLSafeTimedSerializer
from minty_db.models import Entity, EntityFunction, EntityFunctionMap, User, UserEntity

from core import subscription_client
from hub.tests.test_public_and_transport import make_user, token

URL = "/api/me/entities"
WEB = "http://localhost:3000"


@pytest.fixture
def catalogue(db):
    return {code: EntityFunction.objects.create(id=uuid.uuid4(), function_code=code, function_name=name)
            for code, name in (("PETTY_CASH", "Petty Cash"), ("PAYMENT_REQUEST", "Payment Request"))}


def company(name, *, status="connected", opened=None, by=None, zone=None, modules=(), catalogue=None, member=None,
            role="admin"):
    entity = Entity.objects.create(id=uuid.uuid4(), name=name, status=status, last_accessed_at=opened,
                                   last_accessed_by_user_id=by.id if by else None, timezone=zone)
    for code in modules:
        EntityFunctionMap.objects.create(entity_id=entity.id, entity_function_id=catalogue[code].id, is_enabled=True)
    if member is not None:
        UserEntity.objects.create(user_id=member.id, entity_id=entity.id, role=role, approved=True)
    return entity


@pytest.fixture
def subscriptions(monkeypatch):
    answer = {"attention": {}, "asked": []}

    def attention(entity_ids):
        answer["asked"].append(sorted(str(i) for i in entity_ids))
        return answer["attention"]

    monkeypatch.setattr(subscription_client, "attention", attention)
    return answer


def get(person, **query):
    return Client().get(URL, query, HTTP_AUTHORIZATION=f"Bearer {token(person.id)}", HTTP_ORIGIN=WEB)


def names(resp):
    return [e["name"] for e in resp.json()["entities"]]


def test_the_list_is_the_persons_own_setup_first_then_most_recently_opened(db, catalogue, subscriptions):
    me, other = make_user(), make_user("other@test.com")
    now = datetime.now(UTC)
    company("Old", opened=now - timedelta(days=9), member=me, catalogue=catalogue)
    company("Never", member=me, catalogue=catalogue)
    company("Recent", opened=now - timedelta(hours=1), member=me, catalogue=catalogue)
    company("Wizard", status="onboarding", member=me, catalogue=catalogue)
    company("Not mine", opened=now, member=other, catalogue=catalogue)

    resp = get(me)

    assert resp.status_code == 200 and resp["Access-Control-Allow-Origin"] == WEB
    assert names(resp) == ["Wizard", "Recent", "Old", "Never"]
    assert len(subscriptions["asked"]) == 1  # one question for the whole list


def test_an_entry_carries_every_field(db, catalogue, subscriptions):
    me = make_user()
    opener = make_user("cass@test.com")
    opened = datetime(2026, 11, 5, 16, 30, tzinfo=UTC)
    entity = company("Acme", opened=opened, by=opener, modules=("PETTY_CASH",), catalogue=catalogue, member=me,
                     role="cashier")

    entry, = get(me).json()["entities"]

    assert entry == {"id": str(entity.id), "name": "Acme", "status": "connected", "modules": ["PETTY_CASH"],
                     "trial_modules": [], "trial_module_names": [], "last_accessed_at": "2026-11-05T16:30:00+00:00",
                     "last_accessed_by": "Olive Owner", "role": "cashier", "trial_ends_on": None,
                     "payment_failed": False}


def test_a_superuser_sees_every_company_and_reads_superadmin_where_not_a_member(db, catalogue, subscriptions):
    boss = make_user("boss@test.com")
    User.objects.filter(id=boss.id).update(system_role="superadmin")
    other = make_user("other@test.com")
    company("Theirs", member=other, catalogue=catalogue)
    company("Mine too", member=boss, role="accountant", catalogue=catalogue)

    roles = {e["name"]: e["role"] for e in get(boss).json()["entities"]}
    assert roles == {"Theirs": "superadmin", "Mine too": "accountant"}


def test_a_trial_badge_never_claims_a_module_that_is_off(db, catalogue, subscriptions):
    me = make_user()
    entity = company("Acme", modules=("PETTY_CASH",), catalogue=catalogue, member=me)
    subscriptions["attention"] = {str(entity.id): {"trials": {"PETTY_CASH": "2026-12-01T00:00:00+00:00",
                                                              "PAYMENT_REQUEST": "2026-11-20T00:00:00+00:00"},
                                                   "payment_failed": False}}
    entry, = get(me).json()["entities"]
    assert (entry["trial_modules"], entry["trial_module_names"]) == (["PETTY_CASH"], ["Petty Cash"])
    assert entry["trial_ends_on"] == "2026-12-01"


def test_the_trial_end_is_the_earliest_on_the_companys_own_calendar(db, catalogue, subscriptions):
    me = make_user()
    hk = company("HK", modules=("PETTY_CASH", "PAYMENT_REQUEST"), catalogue=catalogue, member=me)
    ny = company("NY", modules=("PETTY_CASH",), zone="America/New_York", catalogue=catalogue, member=me)
    late_utc = "2026-11-30T20:00:00+00:00"  # already 1 December in Hong Kong, still 30 November in New York
    subscriptions["attention"] = {
        str(hk.id): {"trials": {"PETTY_CASH": "2026-12-05T00:00:00+00:00", "PAYMENT_REQUEST": late_utc},
                     "payment_failed": False},
        str(ny.id): {"trials": {"PETTY_CASH": late_utc}, "payment_failed": False},
    }
    ends = {e["name"]: e["trial_ends_on"] for e in get(me).json()["entities"]}
    assert ends == {"HK": "2026-12-01", "NY": "2026-11-30"}


def test_a_failed_payment_flags_the_company(db, catalogue, subscriptions):
    me = make_user()
    entity = company("Acme", modules=("PETTY_CASH",), catalogue=catalogue, member=me)
    subscriptions["attention"] = {str(entity.id): {"trials": {}, "payment_failed": True}}
    assert get(me).json()["entities"][0]["payment_failed"] is True


def test_subscription_api_not_answering_leaves_the_list_without_badges(db, catalogue):
    me = make_user()  # the conftest blocks every real call to minty-subscription-api
    company("Acme", modules=("PETTY_CASH",), catalogue=catalogue, member=me)
    resp = get(me)
    assert resp.status_code == 200
    assert resp.json()["entities"][0]["payment_failed"] is False


def test_a_person_with_no_company_gets_an_empty_list(db, subscriptions):
    assert get(make_user()).json() == {"entities": [], "notices": []}
    assert subscriptions["asked"] == []


def test_a_signed_hand_over_comes_back_as_notices(db, subscriptions):
    from django.conf import settings

    flash = URLSafeTimedSerializer(settings.SECRET_KEY, salt="hub-flash").dumps(
        [{"category": "success", "message": "You've left Acme."}])
    assert get(make_user(), flash=flash).json()["notices"] == [{"category": "success", "message": "You've left Acme."}]


def test_no_token_is_the_readable_401(db):
    resp = Client().get(URL, HTTP_ORIGIN=WEB)
    assert (resp.status_code, resp.content, resp["Access-Control-Allow-Origin"]) == (
        401, b'{"error":"unauthorized"}\n', WEB)
