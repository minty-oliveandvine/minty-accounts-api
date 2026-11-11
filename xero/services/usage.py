"""Which of a company's accounts and contacts something still uses - so nothing used is ever deleted.

"Used" is the owner's definition (2026-11-11, extending ``Minty/docs/modernisation/
xero_optional_database.md`` Q8): an account is used when an expense line, a bill line (by id, or by
code on a line saved before ``bill_line.account_id``), any of daily closing's accounts
(``entity_pettycash_settings``), or a sales method (``entity_sale_setting.account_id``) points at it,
or another account was combined into it (``merged_into_id``). The sync archives a used account Xero
dropped instead of deleting it; matching (``matching.py``) requires a used account to be matched.

A contact is used when an expense line, a bill, or daily closing's contacts point at it.
"""

from __future__ import annotations

from minty_db.models import (
    AccountInfo,
    Bill,
    BillLine,
    EntityPettycashSettings,
    EntitySaleSetting,
    ReportExpense,
)

ACCOUNT_SLOTS = (
    "pettycash_account_id",
    "bank_account_id",
    "cash_sale_account_id",
    "discrepancy_bank_account_id",
    "discrepancy_account_id",
    "director_account_id",
)
CONTACT_SLOTS = ("director_contact_id", "cash_sale_contact_id", "discrepancy_contact_id")


def used_account_ids(entity_id) -> set:
    used = set(
        ReportExpense.objects.filter(report__entity_id=entity_id, account_id__isnull=False)
        .values_list("account_id", flat=True)
    )
    lines = BillLine.objects.filter(bill__entity_id=entity_id)
    used |= set(lines.filter(account_id__isnull=False).values_list("account_id", flat=True))
    codes = {c.strip() for c in lines.values_list("account_code", flat=True) if c and c.strip()}
    if codes:
        used |= set(
            AccountInfo.objects.filter(entity_id=entity_id, xero_code__in=codes).values_list("id", flat=True)
        )
    settings = EntityPettycashSettings.objects.filter(entity_id=entity_id).values(*ACCOUNT_SLOTS).first()
    if settings:
        used |= {v for v in settings.values() if v}
    used |= set(
        EntitySaleSetting.objects.filter(entity_id=entity_id, account_id__isnull=False)
        .values_list("account_id", flat=True)
    )
    used |= set(
        AccountInfo.objects.filter(entity_id=entity_id, merged_into_id__isnull=False)
        .values_list("merged_into_id", flat=True)
    )
    return used


def used_contact_ids(entity_id) -> set:
    used = set(
        ReportExpense.objects.filter(report__entity_id=entity_id, contact_id__isnull=False)
        .values_list("contact_id", flat=True)
    )
    used |= set(
        Bill.objects.filter(entity_id=entity_id, contact_id__isnull=False).values_list("contact_id", flat=True)
    )
    settings = EntityPettycashSettings.objects.filter(entity_id=entity_id).values(*CONTACT_SLOTS).first()
    if settings:
        used |= {v for v in settings.values() if v}
    return used
