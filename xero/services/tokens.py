"""A currently-valid Xero access token for a company, for Minty's other backends.

Ported from Flask (``Minty/services/auth/token_service.py``: ``resolve_entity_access_token_for_service``
and the parts of ``ensure_valid_token`` it runs) in Phase 2 of the Flask retirement, 2026-11-05.

Xero rotates the refresh token on every use and invalidates the one it was sent, so two refreshes
of one bundle racing each other leave a dead token in one store and break the connection until a
person reconnects. The other backends therefore never refresh: they ask here. Refreshes of one
bearer are serialised by a Postgres advisory lock held across the call to Xero - the SAME key
Flask's service path takes, so while both run they wait for each other.

The bundle belongs to the person who connected the company (``entities.connected_by_user_id``)
and lives in ``user_token``. ``None`` whenever no usable token can be produced: the caller shows a
reconnect prompt instead of sending Xero a dead token.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import requests
from django.conf import settings
from django.db import connection
from django.utils import timezone
from minty_db.models import Entity, User, UserToken

logger = logging.getLogger("minty-accounts-api")

#: int4 namespace of the advisory lock ("XERO", 0x5845524F): Flask's ``_XERO_LOCK_NAMESPACE``.
#: Never change one without the other while Flask still refreshes.
LOCK_NAMESPACE = 1481594447
#: How long to wait for another refresh of the same bearer to finish.
LOCK_WAIT_SECONDS = 5.0
#: A token this close to its end counts as expired (Flask's ``token_expired``).
EXPIRY_MARGIN_SECONDS = 300
TOKEN_URL = "https://identity.xero.com/connect/token"
#: (connect, read): a slow identity.xero.com must not hold the request.
XERO_TIMEOUT = (5, 10)


def lock_key(user_id: uuid.UUID) -> str:
    """The advisory lock's text key, hashed by Postgres' ``hashtext`` - Flask's exactly."""
    return f"xero_refresh:{user_id}"


@contextmanager
def refresh_lock(user_id: uuid.UUID, wait_seconds: float | None = None) -> Iterator[bool]:
    """Serialise Xero refreshes for one bearer across every process. Yields whether the lock was
    taken before ``wait_seconds`` ran out.

    Session-level, on this request's connection (autocommit: no request here opens a
    transaction), so it is held across the HTTP call to Xero - that call is what spends the
    refresh token. A dropped connection releases it too.
    """
    wait = LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds
    params = [LOCK_NAMESPACE, lock_key(user_id)]
    acquired = False
    with connection.cursor() as cursor:
        deadline = time.monotonic() + wait
        while True:
            cursor.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s))", params)
            acquired = bool(cursor.fetchone()[0])
            if acquired or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        try:
            yield acquired
        finally:
            if acquired:
                cursor.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))", params)


def is_fresh(row: UserToken) -> bool:
    """The access token has more than ``EXPIRY_MARGIN_SECONDS`` of its life left. A row with no
    lifetime or no obtained-at stamp is not fresh."""
    if row.access_token_expires_in is None or row.access_token_obtained_at is None:
        return False
    elapsed = (timezone.now() - row.access_token_obtained_at).total_seconds()
    return elapsed <= int(row.access_token_expires_in) - EXPIRY_MARGIN_SECONDS


def request_refresh(refresh_token: str) -> dict | None:
    """Xero's answer to a refresh, or ``None`` when it refused or could not be reached (logged).
    Nothing is written here: a refused refresh leaves the stored refresh token as it was."""
    try:
        response = requests.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=(settings.XERO_CLIENT_ID, settings.XERO_CLIENT_SECRET),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=XERO_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.error("xero token: refresh failed, Xero unreachable: %s", exc)
        return None
    if response.status_code != 200:
        # Xero's error body names the reason (invalid_grant...), never a token.
        logger.error("xero token: refresh failed with status %s: %s",
                     response.status_code, (response.text or "")[:300])
        return None
    bundle = response.json()
    if not bundle.get("access_token"):
        logger.error("xero token: refresh answered 200 without an access token")
        return None
    return bundle


def store_refreshed(row: UserToken, bundle: dict) -> None:
    """Write a refreshed bundle (Flask's ``upsert_user_token(is_refresh=True)``): each part Xero
    sent, the access token's obtained-at and the refresh token's last use stamped now."""
    now = timezone.now()
    row.access_token = bundle["access_token"]
    row.access_token_obtained_at = now
    fields = ["access_token", "access_token_obtained_at", "refresh_token_last_used_at"]
    if bundle.get("expires_in") is not None:
        row.access_token_expires_in = int(bundle["expires_in"])
        fields.append("access_token_expires_in")
    if bundle.get("refresh_token") is not None:
        row.refresh_token = bundle["refresh_token"]
        fields.append("refresh_token")
    if bundle.get("id_token") is not None:
        row.id_token = bundle["id_token"]
        fields.append("id_token")
    row.refresh_token_last_used_at = now
    row.save(update_fields=fields)


def resolve_entity_access_token(entity_id: uuid.UUID) -> tuple[str, str] | None:
    """``(access_token, xero_org_id)`` for the company, refreshed when it is about to expire, or
    ``None`` (logged) when a person has to reconnect or another refresh did not finish in time."""
    entity = Entity.objects.filter(id=entity_id).values("xero_org_id", "connected_by_user_id", "archived_at").first()
    if entity and entity["archived_at"] is not None:
        logger.info("xero token: entity %s is archived; no token", entity_id)
        return None
    if not entity or not entity["xero_org_id"]:
        logger.info("xero token: entity %s has no Xero org linked", entity_id)
        return None
    xero_org_id = str(entity["xero_org_id"])
    bearer_id = entity["connected_by_user_id"]
    if not bearer_id:
        logger.info("xero token: entity %s has no connected_by_user_id; reconnect required", entity_id)
        return None
    if not User.objects.filter(id=bearer_id).exists():
        logger.warning("xero token: connected_by_user_id %s on entity %s points to a missing user",
                       bearer_id, entity_id)
        return None

    with refresh_lock(bearer_id) as acquired:
        # Read under the lock: whoever held it before us has very likely just written a fresh
        # bundle, and then there is nothing left to do.
        row = UserToken.objects.filter(user_id=bearer_id).first()
        if row is not None and row.access_token and is_fresh(row):
            return row.access_token, xero_org_id
        if not acquired:
            # Another refresh is in flight: refreshing now would spend the refresh token that
            # request is about to spend.
            logger.warning("xero token: timed out waiting for the refresh lock on bearer %s "
                           "(entity %s); not refreshing", bearer_id, entity_id)
            return None
        if row is None or not row.access_token or not row.refresh_token:
            logger.info("xero token: bearer %s of entity %s has no usable bundle; reconnect required",
                        bearer_id, entity_id)
            return None
        logger.info("xero token: access token for entity %s expired, refreshing", entity_id)
        bundle = request_refresh(row.refresh_token)
        if bundle is None:
            logger.info("xero token: refresh failed for bearer %s (entity %s); reconnect required",
                        bearer_id, entity_id)
            return None
        store_refreshed(row, bundle)
        logger.info("xero token: refreshed for entity %s", entity_id)
        return row.access_token, xero_org_id
