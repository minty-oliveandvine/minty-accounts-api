"""Who is signed in through the browser's cookie - Flask-Login's session, its sign-in hooks and
``services/user_presence.py`` (Phase 6 of the Flask retirement, 2026-11-06).

The session is Django's (``django_session``, settings ``SESSION_*``). There is no
``django.contrib.auth``: the session holds the minty-db user's id and the sign-in's id
(``login_sid``, the module tokens' ``sid`` claim), nothing else that grants anything.

- ``signed_in_user(request)``: the approved person the cookie names, else None. A cookie naming a
  person who is gone or switched off is emptied (Flask's ``before_request_middleware``).
- ``sign_in(request, user)``: a NEW session key (no fixation), a fresh ``login_sid``, and the
  presence stamps Flask's ``user_logged_in`` signal wrote. ``sign_out(request)``: the reverse.
- ``flash`` / ``drain_flashes``: Flask's flashed messages, carried in the session until a redirect
  signs them into ``?flash=`` (``hub/notices.sign_notices``).
- ``safe_internal_path``: the one rule for a redirect target taken from a request.
- Presence (who is signed in, for Settings > Users): ``signed_in_at`` + ``last_seen_at`` on the
  user, the same two columns and rules as Flask. A failed presence write is logged and never breaks
  a sign-in.
"""

from __future__ import annotations

import logging
import time
import uuid
from urllib.parse import urlsplit

from django.db.models import Case, F, Value, When
from django.db.models.functions import Coalesce
from django.http import HttpRequest
from django.utils import timezone
from minty_db.models import User

logger = logging.getLogger("minty-accounts-api")

USER_KEY = "user_id"
LOGIN_SID_KEY = "login_sid"
FLASHES_KEY = "_flashes"
PRESENCE_KEY = "presence_seen_at"
#: Presence is refreshed at most this often per session (Flask's ``SEEN_REFRESH_SECONDS``).
SEEN_REFRESH_SECONDS = 60


# ---- the person ---------------------------------------------------------------------------------


def signed_in_user(request: HttpRequest) -> User | None:
    user_id = request.session.get(USER_KEY)
    if not user_id:
        return None
    user = User.objects.filter(id=user_id).first()
    if user is None or not user.approved:
        logger.info("session: the cookie names a person who is gone or switched off; emptied (user=%s)", user_id)
        request.session.flush()
        return None
    return user


def sign_in(request: HttpRequest, user: User) -> None:
    request.session.cycle_key()
    request.session.pop("terms_ok", None)  # a new sign-in is checked against the Terms afresh
    request.session[USER_KEY] = str(user.id)
    request.session[LOGIN_SID_KEY] = uuid.uuid4().hex
    request.session["last_activity"] = time.time()
    request.session[PRESENCE_KEY] = time.time()
    mark_signed_in(user)
    logger.info("session: signed in user=%s", user.id)


def sign_out(request: HttpRequest) -> None:
    user_id = request.session.get(USER_KEY)
    if user_id:
        _stamp(user_id, signed_in_at=None, last_seen_at=timezone.now())
        logger.info("session: signed out user=%s", user_id)
    request.session.flush()


def login_sid(request: HttpRequest) -> str | None:
    return request.session.get(LOGIN_SID_KEY)


# ---- flashed messages ---------------------------------------------------------------------------


def flash(request: HttpRequest, message: str, category: str = "message") -> None:
    request.session[FLASHES_KEY] = [*request.session.get(FLASHES_KEY, []), [category, message]]


def drain_flashes(request: HttpRequest) -> list[tuple[str, str]]:
    return [tuple(pair) for pair in request.session.pop(FLASHES_KEY, [])]


# ---- redirect targets ---------------------------------------------------------------------------


def safe_internal_path(candidate: str | None) -> str | None:
    """``candidate`` when it is a path on this site, else None (Flask's ``safe_redirect``): no
    ``//host``, backslash, control character or ``..``, nothing ``urlsplit`` reads as a scheme or
    host - browsers turn ``/%09/evil.com`` and ``/\\evil.com`` into ``//evil.com``."""
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return None
    if "\\" in candidate or ".." in candidate:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
        return None
    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc:
        return None
    return candidate


def safe_next(candidate: str | None, default: str) -> str:
    return safe_internal_path((candidate or "").strip()) or default


# ---- presence -----------------------------------------------------------------------------------


def _stamp(user_id, **values) -> None:
    try:
        User.objects.filter(id=user_id).update(**values)
    except Exception as exc:  # noqa: BLE001 - presence is decoration on a user list
        logger.warning("user_presence: write failed user=%s: %s", user_id, exc)


def mark_signed_in(user: User) -> None:
    now = timezone.now()
    _stamp(user.id, signed_in_at=now, last_seen_at=now)


def resume_presence(user: User) -> None:
    """Back on the list because they just opened a company (keeps the original sign-in time)."""
    now = timezone.now()
    _stamp(user.id, last_seen_at=now, signed_in_at=Coalesce(F("signed_in_at"), Value(now)))


def refresh_presence(request: HttpRequest, user: User) -> None:
    """Hold a signed-in person on the list (at most once a minute per session). A blank
    ``signed_in_at`` beside a set ``last_seen_at`` means they signed out elsewhere: left blank."""
    last = request.session.get(PRESENCE_KEY)
    now_ts = time.time()
    if last and now_ts - float(last) < SEEN_REFRESH_SECONDS:
        return
    request.session[PRESENCE_KEY] = now_ts
    now = timezone.now()
    _stamp(user.id, last_seen_at=now, signed_in_at=Case(
        When(signed_in_at__isnull=False, then=F("signed_in_at")),
        When(last_seen_at__isnull=True, then=Value(now)),
        default=Value(None)))
