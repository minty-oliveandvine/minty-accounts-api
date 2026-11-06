"""The published legal documents' manifest - Flask's ``legal/registry.py`` copied unchanged (Phase 5 of the
Flask retirement, 2026-11-06): it is plain Python, and Flask and this service read the same
``terms_consent`` rows and versions while both run. Its own notes follow.


A MANIFEST, NOT THE DOCUMENTS
-----------------------------
The documents themselves left this app on 2026-10-12. The `.md` files, the
Markdown renderer and the public pages now live in **minty-web**
(`minty-web/lib/legal/`, served at `/terms` and `/privacy`), which is where
anyone reads them. What stays here is the table below: which versions exist,
when each took effect, and what each one's SHA-256 is.

The split: **this app names the version; minty-web supplies the text.** This
side owns the record (`terms_consent`) and therefore has to own the version a
consent row may name, and the fingerprint stamped into it. If both apps decided
which version was live, a deploy skew would refuse sign-ups.

WHY A FINGERPRINT
-----------------
Recording "this person accepted beta-1" proves they clicked a button with a
label on it. It does not prove what the document said. So every consent row
also stores the SHA-256 of the exact wording shown, and this table is where
that number comes from — never the client, which sends one only so it can be
COMPARED (`blueprints/legal/routes/hub.py`).

The hash is taken over the `.md`, never a PDF: PDF writers embed a creation
timestamp, so building the same PDF twice yields two different fingerprints.
It is computed over text normalised to `\\n` endings with trailing whitespace
stripped and one trailing newline — part of the definition of the fingerprint,
not an implementation detail, and minty-web's
`lib/legal/registry.server.ts::normalise` is the same function. It must not
change once anything is published.

KEEPING THE TWO IN STEP
-----------------------
`MANIFEST` and minty-web's `PINNED_HASHES` are the same table, and a version
added or repinned in one must be changed in the other in the same breath.
`verify_manifest()` can no longer check a hash against a file — there is no
file here any more. What catches a divergence:

  1. minty-web's `lib/legal/__tests__/registry.test.ts` — re-hashes every `.md`
     and holds `c90a335e…` as a frozen literal.
  2. `tests/test_legal_manifest.py` — this table's invariants.
  3. `verify_manifest()` at startup — a live version with no manifest entry.
  4. The stack e2e job — the two RUNNING services compared.
  5. `POST /api/me/terms/accept`'s 409 `document_mismatch` — the last line.

A version whose `pinned` is False is still a DRAFT: its wording is not final
and it shows a draft notice. Pin it (and only then) when the wording is final.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("minty-accounts-api")

TERMS = "terms"
PRIVACY = "privacy"
DOCUMENT_KINDS = (TERMS, PRIVACY)

# The single setting that drives who gets asked to agree. Changing it moves
# every user from AGREED_CURRENT to OUT_OF_DATE at once — that is the whole
# mechanism for a Terms revamp, so change it only for meaningful revisions.
# A typo fix gets a new file and leaves this alone.
#
# minty-web has the same two settings, for which version its `/terms` page
# shows. Change them together — this one decides which version a consent row
# may name, and a skew means nobody can agree (minty-web's gate fails open and
# says so loudly rather than record an agreement to wording nobody read).
CURRENT_TERMS_VERSION = os.environ.get("CURRENT_TERMS_VERSION", "beta-1")
CURRENT_PRIVACY_VERSION = os.environ.get("CURRENT_PRIVACY_VERSION", "beta-1")

# Whether an unpinned (draft) document shows its "this wording is not final"
# banner on the acceptance panel.
#
# DEFAULTS TO ON, and should stay on anywhere real people are asked to agree.
# The opt-out exists so a draft can be hidden for demos and screenshots without
# PINNING the hash, which would assert the wording is final. terms/beta-1 is
# pinned since 2026-09-18 and no longer shows the banner; privacy/beta-1 is
# still a placeholder draft and does.
#
# Because it defaults to on, an environment that simply does not set it — a
# fresh production deploy, say — still warns. Hiding the banner has to be a
# deliberate act, recorded in that environment's config.
SHOW_DRAFT_BANNER = os.environ.get(
    "LEGAL_SHOW_DRAFT_BANNER", "1"
).strip().lower() not in {"0", "false", "no", "off"}

# Whether sign-up REFUSES to create an account without agreement.
#
# ON as of Phase 6. A sign-up that does not carry an explicit agreement to the
# live version is rejected with 400 — no account, no consent row.
#
# *** DEPLOY ORDER STILL MATTERS ***
#
# Minty and the apps that sign people up release separately. If this is on in
# Flask while minty-web is still running a bundle that does not send
# `terms_accepted`, EVERY sign-up through it fails. The order is: ship Flask
# accepting the fields, ship the frontend sending them, confirm both are live,
# and only then deploy with this on.
#
# Set REQUIRE_TERMS_AT_SIGNUP=false to fall back to the rollout behaviour
# without a code change — sign-up succeeds and the person meets the acceptance
# gate at their next request instead. That is the lever to pull if sign-ups
# start failing after a deploy.
REQUIRE_TERMS_AT_SIGNUP = (
    os.environ.get("REQUIRE_TERMS_AT_SIGNUP", "true").strip().lower() == "true"
)


@dataclass(frozen=True)
class ManifestEntry:
    """One published version, as this app knows it — everything but the words.

    Deliberately keeps the attribute names the old `LegalDocument` had, minus
    `text` and `html`, so the gate and the hub API read the same as before.
    """

    kind: str
    version: str
    #: SHA-256 of the normalised `.md`. MANDATORY for `terms` — it is what goes
    #: into `terms_consent.document_hash`.
    sha256: str | None
    effective_date: str | None
    is_pinned: bool

    @property
    def show_draft_notice(self) -> bool:
        """Whether the acceptance panel should warn that this is a draft.

        Kept as a property rather than a second condition at the call site, so
        every reader asks one question and they cannot disagree about it.
        """
        return not self.is_pinned and SHOW_DRAFT_BANNER


# Every published version. Per version:
#
#   sha256         the fingerprint of the normalised `.md` held by minty-web.
#                  Mandatory for `terms`: it is the number recorded against a
#                  consent, and a version with none could not be agreed to.
#                  `privacy` may be None — no consent is ever recorded for the
#                  Privacy Policy, and minty-web's own registry supplies the
#                  fingerprint its `/privacy` page prints.
#   effective_date exactly as printed in the document header (every surface
#                  renders the string verbatim). None where a version has no
#                  recorded date, and `GET /legal/current` reports null rather
#                  than inventing one.
#   pinned         whether the wording is final. False == draft.
#
# terms/beta-1 was pinned on 2026-09-18, in the commit that filled the header
# date and the §23 contact email and address. From here on its wording is
# final: a change of any kind — even a typo — is a new file (`beta-2.md` in
# minty-web), never an edit of the existing one, or every consent row pointing
# at it looks tampered with.
#
# privacy/beta-1 is UNPINNED on purpose: it is a placeholder awaiting the
# actual policy text (runbook open decision #1).
MANIFEST: dict[tuple[str, str], ManifestEntry] = {
    (TERMS, "beta-1"): ManifestEntry(
        kind=TERMS,
        version="beta-1",
        sha256="c90a335ed1b5b8cf5d8f5dc6702117418878594053ef3c0720b7816182c7c438",
        effective_date="18 September 2026",
        is_pinned=True,
    ),
    (PRIVACY, "beta-1"): ManifestEntry(
        kind=PRIVACY,
        version="beta-1",
        sha256=None,
        effective_date=None,
        is_pinned=False,
    ),
}


def get_version(kind: str, version: str) -> ManifestEntry | None:
    return MANIFEST.get((kind, version))


def current_version(kind: str) -> str:
    return CURRENT_TERMS_VERSION if kind == TERMS else CURRENT_PRIVACY_VERSION


def get_current(kind: str) -> ManifestEntry | None:
    return get_version(kind, current_version(kind))


def all_versions(kind: str) -> list[str]:
    return sorted(v for (k, v) in MANIFEST if k == kind)


def previous_version(kind: str, version: str) -> str | None:
    """The version published before `version`, or None if it is the first."""
    versions = all_versions(kind)
    if version not in versions:
        return None
    index = versions.index(version)
    return versions[index - 1] if index > 0 else None


def verify_manifest() -> list[str]:
    """Check the manifest's own invariants. Returns the problems.

    Called at startup. It CANNOT verify the wording — the files are minty-web's
    (see the module docstring), and a document edited in place is caught by
    minty-web's tests and by the stack e2e job, not here. What it does catch is
    a live version this app has no entry for, which would make the Terms
    unagreeable, and a `terms` version with no fingerprint to record.
    """
    problems: list[str] = []

    for (kind, version), entry in MANIFEST.items():
        if kind == TERMS and not entry.sha256:
            problems.append(
                f"{kind}/{version}: no fingerprint — a consent for it could not be recorded"
            )
        if not entry.is_pinned:
            logger.warning(
                f"Legal: {kind}/{version} is UNPINNED (draft). "
                "Pin it here and in minty-web's lib/legal/registry.server.ts "
                "before this version is published."
            )

    # A live version with no entry at all: the acceptance panel has nothing to
    # ask for, and the gate lets everyone through (it fails open).
    for kind in DOCUMENT_KINDS:
        if get_current(kind) is None:
            problems.append(
                f"{kind}: CURRENT version {current_version(kind)!r} is not in the manifest"
            )

    for problem in problems:
        logger.error(f"Legal: {problem}")
    return problems
