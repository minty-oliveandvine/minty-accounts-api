"""The short-lived signed hand-overs a redirect carries to minty-web: ``?flash=`` (the messages a
redirect drained) and ``?xero_conflict=`` (a blocked Xero connect). Flask's
``entity/services/entity_list.py`` ``read_notices`` / ``read_conflict`` (Phase 5 of the Flask
retirement, 2026-11-06).

The format is itsdangerous' ``URLSafeTimedSerializer(SECRET_KEY, salt=...)``, exactly as Flask
signs them (Django's ``signing`` is another format), so a hand-over minted by either side reads in
the other while both run. ``sign_notices`` / ``sign_conflict`` are the writing half (Phase 6).
Never raises: a page draws whatever the URL carries; an expired token is the ordinary case of a
reload, a bad signature is someone editing the URL and is logged as such.
"""

from __future__ import annotations

import logging

from django.conf import settings
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger("minty-accounts-api")

NOTICE_SALT = "hub-flash"
CONFLICT_SALT = "hub-xero-conflict"
MAX_AGE_SECONDS = 300
MAX_NOTICES = 5
MAX_NOTICE_LENGTH = 500


def _category(raw: str) -> str:
    """Flask's flash category as minty-web's toast reads it."""
    if raw in ("error", "danger"):
        return "error"
    if raw in ("warning", "info"):
        return raw
    return "success"


def _load(token: str, salt: str, what: str):
    try:
        return URLSafeTimedSerializer(settings.SECRET_KEY, salt=salt).loads(token, max_age=MAX_AGE_SECONDS)
    except SignatureExpired:
        logger.info("%s: the hand-over had expired, nothing shown", what)
    except BadSignature:
        logger.warning("%s: the hand-over's signature did not verify, nothing shown", what)
    return None


def sign_notices(messages) -> str | None:
    """The ``(category, message)`` pairs a redirect drained, signed for the trip to minty-web - or
    None when there is nothing to tell (Flask's ``sign_notices``, byte-compatible)."""
    notices = [{"category": _category(category), "message": str(message)[:MAX_NOTICE_LENGTH]}
               for category, message in messages if str(message).strip()][:MAX_NOTICES]
    return URLSafeTimedSerializer(settings.SECRET_KEY, salt=NOTICE_SALT).dumps(notices) if notices else None


def sign_conflict(entity_id, name: str, can_move: bool) -> str:
    """The company already holding a Xero organisation, for the refused connect's ``?xero_conflict=``."""
    return URLSafeTimedSerializer(settings.SECRET_KEY, salt=CONFLICT_SALT).dumps(
        {"from": str(entity_id), "name": str(name or "")[:MAX_NOTICE_LENGTH], "can_move": bool(can_move)})


def read_notices(token: str | None) -> list[dict]:
    if not token:
        return []
    data = _load(token, NOTICE_SALT, "Hub notices")
    if data is None:
        return []
    if not isinstance(data, list):
        logger.warning("Hub notices: a signed hand-over that is not a list, nothing shown")
        return []
    return [
        {"category": _category(str(item.get("category", ""))), "message": str(item["message"])}
        for item in data[:MAX_NOTICES]
        if isinstance(item, dict) and isinstance(item.get("message"), str) and item["message"].strip()
    ]


def read_conflict(token: str | None) -> dict | None:
    if not token:
        return None
    data = _load(token, CONFLICT_SALT, "Xero conflict hand-over")
    if data is None:
        return None
    if not isinstance(data, dict) or not str(data.get("from") or "").strip():
        logger.warning("Xero conflict hand-over: not a conflict, nothing shown")
        return None
    return {"entity_id": str(data["from"]).strip(), "entity_name": str(data.get("name") or ""),
            "can_move": bool(data.get("can_move"))}
