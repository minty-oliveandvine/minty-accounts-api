"""A company's address: ``/entity/<shortid>/<name>/...`` - Flask's ``blueprints/shared/entity_ref.py``
(Phase 6 of the Flask retirement, 2026-11-06).

``<shortid>`` is the first 8 characters of the company's id and decides; ``<name>`` is its name as
a readable segment (``slugify_name``). A full uuid is accepted too. ``resolve`` never answers "no
such company" itself: an unknown address resolves to a uuid no company has, and the usual sign-in
and membership checks answer, so an address tells a stranger nothing. A GET that named a company
by a full uuid, an old name or capitals is 308'd to the canonical address (``canonical_redirect``).
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid

from django.http import HttpRequest, HttpResponsePermanentRedirect
from django.urls import register_converter, reverse
from minty_db.models import Entity

logger = logging.getLogger("minty-accounts-api")

SHORT_ID_LENGTH = 8
SLUG_MAX = 60
EMPTY_SLUG = "company"
NIL_UUID = "00000000-0000-0000-0000-000000000000"
_HEX8 = "[0-9a-fA-F]{8}"
_FULL_UUID = _HEX8 + "-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def slugify_name(name: str | None) -> str:
    """Lowercase, ``&`` as "and", letters and digits of any script kept, anything else one ``-``."""
    text = unicodedata.normalize("NFKC", name or "").lower().replace("&", " and ")
    slug = "".join(ch if ch.isalnum() else "-" for ch in text)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:SLUG_MAX].rstrip("-")
    return slug or EMPTY_SLUG


def _as_uuid(value) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


class ResolvedEntityId(str):
    """The company's uuid as a plain ``str``, carrying whether its address was canonical."""

    canonical: bool

    def __new__(cls, value: str, *, canonical: bool):
        obj = super().__new__(cls, value)
        obj.canonical = canonical
        return obj


def canonical_ref(entity_id) -> str:
    """``<shortid>/<slug>`` for a company's uuid; anything else is written as it is."""
    full = _as_uuid(entity_id)
    if full is None:
        return str(entity_id)
    name = Entity.objects.filter(id=full).values_list("name", flat=True).first()
    if name is None and not Entity.objects.filter(id=full).exists():
        return full
    return f"{full[:SHORT_ID_LENGTH]}/{slugify_name(name)}"


def company_path(entity_id, sub: str = "") -> str:
    """``/entity/<shortid>/<slug>[/<sub>]``."""
    return f"/entity/{canonical_ref(entity_id)}" + (f"/{sub.lstrip('/')}" if sub else "")


def resolve(value: str) -> ResolvedEntityId:
    if "/" not in value:
        full = _as_uuid(value) or NIL_UUID
        return ResolvedEntityId(full, canonical=not Entity.objects.filter(id=full).exists())
    short_id, slug = value.split("/", 1)
    short_id = short_id.lower()
    low, high = f"{short_id}-0000-0000-0000-000000000000", f"{short_id}-ffff-ffff-ffff-ffffffffffff"
    if _as_uuid(low) is None:
        return ResolvedEntityId(NIL_UUID, canonical=True)
    matches = [(str(i), n or "") for i, n in Entity.objects.filter(id__gte=low, id__lte=high).values_list("id", "name")]
    if len(matches) > 1:
        same = [m for m in matches if slugify_name(m[1]) == slug]
        if len(same) != 1:
            logger.error("company address /entity/%s/%s is ambiguous: %s companies share the short id",
                         short_id, slug, len(matches))
            return ResolvedEntityId(NIL_UUID, canonical=True)
        matches = same
    if not matches:
        return ResolvedEntityId(NIL_UUID, canonical=True)
    full, name = matches[0]
    return ResolvedEntityId(full, canonical=slug == slugify_name(name) and value.split("/", 1)[0] == full[:8])


class EntityRefConverter:
    """``<entity:...>``: ``<8 hex>/<slug>`` or a full uuid (the slug is required in the short form,
    so ``/entity/<shortid>/reports`` is never a company called "reports")."""

    regex = f"(?:{_FULL_UUID}|{_HEX8}/[^/]+)"

    def to_python(self, value: str) -> ResolvedEntityId:
        return resolve(value)

    def to_url(self, value) -> str:
        return canonical_ref(value)


register_converter(EntityRefConverter, "entity")


def canonical_redirect(request: HttpRequest, view_name: str, **kwargs) -> HttpResponsePermanentRedirect | None:
    """A 308 to the canonical address when a GET named a company any other way, query kept."""
    if request.method not in ("GET", "HEAD"):
        return None
    if all(getattr(v, "canonical", True) for v in kwargs.values()):
        return None
    target = reverse(view_name, kwargs={k: str(v) for k, v in kwargs.items()})
    if request.META.get("QUERY_STRING"):
        target += "?" + request.META["QUERY_STRING"]
    response = HttpResponsePermanentRedirect(target)
    response.status_code = 308
    return response
