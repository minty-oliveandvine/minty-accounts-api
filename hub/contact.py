"""The apps' "Talk to Minty" chat: ``POST /api/me/contact`` - Flask's
``user_management/routes/contact_api.py`` (Phase 5 of the Flask retirement, 2026-11-06).

``{name, email, company, topic, message, app, entity?}`` is checked as on the screen (the email
with the invitations' rule, since it becomes the Reply-To) and emailed to ``CONTACT_EMAIL`` from
``CONTACT_FROM``, Reply-To the address given. The typed details are the customer's to give; WHICH
ACCOUNT sent it is not: the email always names the token's person ("Signed in as"). ``entity``,
the company the chat was opened from, is checked against membership (as My Profile's).

Fails loudly: an unset ``CONTACT_EMAIL`` or ``CONTACT_FROM`` is a 500 and a failed send a 502, so a
customer never reads "Sent!" for a message nobody will get. A per-person throttle
(``HOURLY_LIMIT``, in process memory, so per worker - Flask's) stops one account flooding the inbox.
"""

from __future__ import annotations

import html
import json
import logging
import threading
import time
from collections import defaultdict, deque

from django.conf import settings
from django.core.mail import EmailMessage
from django.http import HttpRequest, HttpResponse

from hub.profile import entity_context
from hub.rules import invite_address_error
from hub.transport import guard, refuse, respond

logger = logging.getLogger("minty-accounts-api")

TOPICS = ("Getting started", "Billing & pricing", "Technical issue", "Xero / integrations", "Something else")
APPS = ("minty-web", "minty-payment-request-web", "minty-petty-cash-web", "minty-onboarding-web")
MESSAGE_MAX = 5000
NAME_MAX = 100
COMPANY_MAX = 150
HOURLY_LIMIT = 5

REFUSED = "A detail needs another look before this can send."
FIELD = {
    "name_empty": "Your name is still empty. Add your name to continue.",
    "name_long": f"Your name is over {NAME_MAX} characters. Shorten it to {NAME_MAX} or fewer.",
    "email_empty": "The email is still empty. Add your email so we can reply.",
    "company_empty": "The company is still empty. Add the company this is about.",
    "company_long": f"The company is over {COMPANY_MAX} characters. Shorten it to {COMPANY_MAX} or fewer.",
    "topic": "No topic is picked yet. Pick the one closest to your question.",
    "message_empty": "The message is still empty. Tell us how we can help.",
    "message_long": f"The message is over {MESSAGE_MAX:,} characters. Shorten it to {MESSAGE_MAX:,} or fewer.",
    "app": "This app can't send messages yet. Email us instead.",
}
NO_ACCESS = "You don't have access to that company."
HOUSE = "Something went wrong on our side. Please try again in a moment."

_recent: dict[str, deque[float]] = defaultdict(deque)
_recent_lock = threading.Lock()


def reset_throttle() -> None:
    """Forget every count (tests)."""
    with _recent_lock:
        _recent.clear()


def _allow(user_id: str) -> bool:
    now = time.monotonic()
    with _recent_lock:
        sent = _recent[user_id]
        while sent and now - sent[0] > 3600:
            sent.popleft()
        if len(sent) >= HOURLY_LIMIT:
            return False
        sent.append(now)
        return True


def _one_line(value: str) -> str:
    """No line break may reach a header (it could add headers)."""
    return " ".join((value or "").split())


def _text(body: dict, key: str) -> str:
    value = body.get(key)
    return value.strip() if isinstance(value, str) else ""


def _problems(body: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    name = _text(body, "name")
    if not name:
        out["name"] = FIELD["name_empty"]
    elif len(name) > NAME_MAX:
        out["name"] = FIELD["name_long"]
    email = _text(body, "email")
    if not email:
        out["email"] = FIELD["email_empty"]
    elif problem := invite_address_error(email):
        out["email"] = problem
    company = _text(body, "company")
    if not company:
        out["company"] = FIELD["company_empty"]
    elif len(company) > COMPANY_MAX:
        out["company"] = FIELD["company_long"]
    if not isinstance(body.get("topic"), str) or body["topic"] not in TOPICS:
        out["topic"] = FIELD["topic"]
    message = _text(body, "message")
    if not message:
        out["message"] = FIELD["message_empty"]
    elif len(message) > MESSAGE_MAX:
        out["message"] = FIELD["message_long"]
    if body.get("app") not in APPS:
        out["app"] = FIELD["app"]
    return out


def _html(**fields) -> str:
    rows = [("Name", fields["name"]), ("Email", fields["email"]), ("Company", fields["company"]),
            ("Signed in as", fields["account"]), ("Opened from", fields["on_record"] or "-"), ("App", fields["app"]),
            ("Topic", fields["topic"])]
    cells = "".join(f"<tr><td style='padding:4px 12px 4px 0;color:#666'>{html.escape(k)}</td>"
                    f"<td style='padding:4px 0'>{html.escape(v)}</td></tr>" for k, v in rows)
    text = html.escape(fields["message"]).replace("\n", "<br>")
    return f"<div style='font-family:Arial,sans-serif;font-size:14px;color:#111'><table>{cells}</table><hr><p>{text}</p></div>"


def my_contact(request: HttpRequest) -> HttpResponse:
    early, user = guard(request)
    if early is not None:
        return early
    if request.method != "POST":
        return refuse(request, "Method not allowed", 405)
    inbox, sender = settings.CONTACT_EMAIL, settings.CONTACT_FROM
    try:
        body = json.loads(request.body or b"null")
    except (ValueError, UnicodeDecodeError):
        body = None
    if not isinstance(body, dict):
        return respond(request, {"error": REFUSED, "fields": {"message": FIELD["message_empty"]}}, 422)
    if problems := _problems(body):
        return respond(request, {"error": REFUSED, "fields": problems}, 422)
    on_record = None
    if body.get("entity"):
        entity = entity_context(user, str(body["entity"]))
        if entity is None:
            return refuse(request, NO_ACCESS, 403)
        on_record = entity["name"]
    if not inbox or not sender:
        missing = " and ".join(n for n, v in (("CONTACT_EMAIL", inbox), ("CONTACT_FROM", sender)) if not v)
        logger.error("%s not set: a contact message was refused", missing)
        return refuse(request, HOUSE, 500)
    if not _allow(str(user.id)):
        return refuse(request, f"That's a lot of messages in one hour. Try again later, or email us at {inbox}.", 429)
    name, email, topic = _one_line(_text(body, "name")), _text(body, "email"), body["topic"]
    message = EmailMessage(
        subject=f"[Minty app] {topic} — {name}", from_email=sender, to=[inbox], reply_to=[email],
        body=_html(name=name, email=email, company=_text(body, "company"), account=user.email or "",
                   on_record=on_record, app=body["app"], topic=topic, message=_text(body, "message")),
    )
    message.content_subtype = "html"
    try:
        message.send()
    except Exception:
        logger.exception("Contact message from user=%s failed to send", user.id)
        return refuse(request, f"We couldn't send your message just now. Try again, or email us at {inbox}.", 502)
    logger.info("Contact message sent for user=%s topic=%r app=%s", user.id, topic, body["app"])
    return respond(request, {"sent": True, "reply_to": email})
