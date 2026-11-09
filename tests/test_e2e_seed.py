"""``scripts/e2e_seed.py``: the identities the stack's browser suites sign in as (moved from
Minty's Flask seed, 2026-11-06). Runs on SQLite and on the Postgres build of the schema file."""

from __future__ import annotations

import importlib.util
import uuid
from datetime import date
from pathlib import Path

import pytest
from minty_db.models import (
    AccountInfo,
    EntityAccountXero,
    EntityBillAccountXero,
    EntityFunction,
    EntityFunctionMap,
    EntityModuleSubscription,
    EntityPettycashSettings,
    EntitySaleSetting,
    Report,
    ReportHistory,
    SaleInfo,
    TermsConsent,
    User,
    UserEntity,
)

from hub import registry

_SPEC = importlib.util.spec_from_file_location(
    "e2e_seed", Path(__file__).resolve().parents[1] / "scripts" / "e2e_seed.py")
e2e_seed = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(e2e_seed)

pytestmark = pytest.mark.django_db


def _enabled(entity_id) -> dict[str, bool]:
    codes = dict(EntityFunction.objects.values_list("id", "function_code"))
    return {codes[row.entity_function_id]: row.is_enabled
            for row in EntityFunctionMap.objects.filter(entity_id=entity_id)}


def _active_sales(entity_id) -> list[tuple[str, str, int]]:
    rows = EntitySaleSetting.objects.filter(entity_id=entity_id, is_active=True).select_related("sale")
    return sorted((r.sale.type, r.sale.sale_name, r.display_order) for r in rows)


def test_refuses_without_a_password(monkeypatch, capsys):
    monkeypatch.delenv("E2E_MINTY_PASSWORD", raising=False)
    assert e2e_seed.main([]) == 2
    assert "E2E_MINTY_PASSWORD is not set" in capsys.readouterr().err
    assert not User.objects.filter(username=e2e_seed.E2E_EMAIL).exists()


def test_seeds_the_people_and_companies(capsys):
    ids = e2e_seed.seed("pw-one")

    user = User.objects.get(username=e2e_seed.E2E_EMAIL)
    terms_user = User.objects.get(username=e2e_seed.E2E_TERMS_EMAIL)
    assert str(user.id) == ids["E2E_MINTY_USER"] and str(terms_user.id) == ids["E2E_MINTY_TERMS_USER"]
    assert user.approved and terms_user.approved and user.system_role == "normal"
    assert user.password.startswith("pbkdf2:sha256:")

    # Terms: never on the panel account, the live version on the journeys' account
    assert not TermsConsent.objects.filter(user_id=user.id).exists()
    consent = TermsConsent.objects.get(user_id=terms_user.id)
    assert consent.terms_version == registry.current_version(registry.TERMS)
    assert consent.source == "gate"

    shop = ids["E2E_MINTY_ENTITY"]
    assert {(str(m.user_id), m.role) for m in UserEntity.objects.filter(entity_id=shop)} == {
        (str(user.id), "admin"), (str(terms_user.id), "admin")}
    assert _enabled(shop) == {"PETTY_CASH": True, "PAYMENT_REQUEST": True}

    settings = EntityPettycashSettings.objects.get(entity_id=shop)
    assert settings.pettycash_account_id and settings.cash_sale_contact_id and settings.director_account_id
    assert EntityAccountXero.objects.filter(account__entity_id=shop, is_active=True).count() == 3
    assert set(EntityBillAccountXero.objects.filter(entity_id=shop).values_list("account_code", flat=True)) == {
        "429", "408", "445"}
    assert _active_sales(shop) == [("delivery", "Foodpanda", 1), ("electronic", "Alipay", 2),
                                   ("electronic", "Visa", 1)]

    sub_shop = ids["E2E_MINTY_SUBSCRIPTION_ENTITY"]
    assert _enabled(sub_shop) == {"PETTY_CASH": False, "PAYMENT_REQUEST": False}
    assert UserEntity.objects.filter(entity_id=sub_shop, user_id=user.id, role="admin").exists()

    out = capsys.readouterr().out
    assert f"entity  {shop}  E2E Petty Cash Shop  (reports wiped: 0)" in out


def test_a_second_run_resets_without_duplicating():
    first = e2e_seed.seed("pw-one")
    shop, sub_shop, user_id = first["E2E_MINTY_ENTITY"], first["E2E_MINTY_SUBSCRIPTION_ENTITY"], first["E2E_MINTY_USER"]
    user = User.objects.get(id=user_id)
    old_hash = user.password

    # what a run of the suites leaves behind
    report = Report.objects.create(id=uuid.uuid4(), entity_id=shop, transaction_date=date(2026, 11, 6))
    ReportHistory.objects.create(report_id=report.id, user_id=user.id, action="created")
    TermsConsent.objects.create(user_id=user.id, terms_version="beta-1", document_hash="x" * 64, source="hub")
    EntityFunctionMap.objects.filter(entity_id=sub_shop).update(is_enabled=True)
    EntityModuleSubscription.objects.create(entity_id=sub_shop, function_code="PETTY_CASH", phase="trial")
    visa = SaleInfo.objects.get(sale_name="Visa")
    EntitySaleSetting.objects.filter(entity_id=shop, sale_id=visa.id).update(is_active=False)
    paypal = SaleInfo.objects.create(id=uuid.uuid4(), sale_name="PayPal", type="electronic", value_name="paypal_sales")
    EntitySaleSetting.objects.create(entity_id=shop, sale_id=paypal.id, is_active=True, display_order=3)
    counts = (AccountInfo.objects.count(), EntityAccountXero.objects.count(), EntityBillAccountXero.objects.count(),
              User.objects.count(), UserEntity.objects.count(), SaleInfo.objects.count())

    second = e2e_seed.seed("pw-two")

    assert second == first
    assert User.objects.get(id=user_id).password != old_hash
    assert not Report.objects.filter(entity_id=shop).exists()
    assert not ReportHistory.objects.filter(report_id=report.id).exists()
    assert not TermsConsent.objects.filter(user_id=user_id).exists()
    assert _enabled(sub_shop) == {"PETTY_CASH": False, "PAYMENT_REQUEST": False}
    assert not EntityModuleSubscription.objects.filter(entity_id=sub_shop).exists()
    assert _active_sales(shop) == [("delivery", "Foodpanda", 1), ("electronic", "Alipay", 2),
                                   ("electronic", "Visa", 1)]
    assert EntitySaleSetting.objects.filter(entity_id=shop, sale_id=paypal.id).exists()  # off, never deleted
    assert (AccountInfo.objects.count(), EntityAccountXero.objects.count(), EntityBillAccountXero.objects.count(),
            User.objects.count(), UserEntity.objects.count(), SaleInfo.objects.count()) == counts


def test_a_xero_connected_shop_keeps_its_mapping():
    first = e2e_seed.seed("pw")
    from minty_db.models import Entity

    Entity.objects.filter(id=first["E2E_MINTY_ENTITY"]).update(xero_org_id="org-1", xero_tenant_name="Demo Company")
    AccountInfo.objects.filter(entity_id=first["E2E_MINTY_ENTITY"]).delete()

    e2e_seed.seed("pw")

    assert not AccountInfo.objects.filter(entity_id=first["E2E_MINTY_ENTITY"]).exists()


def test_print_emits_the_env_block(monkeypatch, capsys):
    monkeypatch.setenv("E2E_MINTY_PASSWORD", "pw")
    assert e2e_seed.main(["--print"]) == 0
    out = capsys.readouterr().out
    exports = [line for line in out.splitlines() if line.startswith("export E2E_")]
    assert [line.split("=", 1)[0] for line in exports] == [
        "export E2E_MINTY_USER", "export E2E_MINTY_ENTITY", "export E2E_MINTY_SUBSCRIPTION_ENTITY",
        "export E2E_MINTY_EMAIL", "export E2E_MINTY_TERMS_USER", "export E2E_MINTY_TERMS_EMAIL",
        "export E2E_MINTY_PASSWORD"]
    assert "export E2E_MINTY_PASSWORD=<the value you seeded with>" in exports
    assert "pw" not in [line.split("=", 1)[1] for line in exports]
