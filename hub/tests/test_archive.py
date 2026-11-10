"""Archived companies (``entities.archived_at``, owner 2026-11-10): hidden from the list (a
superuser sees them, filtered by ``?archived=``), refused at every door, archived by an admin
(``POST /api/me/company/archive``, refused while a module is paid for) and restored by a superuser
only (``POST /api/me/entities/<id>/restore``)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from django.test import Client
from minty_db.models import Entity, Invitation, User, UserEntity

from hub import invitations
from hub.tests.test_company import WEB, call
from hub.tests.test_public_and_transport import make_user, token
from xero.services import tokens

LIST = "/api/me/entities"
ARCHIVE = "/api/me/company/archive"


def archive(entity, by=None):
    Entity.objects.filter(id=entity.id).update(archived_at=datetime.now(UTC),
                                               archived_by_user_id=by.id if by else None)


def superuser(email="boss@test.com"):
    boss = make_user(email)
    User.objects.filter(id=boss.id).update(system_role="superadmin")
    return boss


def listing(person, **query):
    return Client().get(LIST, query, HTTP_AUTHORIZATION=f"Bearer {token(person.id)}", HTTP_ORIGIN=WEB)


def names(resp):
    return sorted(e["name"] for e in resp.json()["entities"])


def restore(person, entity_id):
    return Client().post(f"/api/me/entities/{entity_id}/restore", HTTP_ORIGIN=WEB,
                         HTTP_AUTHORIZATION=f"Bearer {token(person.id)}")


# ---- the list --------------------------------------------------------------------------------


def test_a_member_never_sees_an_archived_company(shop):
    archive(shop["other"])
    resp = listing(shop["admin"])
    assert resp.status_code == 200
    assert names(resp) == ["Olive & Vine"]
    assert resp.json()["is_superuser"] is False


def test_a_member_cannot_ask_for_the_archived_ones(shop):
    archive(shop["other"])
    assert names(listing(shop["admin"], archived="archived")) == ["Olive & Vine"]


def test_a_superuser_sees_every_company_with_its_archived_stamp(shop):
    archive(shop["other"], by=shop["admin"])
    resp = listing(superuser())
    rows = {e["name"]: e["archived_at"] for e in resp.json()["entities"]}
    assert resp.json()["is_superuser"] is True
    assert rows["Olive & Vine"] is None and rows["Other Co"] is not None


def test_a_superuser_filters_active_and_archived(shop):
    archive(shop["other"])
    boss = superuser()
    assert names(listing(boss, archived="active")) == ["Olive & Vine"]
    assert names(listing(boss, archived="archived")) == ["Other Co"]
    assert names(listing(boss, archived="all")) == ["Olive & Vine", "Other Co"]


def test_an_unknown_filter_is_refused_for_a_superuser(shop):
    resp = listing(superuser(), archived="gone")
    assert resp.status_code == 422 and "All, Active or Archived" in resp.json()["error"]


# ---- the doors -------------------------------------------------------------------------------


def test_the_company_tabs_refuse_an_archived_company(shop):
    archive(shop["entity"])
    resp = call("get", "/api/me/company/users", shop["admin"], shop["entity"].id)
    assert resp.status_code == 403


def test_a_superuser_cannot_open_an_archived_company_either(shop):
    archive(shop["entity"])
    resp = call("get", "/api/me/company/users", superuser(), shop["entity"].id)
    assert resp.status_code == 403


def test_no_invitation_is_made_into_an_archived_company(shop):
    archive(shop["entity"])
    invitation, error = invitations.create_invitation(shop["entity"].id, "new@test.com", "cashier", shop["admin"].id,
                                                      "New", "Person")
    assert invitation is None and error


def test_an_open_invitation_into_an_archived_company_cannot_be_accepted(shop):
    invitation, error = invitations.create_invitation(shop["entity"].id, "new@test.com", "cashier", shop["admin"].id,
                                                      "New", "Person")
    assert error is None
    archive(shop["entity"])
    joiner = make_user("new@test.com")
    entity_id, refusal = invitations.accept_invitation(invitation.token, joiner.id)
    assert entity_id is None and refusal
    assert not UserEntity.objects.filter(user_id=joiner.id, entity_id=shop["entity"].id).exists()
    assert Invitation.objects.get(id=invitation.id).status == "pending"


def test_no_xero_token_is_handed_out_for_an_archived_company(shop):
    Entity.objects.filter(id=shop["entity"].id).update(xero_org_id="org-1", connected_by_user_id=shop["admin"].id)
    archive(shop["entity"])
    assert tokens.resolve_entity_access_token(shop["entity"].id) is None


# ---- archive ---------------------------------------------------------------------------------


def test_an_admin_archives_their_company(shop):
    resp = call("post", ARCHIVE, shop["admin"], shop["entity"].id)
    assert resp.status_code == 200 and "archived" in resp.json()["message"]
    row = Entity.objects.get(id=shop["entity"].id)
    assert row.archived_at is not None and row.archived_by_user_id == shop["admin"].id


def test_only_an_admin_may_archive(shop):
    resp = call("post", ARCHIVE, shop["accountant"], shop["entity"].id)
    assert resp.status_code == 403
    assert Entity.objects.get(id=shop["entity"].id).archived_at is None


def test_a_paid_subscription_blocks_the_archive(shop, facts):
    facts["value"] = {**facts["value"], "paid_modules": ["PETTY_CASH"]}
    resp = call("post", ARCHIVE, shop["admin"], shop["entity"].id)
    assert resp.status_code == 409 and "Cancel its subscription first" in resp.json()["error"]
    assert Entity.objects.get(id=shop["entity"].id).archived_at is None


def test_a_trial_or_a_running_out_cancellation_does_not_block(shop, facts):
    facts["value"] = {**facts["value"], "billed_modules": ["PETTY_CASH"], "paid_modules": []}
    assert call("post", ARCHIVE, shop["admin"], shop["entity"].id).status_code == 200


def test_no_answer_from_billing_archives_nothing(shop, facts):
    facts["value"] = None
    resp = call("post", ARCHIVE, shop["admin"], shop["entity"].id)
    assert resp.status_code == 503
    assert Entity.objects.get(id=shop["entity"].id).archived_at is None


def test_archive_is_post_only(shop):
    assert call("get", ARCHIVE, shop["admin"], shop["entity"].id).status_code == 405


# ---- restore ---------------------------------------------------------------------------------


def test_a_superuser_restores_an_archived_company(shop):
    archive(shop["entity"], by=shop["admin"])
    resp = restore(superuser(), shop["entity"].id)
    assert resp.status_code == 200 and "Olive & Vine" in resp.json()["message"]
    row = Entity.objects.get(id=shop["entity"].id)
    assert row.archived_at is None and row.archived_by_user_id is None


def test_an_admin_cannot_restore(shop):
    archive(shop["entity"])
    assert restore(shop["admin"], shop["entity"].id).status_code == 403
    assert Entity.objects.get(id=shop["entity"].id).archived_at is not None


def test_restoring_a_company_that_is_not_there_is_a_404(shop):
    assert restore(superuser(), uuid.uuid4()).status_code == 404
    assert restore(superuser("boss2@test.com"), "not-an-id").status_code == 404


def test_restore_is_post_only(shop):
    archive(shop["entity"])
    resp = Client().get(f"/api/me/entities/{shop['entity'].id}/restore", HTTP_ORIGIN=WEB,
                        HTTP_AUTHORIZATION=f"Bearer {token(superuser().id)}")
    assert resp.status_code == 405


def test_an_older_billing_answer_without_paid_modules_archives_nothing(shop, facts):
    facts["value"] = {k: v for k, v in facts["value"].items() if k != "paid_modules"}
    assert call("post", ARCHIVE, shop["admin"], shop["entity"].id).status_code == 503
    assert Entity.objects.get(id=shop["entity"].id).archived_at is None
