"""Seed (or reset) the identities the Minty browser tests sign in as.

    .venv/Scripts/python.exe scripts/e2e_seed.py            # against the database .env points at
    .venv/Scripts/python.exe scripts/e2e_seed.py --print    # also the env block the specs need

Moved from ``Minty/scripts/e2e_seed.py`` on 2026-11-06 (the Flask retirement, Phase 7) and
rewritten on minty-db's models; what it does to the database is unchanged.

Idempotent and narrow: it only ever touches rows it created - the users ``e2e@minty.test`` and
``e2e-terms@minty.test``, the companies ``E2E Petty Cash Shop`` and ``E2E Subscription Shop`` and
those companies' reports, mappings and subscription rows. Everything else is left alone.

Each run:

* creates ``e2e@minty.test`` if missing, sets its password from ``E2E_MINTY_PASSWORD``
  (required), approved, normal system role, and REMOVES its Terms consent so the first browser
  journey always sees minty-web's Terms panel. Nothing ever accepts on this account
  (minty-web's Terms rule);
* creates ``e2e-terms@minty.test`` (same password) WITH the live Terms accepted, every run, as a
  second admin of the Petty Cash shop: the account the journeys sign in as;
* creates the Petty Cash shop if missing (HKD, HK, Petty Cash and Payment Request modules on)
  with both users as admins, and its local Xero placeholders (accounts, contacts, the Petty Cash
  mapping and both modules' account-code lists) unless the company is really connected to Xero;
* deletes the shop's reports (and their expenses, sales, cash counts, history) and share links,
  so the wizard starts from an empty history on every run;
* sets the shop's sales methods (Visa and Alipay electronic, Foodpanda delivery), Flask's
  ``replace_sales_methods`` reconcile;
* creates a SECOND company for the subscription journeys (``E2E Subscription Shop``, the same
  user as admin) and resets it every run: both modules OFF and no subscription rows at all
  (module rows, consent, billing groups, transfers, audit) - so minty-web's live-API specs can
  start a card-free trial on a module the company has never held. The Petty Cash shop keeps
  both modules ON for the other suites and its subscription rows are never touched here.

One transaction: a run either completes or changes nothing.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

E2E_EMAIL = "e2e@minty.test"
# Signs in for the journeys; its Terms are accepted by this seed, so no run ever accepts on
# E2E_EMAIL (whose consent stays removed - the panel is checked there, never answered).
E2E_TERMS_EMAIL = "e2e-terms@minty.test"
E2E_ENTITY_NAME = "E2E Petty Cash Shop"
E2E_SUBSCRIPTION_ENTITY_NAME = "E2E Subscription Shop"
#: Flask's ``SOURCE_GATE``: the agreement recorded as given at the post-sign-in gate.
SOURCE_GATE = "gate"
#: HKD's notes and coins, largest first (Flask's seed's list).
HKD_DENOMINATIONS = ("1000", "500", "100", "50", "20", "10", "5", "2", "1", "0.5", "0.2", "0.1")


class SeedError(RuntimeError):
    """A run that cannot complete; the transaction is rolled back."""


def _now() -> datetime:
    return datetime.now(UTC)


def _user(email: str, first_name: str, last_name: str, password_hash: str):
    from minty_db.models import User

    user = User.objects.filter(username=email).first()
    if user is None:
        return User.objects.create(id=uuid.uuid4(), email=email, username=email, first_name=first_name,
                                   last_name=last_name, password=password_hash, system_role="normal",
                                   approved=True)
    user.password = password_hash
    user.approved = True
    user.save(update_fields=["password", "approved"])
    return user


def _entity(name: str, hkd):
    from minty_db.models import Entity

    entity = Entity.objects.filter(name=name).first()
    if entity is None:
        entity = Entity.objects.create(id=uuid.uuid4(), name=name, status="disconnected", currency_id=hkd.id,
                                       country_code="HK")
    return entity


def _admin(user, entity) -> None:
    from minty_db.models import UserEntity

    if not UserEntity.objects.filter(user_id=user.id, entity_id=entity.id).exists():
        UserEntity.objects.create(user_id=user.id, entity_id=entity.id, role="admin", approved=True)


def _reference_data():
    """HKD with its notes and coins, Hong Kong, and the two module rows. Returns
    ``(hkd, petty, bill)``."""
    from minty_db.models import CashInfo, CountryInfo, CurrencyInfo, EntityFunction

    hkd = CurrencyInfo.objects.filter(currency_code="HKD").first()
    if hkd is None:
        # symbol="" as production records it, NOT "$": the browser falls back to the code when no
        # symbol is recorded, so production renders "HKD 400" and the specs match on that. In the
        # stack seed_catalogue.sql has already created HKD, so this only fires on a bare database.
        hkd = CurrencyInfo.objects.create(id=uuid.uuid4(), currency_code="HKD", currency_name="Hong Kong Dollar",
                                          symbol="", decimal_places=2, is_active=True)
    if not CashInfo.objects.filter(currency_id=hkd.id).exists():
        for order, value in enumerate(HKD_DENOMINATIONS, start=1):
            CashInfo.objects.create(currency_id=hkd.id, cash_value=Decimal(value),
                                    type="note" if Decimal(value) >= 10 else "coin", cash_name=value,
                                    display_order=order, is_active=True)
    hk = CountryInfo.objects.filter(country_code="HK").first()
    if hk is None:
        CountryInfo.objects.create(country_code="HK", alpha3_code="HKG", country_name_en="Hong Kong",
                                   currency_id=hkd.id, phone_code="+852", is_active=True, display_order=1)
    elif not (hk.phone_code or "").strip():
        # The onboarding API refuses a phone whose dial code is not in country_info.
        hk.phone_code = "+852"
        hk.save(update_fields=["phone_code"])

    def module(code: str, name: str, description: str):
        row = EntityFunction.objects.filter(function_code=code).first()
        if row is None:
            row = EntityFunction.objects.create(id=uuid.uuid4(), function_code=code, function_name=name,
                                                description=description, is_active=True)
        return row

    petty = module("PETTY_CASH", "Petty Cash", "Petty cash reports")
    bill = module("PAYMENT_REQUEST", "Payment Request", "Bills and payments (Module 2)")
    return hkd, petty, bill


def _xero_placeholders(entity, user) -> None:
    """The Petty Cash dashboard shows "Setup Required" until the company has its Xero
    account/contact mapping. No Xero here: local placeholder rows named E2E-*, enough for the
    mapping to be complete. A company that IS connected to Xero (the deployed e2e shop, linked to
    a Demo Company by hand) keeps the mapping its sync gave it: placeholders there would be picked
    by a spec and refused by Xero on publish."""
    from minty_db.models import (
        AccountInfo,
        EntityAccountXero,
        EntityBillAccountXero,
        EntityPettycashSettings,
        XeroContactSync,
    )

    settings, _ = EntityPettycashSettings.objects.get_or_create(entity_id=entity.id)
    if entity.xero_org_id:
        print(f"entity is connected to Xero ({entity.xero_tenant_name}); mapping left as it is")
        XeroContactSync.objects.filter(entity_id=entity.id, category="E2E").delete()
        return

    def account(name: str, kind: str):
        row = AccountInfo.objects.filter(entity_id=entity.id, name=name).first()
        if row is None:
            row = AccountInfo.objects.create(id=uuid.uuid4(), entity_id=entity.id, type=kind, name=name,
                                             xero_account_id=str(uuid.uuid4()), xero_code=name.split()[-1],
                                             status="ACTIVE")
        return row

    def contact(name: str):
        row = XeroContactSync.objects.filter(entity_id=entity.id, name=name).first()
        if row is None:
            row = XeroContactSync.objects.create(id=uuid.uuid4(), entity_id=entity.id,
                                                 xero_contact_id=str(uuid.uuid4()), name=name, category="E2E")
        return row

    settings.pettycash_account_id = account("E2E Petty Cash 090", "BANK").id
    settings.bank_account_id = account("E2E Bank 091", "BANK").id
    settings.cash_sale_account_id = account("E2E Cash Sales 200", "REVENUE").id
    settings.discrepancy_bank_account_id = account("E2E Discrepancy Bank 092", "BANK").id
    settings.discrepancy_account_id = account("E2E Cash Discrepancy 499", "EXPENSE").id
    settings.director_account_id = account("E2E Director Loan 835", "CURRLIAB").id
    settings.cash_sale_contact_id = contact("E2E Cash Customer").id
    settings.director_contact_id = contact("E2E Director").id
    settings.discrepancy_contact_id = contact("E2E Discrepancy").id
    settings.save()

    # The expense form's account dropdown reads entity_account_xero (is_active) joined to
    # account_info; its supplier dropdown reads xero_contact_sync. The third name carries commas on
    # purpose: the receipt key is minted from the account name, and a comma inside a key once split
    # it into two broken halves.
    for name in ("E2E Office Expenses 429", "E2E Cleaning 408", "E2E Light, Power, Heating 445"):
        acc = account(name, "EXPENSE")
        if not EntityAccountXero.objects.filter(account_id=acc.id).exists():
            EntityAccountXero.objects.create(id=uuid.uuid4(), account_id=acc.id, name=acc.name, type=acc.type,
                                             xero_account_id=acc.xero_account_id, is_active=True)
    contact("E2E Stationery Supplier")

    # The payment-request app's "Account Code" picker reads entity_bill_account_xero.
    for code, name in (("429", "E2E Office Expenses"), ("408", "E2E Cleaning"), ("445", "E2E Light, Power, Heating")):
        if not EntityBillAccountXero.objects.filter(entity_id=entity.id, account_code=code).exists():
            EntityBillAccountXero.objects.create(id=uuid.uuid4(), entity_id=entity.id, account_code=code,
                                                 account_name=name, account_type="EXPENSE", is_default=False,
                                                 is_active=True, is_deleted=False,
                                                 xero_account_id=str(uuid.uuid4()), sort_order=0,
                                                 created_by=user.id)


def _wipe_reports(entity) -> int:
    """Delete the company's reports and everything under them, and its share links. Returns how
    many reports went."""
    from minty_db.models import (
        Report,
        ReportCashCount,
        ReportExpense,
        ReportHistory,
        ReportSale,
        ShareLink,
    )

    report_ids = list(Report.objects.filter(entity_id=entity.id).values_list("id", flat=True))
    if report_ids:
        ReportExpense.objects.filter(report_id__in=report_ids).delete()
        ReportSale.objects.filter(report_id__in=report_ids).delete()
        ReportCashCount.objects.filter(report_id__in=report_ids).delete()
        ReportHistory.objects.filter(report_id__in=report_ids).delete()
        ShareLink.objects.filter(entity_id=entity.id).delete()
        Report.objects.filter(id__in=report_ids).delete()
    return len(report_ids)


def _replace_sales_methods(entity, electronic: list[str], delivery: list[str]) -> None:
    """Flask's ``replace_sales_methods``: the named methods linked (the global catalogue row found
    by name case-insensitively, created if nobody has it), switched on and ordered 1..n within
    their type; previously enabled electronic/delivery methods absent from the lists are switched
    off (never deleted - old reports reference them)."""
    from minty_db.models import EntitySaleSetting, SaleInfo

    links = {link.sale_id: link for link in
             EntitySaleSetting.objects.filter(entity_id=entity.id, sale__type__in=("electronic", "delivery"))}
    wanted = set()
    for sale_type, names in (("electronic", electronic), ("delivery", delivery)):
        for order, name in enumerate(names, start=1):
            row = SaleInfo.objects.filter(sale_name__iexact=name).first()
            if row is None:
                row = SaleInfo.objects.create(id=uuid.uuid4(), sale_name=name, type=sale_type,
                                              value_name=name.lower().replace(" ", "_") + "_sales", enabled=True)
            wanted.add(row.id)
            if row.id in links:
                EntitySaleSetting.objects.filter(entity_id=entity.id, sale_id=row.id).update(
                    is_active=True, display_order=order)
            else:
                links[row.id] = EntitySaleSetting.objects.create(entity_id=entity.id, sale_id=row.id,
                                                                 is_active=True, display_order=order)
    for sale_id, link in links.items():
        if sale_id not in wanted and link.is_active:
            EntitySaleSetting.objects.filter(entity_id=entity.id, sale_id=sale_id).update(is_active=False)


def _modules(entity, user, functions, *, enabled: bool) -> None:
    """Each module row present; ``enabled=True`` only adds missing ones as on (the Petty Cash
    shop's rows are otherwise left as they are), ``enabled=False`` also switches existing ones off."""
    from django.db.models.functions import Now
    from minty_db.models import EntityFunctionMap

    now = _now()
    for fn in functions:
        rows = EntityFunctionMap.objects.filter(entity_id=entity.id, entity_function_id=fn.id)
        if not rows.exists():
            EntityFunctionMap.objects.create(entity_id=entity.id, entity_function_id=fn.id, is_enabled=enabled,
                                             created_by=user.id, enabled_at=now if enabled else None,
                                             disabled_at=None if enabled else now)
        elif not enabled:
            rows.filter(is_enabled=True).update(is_enabled=False, disabled_at=now, updated_at=Now())


def _reset_subscription_shop(user, hkd, functions):
    """The company minty-web's live subscription journeys run against, reset to "never held
    anything": both modules off, no subscription rows. Its own company, not the Petty Cash shop:
    a module already on is not trial-eligible. The user's account-level rows (Stripe customer,
    billing accounts, cards) are left alone - a card-free trial needs none of them."""
    from minty_db.models import (
        EntityBillingConsent,
        EntityBillingGroup,
        EntityModuleSubscription,
        SubscriptionAuditLog,
        SubscriptionTransfer,
    )

    entity = _entity(E2E_SUBSCRIPTION_ENTITY_NAME, hkd)
    _admin(user, entity)
    for model in (SubscriptionTransfer, SubscriptionAuditLog, EntityBillingConsent, EntityBillingGroup,
                  EntityModuleSubscription):
        model.objects.filter(entity_id=entity.id).delete()
    _modules(entity, user, functions, enabled=False)
    return entity


def seed(password: str) -> dict[str, str]:
    """One run, in one transaction, on the configured database. Returns the ids the specs read."""
    from django.db import transaction
    from django.http import HttpRequest
    from minty_db.models import TermsConsent

    from hub import registry
    from hub.email_signin import hash_code
    from hub.terms import record_consent

    with transaction.atomic():
        hkd, petty, bill = _reference_data()

        # werkzeug's pbkdf2:sha256 format, as every password in the table.
        user = _user(E2E_EMAIL, "Eve", "Tester", hash_code(password))
        TermsConsent.objects.filter(user_id=user.id).delete()

        entity = _entity(E2E_ENTITY_NAME, hkd)
        _admin(user, entity)

        terms_user = _user(E2E_TERMS_EMAIL, "Tess", "Tester", hash_code(password))
        # The fingerprint comes from the manifest (hub/registry.py): False means the live version
        # has no manifest entry, so no consent could be recorded.
        version = registry.current_version(registry.TERMS)
        if not record_consent(HttpRequest(), terms_user.id, version, source=SOURCE_GATE):
            raise SeedError(f"the live Terms version {version!r} is not in the manifest; "
                            f"cannot accept for {E2E_TERMS_EMAIL}")
        _admin(terms_user, entity)
        # Both modules on: the Petty Cash app AND the payment-request app.
        _modules(entity, user, (petty, bill), enabled=True)

        _xero_placeholders(entity, user)
        wiped = _wipe_reports(entity)
        _replace_sales_methods(entity, ["Visa", "Alipay"], ["Foodpanda"])

        subscription_shop = _reset_subscription_shop(user, hkd, (petty, bill))

    print(f"user    {user.id}  {E2E_EMAIL}  (Terms NOT accepted, never accept on it)")
    print(f"user    {terms_user.id}  {E2E_TERMS_EMAIL}  (Terms accepted; the journeys sign in as it)")
    print(f"entity  {entity.id}  {E2E_ENTITY_NAME}  (reports wiped: {wiped})")
    print(f"entity  {subscription_shop.id}  {E2E_SUBSCRIPTION_ENTITY_NAME}  (modules off, subscription rows wiped)")
    return {
        "E2E_MINTY_USER": str(user.id),
        "E2E_MINTY_ENTITY": str(entity.id),
        "E2E_MINTY_SUBSCRIPTION_ENTITY": str(subscription_shop.id),
        "E2E_MINTY_EMAIL": E2E_EMAIL,
        "E2E_MINTY_TERMS_USER": str(terms_user.id),
        "E2E_MINTY_TERMS_EMAIL": E2E_TERMS_EMAIL,
    }


def env_block(ids: dict[str, str]) -> str:
    """The lines the stack job copies into its environment. The password line is a placeholder,
    never the value: the job keeps the one it seeded with."""
    lines = [f"export {name}={value}" for name, value in ids.items()]
    lines.append("export E2E_MINTY_PASSWORD=<the value you seeded with>")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print", action="store_true", help="print the ids and the env block the specs need")
    args = parser.parse_args(argv)

    password = os.environ.get("E2E_MINTY_PASSWORD")
    if not password:
        print("E2E_MINTY_PASSWORD is not set; refusing to seed a user with an unknown password.", file=sys.stderr)
        return 2

    from django.conf import settings

    if not settings.configured:
        sys.path.insert(0, str(ROOT))
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django

        django.setup()
    db = settings.DATABASES["default"]
    print(f"database: {db.get('HOST') or 'local'}:{db.get('PORT', '')}/{db.get('NAME')} "
          f"schema {getattr(settings, 'DB_SCHEMA', '?')}")

    try:
        ids = seed(password)
    except SeedError as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.print:
        print()
        print(env_block(ids))
    return 0


if __name__ == "__main__":
    sys.exit(main())
