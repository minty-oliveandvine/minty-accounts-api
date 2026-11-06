"""The Terms gate as minty-web draws it: ``GET /api/me/terms`` and ``POST /api/me/terms/accept`` -
Flask's ``legal/routes/hub.py``, ``services/gate.terms_owed`` and ``services/consent`` (Phase 5 of
the Flask retirement, 2026-11-06).

This service names the version; minty-web holds the text. What is owed: the live version's
number, date, draft flag and fingerprint, whether it is a re-acceptance and of what. Accepting:
not ticked is 400; the version changed while the panel sat open is 409 ``version_changed`` (the
client re-reads); a client fingerprint that differs from the manifest's is 409
``document_mismatch`` (two deploys out of step - nothing recorded); no manifest entry is 500.

The recorded fingerprint is the manifest's, never the client's. The row says where the agreement
was given (``source = "hub"``) and keeps the first ``X-Forwarded-For`` address and the user agent
as supporting detail only: that header is client-supplied. Idempotent: one row per person and
version (a double click is caught by the unique key).
"""

from __future__ import annotations

import json
import logging

from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse
from minty_db.models import TermsConsent

from hub import registry
from hub.transport import guard, refuse, respond

logger = logging.getLogger("minty-accounts-api")

SOURCE_HUB = "hub"


def terms_owed(user_id) -> dict | None:
    """What the person still has to agree to, or None (nothing owed, or the live version has no
    manifest entry - nobody is asked to agree to what can't be recorded)."""
    document = registry.get_current(registry.TERMS)
    version = registry.current_version(registry.TERMS)
    if document is None or TermsConsent.objects.filter(user_id=user_id, terms_version=version).exists():
        return None
    previous = TermsConsent.objects.filter(user_id=user_id).order_by("-accepted_at").first()
    return {"document": document, "is_update": previous is not None,
            "previous_version": previous.terms_version if previous else None}


def _client_details(request: HttpRequest) -> tuple[str | None, str | None]:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    ip = forwarded or request.META.get("REMOTE_ADDR")
    agent = request.headers.get("User-Agent")
    return (ip[:45] if ip else None, agent[:512] if agent else None)


def record_consent(request: HttpRequest, user_id, version: str, source: str = SOURCE_HUB) -> bool:
    """The person's agreement to ``version`` (already there counts). False when the version has
    no manifest entry or fingerprint."""
    entry = registry.get_version(registry.TERMS, version)
    if entry is None or not entry.sha256:
        logger.error("Refusing to record consent for %s: no manifest fingerprint for Terms %r", user_id, version)
        return False
    if TermsConsent.objects.filter(user_id=user_id, terms_version=version).exists():
        return True
    ip, agent = _client_details(request)
    try:
        with transaction.atomic():
            TermsConsent.objects.create(user_id=user_id, terms_version=version, document_hash=entry.sha256,
                                        source=source, ip_address=ip, user_agent=agent)
    except IntegrityError:  # two tabs raced: the other one recorded it
        if not TermsConsent.objects.filter(user_id=user_id, terms_version=version).exists():
            raise
    return True


def terms_status(request: HttpRequest) -> HttpResponse:
    early, user = guard(request)
    if early is not None:
        return early
    try:
        owed = terms_owed(user.id)
    except Exception:
        logger.exception("Terms status failed for user=%s", user.id)
        return refuse(request, "The Terms didn't load. Mind trying again?", 500)
    if owed is None:
        return respond(request, {"owed": False})
    entry = owed["document"]
    return respond(request, {
        "owed": True,
        "document": {"version": entry.version, "effective_date": entry.effective_date, "sha256": entry.sha256,
                     "show_draft_notice": entry.show_draft_notice},
        "is_update": owed["is_update"],
        "previous_version": owed["previous_version"],
    })


def terms_accept(request: HttpRequest) -> HttpResponse:
    early, user = guard(request)
    if early is not None:
        return early
    try:
        payload = json.loads(request.body or b"null")
    except (ValueError, UnicodeDecodeError):
        payload = None
    payload = payload if isinstance(payload, dict) else {}
    submitted = payload.get("terms_version")
    shown_hash = payload.get("document_hash")
    if isinstance(shown_hash, str) and shown_hash:
        entry = registry.get_version(registry.TERMS, submitted) if isinstance(submitted, str) else None
        if entry and entry.sha256 and shown_hash != entry.sha256:
            logger.error("Terms %r fingerprint mismatch for user=%s: minty-web and this service are deployed out "
                         "of step - nothing recorded", submitted, user.id)
            return respond(request, {"error": "document_mismatch"}, 409)
    live = registry.current_version(registry.TERMS)
    if payload.get("accepted") is not True:
        return refuse(request, "Please tick the box to continue.", 400)
    if (submitted if isinstance(submitted, str) else "").strip() != live:
        logger.info("Terms version changed under user %s: submitted %r, live %r", user.id, submitted, live)
        return respond(request, {"error": "version_changed", "terms_version": live}, 409)
    if not record_consent(request, user.id, live):
        return refuse(request, "Could not record your agreement. Please try again.", 500)
    logger.info("Terms %s accepted in minty-web by user=%s", live, user.id)
    return respond(request, {"ok": True, "terms_version": live})
