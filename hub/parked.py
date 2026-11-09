"""Something parked for one use: a pending ``django_session`` row, named by its key signed under a
salt. The row's delete decides who wins, so a second redeem - or two at once - gets nothing.

Two users: the code sign-in's hand-off (``hub/email_signin.py``, gap 1, 2026-11-06) and the
one-time code an app swaps for its token (``hub/handoffs.py``, gap 4, 2026-11-09). Expiry is held
twice - on the row and on the signature - so neither a stale row nor an old link outlives it.
"""

from __future__ import annotations

import logging

from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core import signing

logger = logging.getLogger("minty-accounts-api")

PARKED_KEY = "parked"


def park(data: dict, *, salt: str, max_age: int) -> str:
    """Park ``data`` for ``max_age`` seconds; the signed key to redeem it with."""
    row = SessionStore()
    row[PARKED_KEY] = data
    row.set_expiry(max_age)
    row.create()
    return signing.dumps(row.session_key, salt=salt)


def redeem(signed: str, *, salt: str, max_age: int, what: str) -> dict | None:
    """The parked data, once: None for a missing, forged, expired or already used key. ``what``
    names the parked thing in the log line (never the key itself)."""
    try:
        key = str(signing.loads(signed, salt=salt, max_age=max_age))
    except signing.SignatureExpired:
        logger.warning("%s: expired (older than %ss)", what, max_age)
        return None
    except signing.BadSignature:
        logger.warning("%s: bad signature", what)
        return None
    data = SessionStore(session_key=key).load().get(PARKED_KEY)
    deleted, _ = Session.objects.filter(session_key=key).delete()
    if not data or not deleted:
        logger.warning("%s: already used or lapsed", what)
        return None
    return data
