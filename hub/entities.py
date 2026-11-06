"""``GET /api/me/entities`` - the companies a person may pick (minty-web's entity list). Flask's
``entity/routes/me_api.py`` and ``entity/services/entity_list.py`` ``build_entity_list`` (Phase 5
of the Flask retirement, 2026-11-06).

"Setup in progress" first, then most recently opened (never opened last). A superuser sees every
company, entering those they hold no membership on read-only. Each entry: ``id``, ``name``,
``status``, ``modules`` (switched on - fail closed: on only where the company's map row says so),
``trial_modules`` / ``trial_module_names`` (on a free trial, among ``modules``),
``last_accessed_at`` (ISO, UTC), ``last_accessed_by``, ``role`` (the caller's own, or
``superadmin``), ``trial_ends_on`` (the earliest trial end, on the company's calendar) and
``payment_failed``.

The trials and payment-failed flags are minty-subscription-api's answer
(``core/subscription_client.attention``); when it can't answer quickly the list comes without
them (logged), never without the list. ``?flash=`` comes back as ``notices``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Case, IntegerField, Value, When
from django.db.models import F as Field
from django.http import HttpRequest, HttpResponse
from minty_db.enums import SystemRole
from minty_db.models import Entity, EntityFunction, EntityFunctionMap, User, UserEntity

from core import subscription_client
from hub.notices import read_notices
from hub.policy import is_superuser
from hub.transport import guard, refuse, respond

logger = logging.getLogger("minty-accounts-api")

#: The modules a company can hold (``entity_function.function_code``).
MODULE_CODES = ("PETTY_CASH", "PAYMENT_REQUEST")
#: A company's calendar when it names no zone: Hong Kong, as the subscription emails date it.
DEFAULT_ZONE = "Asia/Hong_Kong"


def _modules(entity_ids: list) -> dict[str, set[str]]:
    """Each company's switched-on modules: a map row with ``is_enabled``; no row is off."""
    codes = dict(EntityFunction.objects.filter(function_code__in=MODULE_CODES).values_list("id", "function_code"))
    found: dict[str, set[str]] = {str(e): set() for e in entity_ids}
    for entity_id, function_id in EntityFunctionMap.objects.filter(
            entity_id__in=entity_ids, entity_function_id__in=list(codes), is_enabled=True,
    ).values_list("entity_id", "entity_function_id"):
        found[str(entity_id)].add(str(codes[function_id]))
    return found


def _display_names(codes: set[str]) -> dict[str, str]:
    if not codes:
        return {}
    return {str(code): name or str(code) for code, name in
            EntityFunction.objects.filter(function_code__in=codes).values_list("function_code", "function_name")}


def _company_day(iso: str, zone_name: str | None) -> str:
    """An instant as the day on the company's calendar (``entities.timezone``, else Hong Kong)."""
    instant = datetime.fromisoformat(iso)
    zone = ZoneInfo(DEFAULT_ZONE)
    if zone_name and zone_name.strip():
        try:
            zone = ZoneInfo(zone_name.strip())
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("Entity list: unknown time zone %r; dated in %s", zone_name, DEFAULT_ZONE)
    return instant.astimezone(zone).date().isoformat()


def build_entity_list(user: User) -> list[dict]:
    query = Entity.objects.all()
    if not is_superuser(user):
        query = query.filter(id__in=UserEntity.objects.filter(user_id=user.id).values("entity_id"))
    rows = list(query.annotate(
        setup_first=Case(When(status="onboarding", then=Value(0)), default=Value(1), output_field=IntegerField()),
    ).order_by("setup_first", Field("last_accessed_at").desc(nulls_last=True)))
    if not rows:
        return []
    ids = [r.id for r in rows]
    roles = dict(UserEntity.objects.filter(user_id=user.id, entity_id__in=ids).values_list("entity_id", "role"))
    accessors = {u.id: u for u in User.objects.filter(id__in={r.last_accessed_by_user_id for r in rows
                                                              if r.last_accessed_by_user_id})}
    modules = _modules(ids)
    attention = subscription_client.attention(ids)
    labels = _display_names({code for flags in attention.values() for code in (flags.get("trials") or {})})

    entries = []
    for row in rows:
        on = modules.get(str(row.id), set())
        flags = attention.get(str(row.id)) or {}
        trials = {code: end for code, end in (flags.get("trials") or {}).items() if code in on}
        accessor = accessors.get(row.last_accessed_by_user_id)
        role = roles.get(row.id)
        last = row.last_accessed_at.astimezone(UTC) if row.last_accessed_at else None
        entries.append({
            "id": str(row.id),
            "name": row.name,
            "status": str(row.status) if row.status is not None else None,
            "modules": sorted(on),
            "trial_modules": sorted(trials),
            "trial_module_names": sorted(labels.get(code, code) for code in trials),
            "last_accessed_at": last.isoformat() if last else None,
            "last_accessed_by": f"{accessor.first_name or ''} {accessor.last_name or ''}".strip() or None
            if accessor else None,
            "role": str(role) if role else SystemRole.SUPERADMIN.value,
            "trial_ends_on": _company_day(min(trials.values(), key=datetime.fromisoformat), row.timezone)
            if trials else None,
            "payment_failed": bool(flags.get("payment_failed")),
        })
    return entries


def my_entities(request: HttpRequest) -> HttpResponse:
    early, user = guard(request)
    if early is not None:
        return early
    try:
        entities = build_entity_list(user)
    except Exception:
        logger.exception("Entity list API failed for user=%s", user.id)
        return refuse(request, "Your companies didn't load. Mind trying again?", 500)
    return respond(request, {"entities": entities, "notices": read_notices(request.GET.get("flash"))})
