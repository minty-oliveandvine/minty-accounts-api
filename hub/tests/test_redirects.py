# ruff: noqa: F811 - pytest fixtures imported from test_handoffs are parameters here
"""Addresses that only send a person on (``hub/redirects.py``) - Flask's
``test_hub_company_settings.py`` hand-over cases, ``test_petty_cash_web_handoff.py`` (history,
settings, share link), ``test_settings_payments_redirect.py`` and the legacy-address tests, restated."""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from django.conf import settings
from django.test import Client
from minty_db.models import UserEntity

from hub.notices import read_notices, sign_conflict
from hub.tests.test_handoffs import claims, landing, login, ref, shop  # noqa: F401
from hub.tests.test_public_and_transport import make_user

HUB, PAY, PETTY = settings.MINTY_WEB_URL, settings.PAYMENT_REQUEST_WEB_URL, settings.PETTY_CASH_WEB_URL


def co(shop):
    return f"/entity/{ref(shop['entity'])}"


@pytest.mark.parametrize("tab", ["users", "integration"])
def test_the_hub_tabs_hand_over_with_what_was_flashed_and_a_refused_connect(shop, tab):
    client = login(Client(), shop["owner"])
    client.get("/handoff/petty-cash-web", {"entity_id": str(uuid.uuid4())})  # flashes "no permission"
    conflict = sign_conflict(shop["entity"].id, "Other Co", True)
    query = landing(client.get(f"{co(shop)}/settings/{tab}", {"xero_conflict": conflict}), HUB)
    target = urlsplit(query["next"][0])
    assert target.path == f"{co(shop)}/settings/{tab}"
    carried = parse_qs(target.query)
    assert read_notices(carried["flash"][0])[0]["category"] == "error" and carried["xero_conflict"] == [conflict]
    assert claims(query)["entity_id"] == str(shop["entity"].id)


def test_a_cashier_may_not_open_the_users_tab_and_a_stranger_no_tab(shop):
    cashier = make_user("cash@test.com")
    UserEntity.objects.create(user_id=cashier.id, entity_id=shop["entity"].id, role="cashier", approved=True)
    assert login(Client(), cashier).get(f"{co(shop)}/settings/users")["Location"] == "/entity"
    assert login(Client(), shop["stranger"]).get(f"{co(shop)}/settings/integration")["Location"] == "/entity"


def test_petty_cash_settings_hand_over_to_the_app_and_need_petty_cash(shop):
    client = login(Client(), shop["owner"])
    target = urlsplit(client.get(f"{co(shop)}/settings/petty-cash")["Location"])
    assert target.path == "/handoff/petty-cash-web"
    assert parse_qs(target.query)["next"] == [f"{co(shop)}/settings/petty-cash"]
    shop["modules"]()
    assert client.get(f"{co(shop)}/settings/petty-cash")["Location"] == "/entity"


def test_the_modules_and_payment_request_tabs(shop):
    client = login(Client(), shop["owner"])
    assert landing(client.get(f"{co(shop)}/settings/modules"), HUB)["next"] == [f"{co(shop)}/settings/modules"]
    assert landing(client.get(f"{co(shop)}/settings/payment-request"), PAY)["next"] == [
        f"{co(shop)}/settings/payment-request"]


def test_the_report_history_keeps_its_filters(shop):
    target = urlsplit(login(Client(), shop["owner"]).get(f"{co(shop)}/petty-cash/reports?from=2026-10-01&page=2")[
        "Location"])
    assert parse_qs(target.query)["next"] == [f"{co(shop)}/petty-cash/reports?from=2026-10-01&page=2"]


def test_an_old_share_address_opens_the_apps_share_page():
    resp = Client().get("/Minty_Report/abc12345/2026-10-01/s3cret/")
    assert (resp.status_code, resp["Location"]) == (308, f"{PETTY}/share/abc12345/2026-10-01/s3cret")


def test_xero_not_connected_goes_to_the_integration_tab(shop):
    resp = login(Client(), shop["owner"]).get(f"{co(shop)}/xero-not-connected")
    assert resp["Location"] == quote(f"{co(shop)}/settings/integration")


@pytest.mark.parametrize("old, new", [
    ("/entity/{id}/bills", "{co}/payment-request"),
    ("/entity/{id}/settings/xero", "{co}/settings/integration"),
    ("/entity/settings/users/{id}", "{co}/settings/users"),
    ("/entity/settings/entity/{id}", "{co}/settings/petty-cash"),
    ("/entity/settings/module/{id}", "{co}/settings/modules"),
    ("/entity/settings/payments/{id}", "{co}/settings/payment-request"),
    ("/invitation/xero-not-connected/{id}", "{co}/xero-not-connected"),
    ("/entity/{id}/report/opening", "{co}/petty-cash"),
    ("/entity/{id}/ending/r-1", "{co}/petty-cash"),
    ("/entity/{id}/ending", "{co}/petty-cash"),
    ("{co}", "{co}/petty-cash"),
    ("{co}/reports", "{co}/petty-cash/reports"),
    ("{co}/reports/2026/10", "{co}/petty-cash/reports/2026/10"),
])
def test_old_addresses_are_308d_with_the_query_kept(shop, old, new):
    fill = {"id": str(shop["entity"].id), "co": co(shop)}
    resp = Client().get(old.format(**fill) + "?x=1")
    assert (resp.status_code, resp["Location"]) == (308, quote(new.format(**fill)) + "?x=1")


def test_a_post_to_an_old_settings_address_keeps_its_method(shop):
    resp = Client().post(f"/entity/{shop['entity'].id}/settings/xero")
    assert resp.status_code == 308
