"""The hub tests' shared company: ``shop`` (Olive & Vine and its four members) and ``facts`` (what
minty-subscription-api says about it, faked at ``core.subscription_client``)."""

from __future__ import annotations

import uuid

import pytest
from minty_db.models import CountryInfo, Entity, EntityFunction, EntityFunctionMap, User, UserEntity

from core import subscription_client
from hub import invitations
from hub.tests.test_company import currency, member
from hub.tests.test_public_and_transport import make_user


@pytest.fixture
def facts(monkeypatch):
    """What minty-subscription-api says about the company; ``None`` = it can't answer."""
    answer = {"value": {"payer": None, "payers": [], "pending_transfer_to": None, "billed_modules": []}}

    def entity_facts(entity_id):
        if answer["value"] is None:
            raise subscription_client.SubscriptionUnavailable("down")
        return answer["value"]

    monkeypatch.setattr(subscription_client, "entity_facts", entity_facts)
    return answer


@pytest.fixture
def shop(db, facts):
    """Olive & Vine with an admin, an accountant, a shop manager and a cashier, and a second
    company the admin is in alone."""
    hkd = currency()
    CountryInfo.objects.create(country_code="HK", alpha3_code="HKG", country_name_en="Hong Kong",
                               currency_id=hkd.id, is_active=True, display_order=1)
    petty = EntityFunction.objects.create(id=uuid.uuid4(), function_code="PETTY_CASH", function_name="Petty Cash")
    admin = make_user("admin@test.com")
    User.objects.filter(id=admin.id).update(first_name="Ada", last_name="Admin")
    admin.refresh_from_db()
    entity = Entity.objects.create(id=uuid.uuid4(), name="Olive & Vine", status="disconnected", currency_id=hkd.id,
                                   country_code="HK")
    other = Entity.objects.create(id=uuid.uuid4(), name="Other Co", status="disconnected", currency_id=hkd.id,
                                  country_code="HK")
    for org in (entity, other):
        UserEntity.objects.create(user_id=admin.id, entity_id=org.id, role="admin", approved=True)
        EntityFunctionMap.objects.create(entity_id=org.id, entity_function_id=petty.id, is_enabled=True)
    invitations.reset_cooldowns()
    yield {"admin": admin, "entity": entity, "other": other, "currency": hkd,
           "accountant": member(entity, "acc@test.com", "accountant"),
           "manager": member(entity, "mgr@test.com", "shop_manager"),
           "cashier": member(entity, "cash@test.com", "cashier")}
    invitations.reset_cooldowns()
