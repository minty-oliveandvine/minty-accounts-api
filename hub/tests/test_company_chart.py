"""Settings' accounts and contacts (``hub/company_chart.py``, owner 2026-11-12): the hub's door -
the person, the company, the right - then onboarding-api, which holds the one implementation,
asked with a service assertion naming the company and the person. onboarding-api is faked at
``requests.request``."""

from __future__ import annotations

import uuid

import jwt
import pytest
from django.conf import settings

from hub import company_chart
from hub.tests.test_company import call

ACCOUNTS = "/api/me/company/accounts"
CONTACTS = "/api/me/company/contacts"


class Onboarding:
    def __init__(self):
        self.asked = []
        self.answer = (200, {"accounts": []})
        self.down = False

    def request(self, method, url, headers=None, json=None, timeout=None):
        import requests

        if self.down:
            raise requests.ConnectionError("down")
        claims = jwt.decode(headers["Authorization"][7:], settings.SECRET_KEY, algorithms=["HS256"])
        self.asked.append((method, url.split("/api/internal/chart/")[1], json, claims))
        status, body = self.answer

        class Answer:
            status_code = status

            def json(self):
                return body

        return Answer()


@pytest.fixture
def onboarding(monkeypatch, settings):
    settings.ONBOARDING_API_URL = "http://onboarding.test"
    fake = Onboarding()
    monkeypatch.setattr(company_chart.requests, "request", fake.request)
    return fake


def test_a_cashier_views_and_the_call_names_the_company_and_the_person(shop, onboarding):
    entity = shop["entity"].id
    resp = call("get", ACCOUNTS, shop["cashier"], entity)
    assert (resp.status_code, resp.json()) == (200, {"accounts": []})
    method, path, body, claims = onboarding.asked[0]
    assert (method, path, body) == ("GET", "accounts", None)
    assert (claims["scope"], claims["entity_id"], claims["user_id"]) == (
        "company-chart", str(entity), str(shop["cashier"].id))


def test_changes_need_an_accountant_and_never_reach_onboarding_otherwise(shop, onboarding):
    entity = shop["entity"].id
    refused = call("post", CONTACTS, shop["manager"], entity, {"name": "Kee Wah"})
    assert (refused.status_code, refused.json()) == (
        403, {"error": "You don't have permission to add accounts and contacts here."})
    assert onboarding.asked == []

    onboarding.answer = (201, {"contact": {"name": "Kee Wah"}, "message": "Kee Wah added."})
    made = call("post", CONTACTS, shop["accountant"], entity, {"name": "Kee Wah", "entity_id": "ignored"})
    assert made.status_code == 201 and made.json()["message"] == "Kee Wah added."
    assert onboarding.asked[-1][:3] == ("POST", "contacts", {"name": "Kee Wah", "entity_id": "ignored"})
    # the company is the assertion's, never the body's
    assert onboarding.asked[-1][3]["entity_id"] == str(entity)


def test_onboardings_refusal_comes_back_as_it_is(shop, onboarding):
    account_id = uuid.uuid4()
    onboarding.answer = (403, {"error": "Wages comes with Minty, so it can't be deleted."})
    resp = call("delete", f"{ACCOUNTS}/{account_id}", shop["admin"], shop["entity"].id)
    assert (resp.status_code, resp.json()) == (403, {"error": "Wages comes with Minty, so it can't be deleted."})
    assert onboarding.asked[-1][:2] == ("DELETE", f"accounts/{account_id}")


def test_a_bad_id_a_stranger_and_a_wrong_method_stop_here(shop, onboarding):
    assert call("patch", f"{ACCOUNTS}/nope", shop["admin"], shop["entity"].id, {}).status_code == 404
    assert call("get", ACCOUNTS, shop["admin"], shop["other"].id).status_code == 200  # their other company
    from hub.tests.test_public_and_transport import make_user

    stranger = make_user("stranger@test.com")
    assert call("get", ACCOUNTS, stranger, shop["entity"].id).status_code == 403
    assert call("put", ACCOUNTS, shop["admin"], shop["entity"].id, {}).status_code == 405
    assert all(path != "accounts/nope" for _m, path, _b, _c in onboarding.asked)


def test_onboarding_unreachable_or_unset_is_said_and_logged(shop, onboarding, settings, caplog):
    onboarding.down = True
    resp = call("get", CONTACTS, shop["admin"], shop["entity"].id)
    assert (resp.status_code, resp.json()) == (503, {"error": company_chart.UNAVAILABLE})
    settings.ONBOARDING_API_URL = ""
    resp = call("get", CONTACTS, shop["admin"], shop["entity"].id)
    assert resp.status_code == 503
    assert "ONBOARDING_API_URL is not set" in caplog.text
