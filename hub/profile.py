"""My Profile: ``GET`` / ``PATCH /api/me/profile`` - Flask's ``user_management/routes/me_api.py`` and
``services/profile.py`` (Phase 5 of the Flask retirement, 2026-11-06). minty-web, the payments app
and Petty Cash read it through the shared sidebar; onboarding-api forwards to it.

``?entity=<id>`` names the company the profile was opened from: the answer then carries that
company, the person's role and its modules (403 for one they may not see). ``PATCH {first_name?,
last_name?, email?}`` saves the person's OWN row; a refusal is 422 with the sentence to show.

The email is half of how an account is identified: when ``username`` still holds the old email it
moves with it, otherwise it is a separately chosen handle and stays. Both columns are unique; a
taken address is refused up front and again around the save (another request can take it between).
"""

from __future__ import annotations

import json
import logging
import uuid

from django.db import IntegrityError, transaction
from django.http import HttpRequest, HttpResponse
from minty_db.models import Entity, User, UserEntity

from hub.entities import _modules
from hub.policy import has_entity_access, is_superuser
from hub.rules import EMAIL_ASCII_MESSAGE, is_ascii_email, role_label
from hub.transport import guard, refuse, respond

logger = logging.getLogger("minty-accounts-api")

PATCHABLE = ("first_name", "last_name", "email")
SAVE_FAILED = "That didn't quite save. Mind trying again?"
TAKEN = "That email address is already in use."


class ProfileError(ValueError):
    """A refusal worded for the person, shown as it stands."""


def _clean(value) -> str:
    return (value or "").strip()


def display_name(user: User) -> str:
    """"First Last", or the email (then the username) when the account has no name."""
    return f"{_clean(user.first_name)} {_clean(user.last_name)}".strip() or _clean(user.email) or _clean(user.username)


def initials(user: User) -> str:
    return (_clean(user.first_name)[:1] + _clean(user.last_name)[:1]).upper() or "?"


def entity_context(user: User, entity_id: str) -> dict | None:
    """The company a page was opened from - name, the person's role there, its modules - or None
    when there is no such company, it is archived (``archived_at``; superusers included), or they
    may not see it. A superuser without a membership sees it with no role."""
    try:
        uuid.UUID(str(entity_id))
    except ValueError:
        return None
    org = Entity.objects.filter(id=entity_id, archived_at__isnull=True).first()
    if org is None or not (is_superuser(user) or has_entity_access(user, str(org.id))):
        return None
    membership = UserEntity.objects.filter(user_id=user.id, entity_id=org.id).first()
    role = str(membership.role) if membership is not None and membership.role else None
    return {"id": str(org.id), "name": org.name, "role": role, "role_label": role_label(role),
            "modules": sorted(_modules([org.id]).get(str(org.id), set()))}


def profile_payload(user: User, entity: dict | None = None) -> dict:
    return {
        "user": {"id": str(user.id), "first_name": _clean(user.first_name), "last_name": _clean(user.last_name),
                 "name": display_name(user), "initials": initials(user), "email": _clean(user.email)},
        "entity": entity,
    }


def _taken(field: str, address: str, user_id) -> bool:
    return User.objects.filter(**{f"{field}__iexact": address}).exclude(id=user_id).exists()


def update_profile(user: User, **fields) -> None:
    changed = []
    if "email" in fields:
        email = _clean(fields["email"])
        if not email:
            raise ProfileError("I'll need an email address here.")
        # Not RFC: refuse what could never receive a mail, since an unreachable address locks
        # the account out of password reset.
        if " " in email or email.count("@") != 1 or not all(email.split("@")):
            raise ProfileError("That doesn't look like an email address.")
        if not is_ascii_email(email):
            raise ProfileError(EMAIL_ASCII_MESSAGE)
        previous = _clean(user.email)
        if email.casefold() != previous.casefold():
            logger.info("Profile email change user_id=%s", user.id)
            if _taken("email", email, user.id):
                raise ProfileError(TAKEN)
            if _clean(user.username).casefold() == previous.casefold():
                if _taken("username", email, user.id):
                    raise ProfileError(TAKEN)
                user.username = email
                changed.append("username")
            user.email = email
            changed.append("email")
    for name in ("first_name", "last_name"):
        if name in fields:
            setattr(user, name, _clean(fields[name]))
            changed.append(name)
    if not changed:
        return
    try:
        with transaction.atomic():
            user.save(update_fields=changed)
    except IntegrityError:
        logger.warning("Profile update hit a unique conflict user_id=%s", user.id)
        raise ProfileError(TAKEN) from None
    logger.info("Profile updated user_id=%s fields=%s", user.id, sorted(fields))


def my_profile(request: HttpRequest) -> HttpResponse:
    early, user = guard(request)
    if early is not None:
        return early
    if request.method == "PATCH":
        try:
            body = json.loads(request.body or b"null")
        except (ValueError, UnicodeDecodeError):
            body = None
        fields = {k: body[k] for k in PATCHABLE if k in body} if isinstance(body, dict) else {}
        if not fields or not all(isinstance(v, str) for v in fields.values()):
            return refuse(request, SAVE_FAILED, 400)
        try:
            update_profile(user, **fields)
        except ProfileError as exc:
            return refuse(request, str(exc), 422)
        except Exception:
            logger.exception("Profile save failed for user=%s", user.id)
            return refuse(request, SAVE_FAILED, 500)
    elif request.method != "GET":
        return refuse(request, "Method not allowed", 405)
    entity = None
    entity_id = (request.GET.get("entity") or "").strip()
    if entity_id:
        entity = entity_context(user, entity_id)
        if entity is None:
            return refuse(request, "You don't have access to that company.", 403)
    return respond(request, profile_payload(user, entity))
