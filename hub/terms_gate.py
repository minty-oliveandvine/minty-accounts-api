"""The Terms gate for the browser session - Flask's ``legal/routes/gate.py``
``require_terms_acceptance`` (Phase 6 of the Flask retirement, 2026-11-06).

A person signed in through the cookie who has not agreed to the live Terms is sent to the company
list (``/entity``), which hands over to minty-web, which draws the acceptance panel over itself;
a JSON caller gets 403 ``terms_acceptance_required``. Nobody signed in, the routes that get a
person in or out, and the acceptance flow itself are never gated (``ALLOWED``). The answer is
cached in the session as the version agreed, so a new version re-asks everyone. A live version
with no manifest entry gates nobody (nobody could agree to it), and a failure of the gate itself
lets the request through, loudly - an outage is worse than a briefly skipped backstop (Flask's
choice). The bearer routes carry no cookie and are untouched.
"""

from __future__ import annotations

import logging

from django.http import HttpRequest, HttpResponseRedirect

from core.flask_json import flask_json
from hub import registry
from hub.session import USER_KEY
from hub.terms import terms_owed

logger = logging.getLogger("minty-accounts-api")

TERMS_OK_KEY = "terms_ok"
ALLOWED = frozenset({
    "health", "home", "register", "auth_notices", "email_request_code", "email_verify_code", "email_handoff",
    "logout", "invitation_accept", "entity_list", "legal_current", "invite_terms_status", "terms_status",
    "terms_accept", "invitation_logo",
    # Xero's return: its start routes stay gated, so only a flow begun while agreed gets here, and
    # gating it threw away a connect the person had already granted at Xero (Flask did; 2026-11-06).
    "xero_callback",
})


def _wants_json(request: HttpRequest) -> bool:
    return ("/api/" in request.path or request.content_type == "application/json"
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in (request.headers.get("Accept") or "").lower())


class TermsGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest):
        return self.get_response(request)

    def process_view(self, request: HttpRequest, view_func, view_args, view_kwargs):
        try:
            user_id = request.session.get(USER_KEY)
            match = request.resolver_match
            if not user_id or match is None or match.url_name in ALLOWED:
                return None
            live = registry.current_version(registry.TERMS)
            if registry.get_current(registry.TERMS) is None:
                logger.error("Terms gate is NOT enforcing: current version %r is not in the manifest", live)
                return None
            if request.session.get(TERMS_OK_KEY) == live:
                return None
            if terms_owed(user_id) is None:
                request.session[TERMS_OK_KEY] = live
                return None
            if _wants_json(request):
                return flask_json({"status": "error", "code": "terms_acceptance_required",
                                   "message": "Please accept the Terms of Use to continue."}, 403)
            return HttpResponseRedirect("/entity")
        except Exception:
            logger.exception("Terms acceptance gate failed; allowing the request through. "
                             "This means the gate is NOT enforcing - investigate immediately.")
            return None
