"""Signing in with an emailed code - Flask's ``blueprints/auth/routes/email_auth.py`` and
``services/email_auth.py`` (Phase 6 of the Flask retirement, 2026-11-06).

1. ``POST /auth/email/request-code`` ``{email, mode?, invite?}``: a 6-digit code, valid 60 s, with a
   60 s resend cooldown. Failed tries carry across resends; at 5 the address is locked for 15
   minutes. In login mode an unknown address is refused (404) before a code goes out. A code whose
   email fails to send is rolled back (400) so the person can retry at once.
2. ``POST /auth/email/verify-code`` ``{email, code, invite?, first_name?, last_name?, next?,
   terms_accepted?, terms_version?}``: signs an existing person in, or makes the account for a new
   address that came with a name (with its Terms agreement in the same transaction). The answer
   carries a hand-off URL.
3. ``GET /auth/email/handoff?h=``: the same-origin landing that sets the session cookie.
   **Single use (gap 1, 2026-11-06):** the verify step parks the hand-off as a pending session row
   (5 minutes) and the URL carries its key, signed; the landing deletes that row and signs in only
   when the delete removed it - a replay, a second tab or a copied link finds nothing.

The routes are cross-origin JSON with no cookie (the sign-in page sends ``credentials: "omit"``):
open CORS, no CSRF, as in Flask. Codes are stored as werkzeug's ``pbkdf2:sha256`` hashes, so a code
Flask issued verifies here and back while both run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core import signing
from django.core.mail import EmailMessage
from django.db import transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, HttpResponseRedirect
from minty_db.models import EmailOtp, User

from core.flask_json import flask_json
from hub import invitations, registry
from hub.public import _open_cors
from hub.rules import EMAIL_ASCII_MESSAGE, is_ascii_email
from hub.session import flash, safe_internal_path, sign_in
from hub.terms import record_consent

logger = logging.getLogger("minty-accounts-api")

OTP_EXPIRY_SECONDS = 60
MAX_OTP_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60
LOCKOUT_MINUTES = 15
HANDOFF_SALT = "auth-email-handoff"
HANDOFF_MAX_AGE = 5 * 60
PENDING_KEY = "pending_handoff"
SOURCE_SIGNUP_OTP = "signup_otp"
SOURCE_SIGNUP_INVITE = "signup_invite"
PBKDF2_ITERATIONS = 600_000


# ---- codes --------------------------------------------------------------------------------------


def hash_code(code: str) -> str:
    """werkzeug's ``generate_password_hash(code, method="pbkdf2:sha256")`` format."""
    salt = secrets.token_urlsafe(12)[:16]
    digest = hashlib.pbkdf2_hmac("sha256", code.encode(), salt.encode(), PBKDF2_ITERATIONS).hex()
    return f"pbkdf2:sha256:{PBKDF2_ITERATIONS}${salt}${digest}"


def check_code(stored: str, code: str) -> bool:
    """werkzeug's ``check_password_hash`` for ``pbkdf2:<hash>[:<iterations>]$salt$hex``."""
    try:
        method, salt, digest = stored.split("$", 2)
        kind, algorithm, *rest = method.split(":")
        if kind != "pbkdf2":
            return False
        iterations = int(rest[0]) if rest else 600_000
        computed = hashlib.pbkdf2_hmac(algorithm, code.encode(), salt.encode(), iterations).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(computed, digest)


def resolve_user_by_email(address: str | None) -> User | None:
    """The one person owning ``address`` on either identity column (``email`` or ``xero_email``)."""
    normalized = (address or "").strip().lower()
    if not normalized:
        return None
    return (User.objects.annotate(_email=Lower("email"), _xero=Lower("xero_email"))
            .filter(Q(_email=normalized) | Q(_xero=normalized)).first())


def _lock_remaining(otp: EmailOtp | None) -> int:
    if otp is None or otp.attempts < MAX_OTP_ATTEMPTS or not otp.created_at:
        return 0
    remaining = (otp.created_at + timedelta(minutes=LOCKOUT_MINUTES) - datetime.now(UTC)).total_seconds()
    return int(remaining) if remaining > 0 else 0


def _locked_sentence(seconds: int) -> str:
    return f"Too many attempts. Try again in about {max(1, (seconds + 59) // 60)} minute(s)."


def _latest(email: str) -> EmailOtp | None:
    return EmailOtp.objects.filter(email=email).order_by("-created_at").first()


class _SendFailed(Exception):
    pass


def request_email_otp(email: str) -> tuple[bool, str | None]:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False, "A valid email is required."
    if not is_ascii_email(email):
        return False, EMAIL_ASCII_MESSAGE
    latest = _latest(email)
    if locked := _lock_remaining(latest):
        return False, _locked_sentence(locked)
    if latest and (datetime.now(UTC) - latest.created_at).total_seconds() < RESEND_COOLDOWN_SECONDS:
        return False, "Please wait a moment before requesting another code."
    prior = latest.attempts if latest else 0
    code = f"{secrets.randbelow(1000000):06d}"
    try:
        with transaction.atomic():
            EmailOtp.objects.filter(email=email).delete()
            EmailOtp.objects.create(email=email, code_hash=hash_code(code), attempts=0 if prior >= MAX_OTP_ATTEMPTS
                                    else prior, expires_at=datetime.now(UTC) + timedelta(seconds=OTP_EXPIRY_SECONDS))
            if not _send_code_email(email, code):
                raise _SendFailed
    except _SendFailed:
        return False, "I couldn't send your sign-in code just now. Mind trying again in a moment?"
    return True, None


def verify_email_otp(email: str, code: str) -> tuple[User | None, str | None, bool]:
    """``(user_or_None, error, locked)``; ``(None, None, False)`` = a verified new address."""
    email, code = (email or "").strip().lower(), (code or "").strip()
    if not email or not code:
        return None, "Email and code are required.", False
    otp = _latest(email)
    if otp is None:
        return None, "No code was requested for this email.", False
    if locked := _lock_remaining(otp):
        return None, _locked_sentence(locked), True
    if otp.attempts >= MAX_OTP_ATTEMPTS:
        EmailOtp.objects.filter(id=otp.id).update(attempts=0)
        otp.attempts = 0
    if otp.expires_at and otp.expires_at < datetime.now(UTC):
        return None, "This code has expired. Please request a new one.", False
    if otp.verified_at is not None:
        return None, "This code has already been used. Please request a new one.", False
    if not check_code(otp.code_hash, code):
        otp.attempts += 1
        EmailOtp.objects.filter(id=otp.id).update(attempts=otp.attempts)
        if locked := _lock_remaining(otp):
            return None, _locked_sentence(locked), True
        return None, "Invalid code.", False
    # Single use: only the request that flips verified_at from NULL gets through.
    if not EmailOtp.objects.filter(id=otp.id, verified_at__isnull=True).update(verified_at=datetime.now(UTC),
                                                                               attempts=0):
        return None, "This code has already been used. Please request a new one.", False
    return resolve_user_by_email(email), None, False


def _send_code_email(email: str, code: str) -> bool:
    message = EmailMessage(subject="Your Minty sign-in code", from_email=settings.INVITE_FROM, to=[email],
                           body=_code_email_html(code))
    message.content_subtype = "html"
    try:
        message.send()
    except Exception as exc:  # noqa: BLE001 - the caller rolls back and says so
        logger.error("Failed to send the sign-in code for an address: %s", exc)
        return False
    logger.info("Sign-in code sent")
    return True


def _code_email_html(code: str) -> str:
    return f"""\
<!DOCTYPE html><html><body style="margin:0;padding:40px 0;font-family:Arial,Helvetica,sans-serif;background:#f0f4f8;">
  <table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
    <table width="460" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:16px;padding:36px 40px;box-shadow:0 4px 24px rgba(0,0,0,.08);">
      <tr><td style="text-align:center;">
        <h1 style="color:#2d3748;font-size:20px;margin:0 0 8px;">Your sign-in code</h1>
        <p style="color:#718096;font-size:14px;margin:0 0 24px;">Enter this code to continue to Minty.</p>
        <div style="font-size:34px;font-weight:700;letter-spacing:8px;color:#3BB8BF;">{code}</div>
        <p style="color:#a0aec0;font-size:12px;margin:24px 0 0;">This code expires in {OTP_EXPIRY_SECONDS} seconds. If you didn't request it, you can ignore this email.</p>
      </td></tr>
    </table>
  </td></tr></table>
</body></html>"""


# ---- the single-use hand-off --------------------------------------------------------------------


def mint_handoff_url(request: HttpRequest, user_id, invite: str = "", next_path: str = "") -> str:
    pending = SessionStore()
    pending[PENDING_KEY] = {"user_id": str(user_id), "invite": invite, "next": next_path}
    pending.set_expiry(HANDOFF_MAX_AGE)
    pending.create()
    query = urlencode({"h": signing.dumps(pending.session_key, salt=HANDOFF_SALT)})
    base = settings.ACCOUNTS_API_URL or request.build_absolute_uri("/").rstrip("/")
    return f"{base}/auth/email/handoff?{query}"


def redeem_handoff(token: str) -> dict | None:
    """The parked hand-off, once: None for a missing, forged, expired or already used token."""
    try:
        key = signing.loads(token, salt=HANDOFF_SALT, max_age=HANDOFF_MAX_AGE)
    except signing.SignatureExpired:
        logger.warning("Handoff: token expired (older than %ss)", HANDOFF_MAX_AGE)
        return None
    except signing.BadSignature:
        logger.warning("Handoff: bad token signature")
        return None
    data = SessionStore(session_key=str(key)).load().get(PENDING_KEY)
    deleted, _ = Session.objects.filter(session_key=str(key)).delete()
    if not data or not deleted:
        logger.warning("Handoff: the hand-off was already used or has lapsed")
        return None
    return data


# ---- routes -------------------------------------------------------------------------------------


def _answer(request: HttpRequest, payload: dict, status: int = 200) -> HttpResponse:
    return _open_cors(request, flask_json(payload, status))


def _error(request: HttpRequest, message: str, status: int) -> HttpResponse:
    return _answer(request, {"status": "error", "message": message}, status)


def _body(request: HttpRequest) -> dict:
    try:
        data = json.loads(request.body or b"null")
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _text(data: dict, key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def _post_only(request: HttpRequest) -> HttpResponse | None:
    if request.method == "OPTIONS":
        return _open_cors(request, HttpResponse(status=200))
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    return None


def email_request_code(request: HttpRequest) -> HttpResponse:
    if (early := _post_only(request)) is not None:
        return early
    data = _body(request)
    email = _text(data, "email").lower()
    if not is_ascii_email(email):
        return _error(request, EMAIL_ASCII_MESSAGE, 400)
    if data.get("mode") == "login" and ("@" not in email or resolve_user_by_email(email) is None):
        return _error(request, "Please sign up first", 404)
    if (invite := _text(data, "invite")) and (problem := invitations.invite_error_for_email(invite, email)):
        return _error(request, problem, 400)
    ok, error = request_email_otp(email)
    if not ok:
        return _error(request, error or "Could not send a code.", 400)
    return _answer(request, {"status": "success", "message": "A code has been sent to your email."})


def _terms_for_signup(data: dict) -> tuple[str | None, str | None]:
    """``(version_to_record, refusal)``: only the LIVE version, ticked, is ever recorded."""
    accepted = data.get("terms_accepted") is True
    submitted = _text(data, "terms_version")
    live = registry.current_version(registry.TERMS)
    if accepted and submitted == live:
        return live, None
    if accepted and submitted and submitted != live:
        logger.warning("Sign-up agreed to Terms %r but %r is live; not recording", submitted, live)
        if registry.REQUIRE_TERMS_AT_SIGNUP:
            return None, ("Our Terms of Use were updated while you were signing up. "
                          "Please refresh the page and read the new version.")
        return None, None
    if registry.REQUIRE_TERMS_AT_SIGNUP:
        return None, "You must accept the Terms of Use to create an account."
    return None, None


def _create_passwordless_user(request: HttpRequest, email: str, first_name: str, last_name: str,
                              source: str | None, version: str | None) -> User | None:
    """A new approved, passwordless account (its Terms agreement in the same transaction)."""
    try:
        with transaction.atomic():
            existing = resolve_user_by_email(email) or User.objects.filter(username=email).first()
            if existing is not None:
                return existing
            user = User.objects.create(id=uuid.uuid4(), email=email, username=email, first_name=first_name,
                                       last_name=last_name, password=hash_code(secrets.token_urlsafe(32)),
                                       system_role="normal", approved=True)
            if source and not record_consent(request, user.id, version, source=source):
                raise RuntimeError("the Terms agreement could not be recorded")
    except Exception as exc:  # noqa: BLE001 - answered as a refusal, logged
        logger.error("Failed to create a passwordless user: %s", exc)
        return None
    logger.info("Created passwordless User %s%s", user.id, f" with Terms consent ({source})" if source else "")
    return user


def email_verify_code(request: HttpRequest) -> HttpResponse:
    if (early := _post_only(request)) is not None:
        return early
    data = _body(request)
    invite, first_name, last_name = _text(data, "invite"), _text(data, "first_name"), _text(data, "last_name")
    email = _text(data, "email").lower()
    next_path = safe_internal_path(data.get("next") if isinstance(data.get("next"), str) else None) or ""
    user, error, locked = verify_email_otp(email, _text(data, "code"))
    if error:
        return _error(request, error, 429 if locked else 400)
    if user is not None:
        if invite and (problem := invitations.invite_error_for_email(invite, email)):
            return _error(request, problem, 400)
        if not user.approved and not invite:
            return _error(request, "Your account isn't approved just yet — hang tight!", 403)
        version, _ = _terms_for_signup(data)
        if version:
            try:
                record_consent(request, user.id, version, source=SOURCE_SIGNUP_INVITE if invite else SOURCE_SIGNUP_OTP)
            except Exception as exc:  # noqa: BLE001 - never block a valid sign-in
                logger.warning("Could not record login-time Terms consent for %s: %s", user.id, exc)
        url = mint_handoff_url(request, user.id, invite=invite, next_path=next_path)
        return _answer(request, {"status": "success", "action": "login", "redirect_url": url})
    if invite:
        if not (first_name and last_name):
            return _error(request, "Please contact your inviter.", 400)
        version, refusal = _terms_for_signup(data)
        if refusal:
            return _error(request, refusal, 400)
        new = _create_passwordless_user(request, email, first_name, last_name,
                                        SOURCE_SIGNUP_INVITE if version else None, version)
        if new is None:
            return _error(request, "Could not create your account. Please contact your inviter.", 400)
        url = mint_handoff_url(request, new.id, invite=invite)
        return _answer(request, {"status": "success", "action": "login", "redirect_url": url})
    if not (first_name and last_name):
        return _error(request, "No account found for this email — please sign up.", 404)
    version, refusal = _terms_for_signup(data)
    if refusal:
        return _error(request, refusal, 400)
    new = _create_passwordless_user(request, email, first_name, last_name,
                                    SOURCE_SIGNUP_OTP if version else None, version)
    if new is None:
        return _error(request, "Could not create your account. Please try again.", 400)
    url = mint_handoff_url(request, new.id, next_path=next_path)
    return _answer(request, {"status": "success", "action": "login", "redirect_url": url})


def email_handoff(request: HttpRequest) -> HttpResponse:
    """Sign in on this origin, then the invited company, the signed ``next``, or the company list."""
    from hub.addresses import company_path

    if request.method not in ("GET", "HEAD"):
        return HttpResponseNotAllowed(["GET"])
    home = HttpResponseRedirect("/")
    token = (request.GET.get("h") or "").strip()
    payload = redeem_handoff(token) if token else None
    if payload is None:
        return home
    user = User.objects.filter(id=payload.get("user_id")).first() if payload.get("user_id") else None
    if user is None:
        logger.warning("Handoff: no user for the parked id; to sign-in")
        return home
    if invite := (payload.get("invite") or "").strip():
        entity_id, problem = invitations.accept_invitation(invite, user.id)
        if problem:
            logger.error("Handoff: invite acceptance failed for user %s: %s", user.id, problem)
            flash(request, problem, "danger")
            return home
        user.refresh_from_db()
        sign_in(request, user)
        logger.info("Handoff: user %s accepted an invite to entity %s", user.id, entity_id)
        return HttpResponseRedirect(company_path(entity_id, "petty-cash"))
    if not user.approved:
        logger.warning("Handoff: user %s is not approved; to sign-in", user.id)
        return home
    sign_in(request, user)
    logger.info("Handoff: signed in user %s", user.id)
    return HttpResponseRedirect(safe_internal_path(payload.get("next")) or "/index")
