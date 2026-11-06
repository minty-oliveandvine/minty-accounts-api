"""Rules the hub's routes share - Flask's ``blueprints/shared/email_rules.py`` and the role labels of
``blueprints/shared/enums.py`` (Phase 5 of the Flask retirement, 2026-11-06).

Email addresses are English only (printable ASCII, the owner's call 2026-10-01): every path that
writes or keys on a typed address refuses anything else, in the same words.
"""

from __future__ import annotations

import re

EMAIL_ASCII_MESSAGE = "Email can only contain English letters, numbers and symbols."
EMAIL_FORMAT_MESSAGE = "That doesn't look like an email address."
EMAIL_MAX_LENGTH = 150
_ADDRESS = re.compile(r"^[^@\s<>\"'`()\[\],;:\\]+@[^@\s<>\"'`()\[\],;:\\]+\.[^@\s<>\"'`()\[\],;:\\]+$")

#: The on-screen name of each stored role.
ROLE_LABELS = {
    "entity_base": "Member",
    "cashier": "Cashier",
    "shop_manager": "Shop Manager",
    "accountant": "Accountant",
    "admin": "Admin",
    "super_admin": "Super Admin",
}


def is_ascii_email(value: str | None) -> bool:
    """Every character (trimmed) printable ASCII; empty passes (each caller has its own "required")."""
    return all("\x21" <= ch <= "\x7e" for ch in (value or "").strip())


def invite_address_error(value: str | None) -> str | None:
    """The sentence refusing ``value`` as an address to invite, or None when it is one."""
    address = (value or "").strip()
    if not is_ascii_email(address):
        return EMAIL_ASCII_MESSAGE
    if len(address) > EMAIL_MAX_LENGTH or not _ADDRESS.match(address):
        return EMAIL_FORMAT_MESSAGE
    return None


def role_label(role) -> str | None:
    """A stored role's on-screen name, or None for no role or an unknown one."""
    return ROLE_LABELS.get(str(role)) if role else None
