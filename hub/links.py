"""Where a navigation from this service goes next - Flask's ``blueprints/shared/preview_origin.py``
and ``auth/services/hub_login.py`` (Phase 6 of the Flask retirement, 2026-11-06).

**Preview sets.** Where ``PREVIEW_ORIGIN_REGEX`` is set, a branch preview's front ends name
themselves with ``?return_origin=`` and a hand-off goes back to the same set's host for the target
app. Only a SAME-SITE navigation may name a set (``Sec-Fetch-Site``); the alternate set
(``EXTRA_FRONTEND_ORIGINS``) may be named from any trip; a hop that can't name one (a link on one of
this service's own redirects, Xero's OAuth return) reads the set the session stored, for at most
``SET_TTL_SECONDS``. ``PreviewMiddleware`` is Flask's ``capture`` hook. Unset, nothing here does
anything.

**The sign-in page** is minty-web's ``/login`` (``/signup`` to make an account): ``hub_login_url``
keeps ``next`` only when it is a path on this site and drains the session's flashes into a signed
``?flash=``.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlencode

from django.conf import settings
from django.http import HttpRequest

from hub.notices import sign_notices
from hub.session import drain_flashes, safe_internal_path

SESSION_KEY = "preview_set"
ALT_SLUG = "~alt"
SET_TTL_SECONDS = 30 * 60
HUB_LOGIN_PATH = "/login"
HUB_SIGNUP_PATH = "/signup"

_SET_ORIGIN = re.compile(
    r"https://pr-([a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?)-(my|payment-request|onboarding|pettycash)\.dailyminty\.com"
)


def _configured() -> set[str]:
    return {settings.MINTY_WEB_URL, settings.PAYMENT_REQUEST_WEB_URL, settings.ONBOARDING_WEB_URL,
            settings.PETTY_CASH_WEB_URL}


def parse(origin: str) -> tuple[str, str] | None:
    pattern = settings.PREVIEW_ORIGIN
    if pattern is None or not pattern.fullmatch(origin):
        return None
    match = _SET_ORIGIN.fullmatch(origin)
    return (match.group(1), match.group(2)) if match else None


def _named(request: HttpRequest) -> str | None:
    """A set's slug, ``""`` for the configured front ends, or None for a hop that can't name one."""
    fetch_site = request.headers.get("Sec-Fetch-Site")
    raw = request.GET.get("return_origin")
    if raw is not None:
        origin = raw.strip().rstrip("/")
        if origin in _configured():
            return ""
        if origin in settings.EXTRA_FRONTEND_ORIGINS.values():
            return ALT_SLUG
        parsed = parse(origin)
        if parsed and fetch_site in ("same-site", "same-origin"):
            return parsed[0]
    if request.headers.get("Sec-Fetch-Mode", "navigate") != "navigate":
        return None
    return None if fetch_site in ("same-origin", "cross-site") else ""


def capture(request: HttpRequest) -> None:
    named = _named(request)
    if named:
        request.session[SESSION_KEY] = {"slug": named, "at": int(time.time())}
    elif named == "" and SESSION_KEY in request.session:
        request.session.pop(SESSION_KEY)


def handoff_origin(request: HttpRequest | None, app: str, configured: str) -> str:
    """The set this trip names, else the stored one (still allowed), else ``configured``."""
    if request is None:
        return configured
    slug = _named(request)
    if slug is None:
        stored = request.session.get(SESSION_KEY)
        fresh = isinstance(stored, dict) and time.time() - stored.get("at", 0) < SET_TTL_SECONDS
        slug = stored.get("slug") if fresh else None
    if slug == ALT_SLUG:
        return settings.EXTRA_FRONTEND_ORIGINS.get(app, configured)
    if slug:
        candidate = f"https://pr-{slug}-{app}.dailyminty.com"
        if parse(candidate) == (slug, app):
            return candidate
    return configured


class PreviewMiddleware:
    """Flask's ``preview_origin.capture``, before every view (after the session middleware)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest):
        capture(request)
        return self.get_response(request)


def hub_login_url(request: HttpRequest | None, *, next_path: str | None = None, mode: str | None = None,
                  invite: str | None = None, email: str | None = None, first_name: str | None = None,
                  last_name: str | None = None, carry_flashes: bool = True) -> str:
    """``{hub}/login?...`` (``/signup`` when ``mode`` is ``"signup"``)."""
    params: dict[str, str] = {}
    if safe := safe_internal_path(next_path):
        params["next"] = safe
    for key, value in (("invite", invite), ("email", email), ("fn", first_name), ("ln", last_name)):
        if value:
            params[key] = value
    if carry_flashes and request is not None and (notices := sign_notices(drain_flashes(request))):
        params["flash"] = notices
    path = HUB_SIGNUP_PATH if mode == "signup" else HUB_LOGIN_PATH
    query = f"?{urlencode(params)}" if params else ""
    return f"{handoff_origin(request, 'my', settings.MINTY_WEB_URL)}{path}{query}"
