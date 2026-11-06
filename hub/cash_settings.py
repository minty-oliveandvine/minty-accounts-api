"""A company's cash denominations follow its currency - minty-onboarding-api's
``services/cash_settings.py`` copied unchanged (Phase 5 of the Flask retirement, 2026-11-06), itself
Flask's ``entity/services/cash_settings.py``. The Entity & Integration save calls it when the currency
changes. Its own notes follow.


When a company's currency is set or changed, ``entity_cash_setting`` gets one row per ACTIVE
``cash_info`` row of that currency (``is_active`` true, the catalogue's ``display_order``).
Rows for any other currency are deleted, because a company counts one currency's cash.
Rows already present for the new currency are left alone, so a company's own on/off and
order survive a re-save -- the sync is idempotent.

Flask's ``blueprints/entity/services/cash_settings.py`` is the same rule for the settings
pages; change the two together.
"""

import logging

from django.db import transaction
from minty_db.models import CashInfo, EntityCashSetting

logger = logging.getLogger("minty-accounts-api")


def sync_cash_settings(entity_id, currency_id) -> None:
    """Make ``entity_cash_setting`` hold the active denominations of ``currency_id``."""
    if not currency_id:
        return
    entity_id = str(entity_id)
    catalogue = list(
        CashInfo.objects.filter(currency_id=currency_id, is_active=True).values_list(
            "id", "display_order"
        )
    )
    # str() both sides: Django hands back uuid.UUID, the caller may hold a str.
    wanted = {str(cash_id): order for cash_id, order in catalogue}

    with transaction.atomic():
        existing = {
            str(cash_id)
            for cash_id in EntityCashSetting.objects.filter(entity_id=entity_id).values_list(
                "cash_id", flat=True
            )
        }
        stale = existing - wanted.keys()
        if stale:
            EntityCashSetting.objects.filter(entity_id=entity_id, cash_id__in=stale).delete()
        EntityCashSetting.objects.bulk_create(
            EntityCashSetting(
                entity_id=entity_id, cash_id=cash_id, is_active=True, display_order=order
            )
            for cash_id, order in wanted.items()
            if cash_id not in existing
        )

    if not wanted:
        # A currency with no catalogue leaves the company with nothing to count; Flask's
        # cash count reports the same gap when it renders.
        logger.warning(
            "cash settings: currency %s has no active denominations (entity %s)",
            currency_id,
            entity_id,
        )
    logger.info(
        "cash settings: entity=%s currency=%s rows=%s removed=%s",
        entity_id,
        currency_id,
        len(wanted),
        len(stale),
    )
