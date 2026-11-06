"""What this service asks minty-subscription-api about a company's subscription - read-only
(``billing/internal.py`` there; the owner's choice, 2026-11-06: the hub asks the subscription
service rather than keeping Flask's read-only copy, ``store_ro.py``).

Each call is a 60-second assertion scoped ``subscription-facts``. How a failure lands is each
caller's choice, because the stakes differ:

- the entity list's badges (``attention``) fail SOFT: a short timeout, then no flags (logged) -
  the list never waits on a sleeping service;
- the payer guards (``entity_facts``, ``payer_entities``) raise ``SubscriptionUnavailable`` and the
  caller refuses the change: removing or demoting a payer on a guess is the harm they exist to stop.
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings
from minty_shared import tokens

logger = logging.getLogger("minty-accounts-api")

SCOPE = "subscription-facts"
#: The list's badges: never long enough for a free service's cold start to hold the page.
ATTENTION_TIMEOUT = (2, 4)
#: The guards: a membership change may wait for the answer.
FACTS_TIMEOUT = (5, 30)


class SubscriptionUnavailable(Exception):
    """minty-subscription-api could not answer (logged)."""


def _headers(entity_id="*") -> dict[str, str]:
    return tokens.assertion_headers(tokens.service_assertion(settings.SECRET_KEY, SCOPE, entity_id))


def _call(method: str, path: str, timeout, **kwargs) -> dict:
    url = f"{settings.SUBSCRIPTION_API_URL}/api/internal/subscriptions/{path}"
    try:
        response = requests.request(method, url, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        logger.error("subscription facts: %s unreachable: %s", path, exc)
        raise SubscriptionUnavailable from None
    if response.status_code != 200:
        logger.error("subscription facts: %s answered %s: %s", path, response.status_code, (response.text or "")[:200])
        raise SubscriptionUnavailable
    try:
        body = response.json()
    except ValueError:
        logger.error("subscription facts: %s answered non-JSON", path)
        raise SubscriptionUnavailable from None
    if not isinstance(body, dict):
        raise SubscriptionUnavailable
    return body


def attention(entity_ids) -> dict[str, dict]:
    """``{entity_id: {"trials": {code: ISO end}, "payment_failed": bool}}``; ``{}`` when it
    could not be asked (logged) - a missing badge, never a missing list."""
    ids = [str(i) for i in entity_ids]
    if not ids:
        return {}
    try:
        body = _call("POST", "attention", ATTENTION_TIMEOUT, json={"entity_ids": ids}, headers=_headers())
    except SubscriptionUnavailable:
        return {}
    found = body.get("attention")
    return found if isinstance(found, dict) else {}


def entity_facts(entity_id) -> dict:
    """``{payer, payers, pending_transfer_to, billed_modules}``. Raises ``SubscriptionUnavailable``."""
    return _call("GET", f"entities/{entity_id}", FACTS_TIMEOUT, headers=_headers(entity_id))


def payer_entities(user_id) -> list[tuple[str, str]]:
    """``(entity_id, name)`` for every company this person pays for. Raises ``SubscriptionUnavailable``."""
    body = _call("GET", f"payers/{user_id}/entities", FACTS_TIMEOUT, headers=_headers())
    return [(str(e.get("id")), str(e.get("name") or e.get("id"))) for e in body.get("entities") or []]
