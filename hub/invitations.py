"""Invitations to join a company - Flask's ``invitation/services/invite.py`` (Phase 5 of the Flask
retirement, 2026-11-06): create, cancel, resend, and the invitation email. The hub's Users tab and
onboarding's invite step both use these (subscription-api's "invite an admin" reaches the latter).

- An invitation holds a secret token (the accept link), the names the account is made from at
  sign-in, and an expiry ``INVITATION_TTL_DAYS`` ahead. An already-approved member, or an unexpired
  pending invitation for the same address, is refused (an expired one is marked so, and a new one
  goes ahead). The always-superuser view-all accounts (``AUTO_SUPERUSER_EMAILS``) are promoted
  when they already exist.
- Resend rotates the token (the old link dies) and resets the expiry, no sooner than
  ``RESEND_COOLDOWN_SECONDS`` after the last send. The cooldown is in process memory, per worker,
  as Flask's: best effort, never a security property.
- The email is Flask's HTML, from ``INVITE_FROM``; the accept link is on ``ACCOUNTS_API_URL``
  (``/invitation/accept/<token>``, still Flask's page until Phase 6) with ``?fn=&ln=``; the logo is
  ``/static/img/new_logo.png`` fingerprinted by content (the same file Flask serves). A failed send
  returns False and is logged - the caller says so (``email_sent: false``).
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from minty_db.models import Entity, Invitation, User, UserEntity

logger = logging.getLogger("minty-accounts-api")

INVITATION_TTL_DAYS = int(os.environ.get("INVITATION_TTL_DAYS", 7))
RESEND_COOLDOWN_SECONDS = int(os.environ.get("INVITATION_RESEND_COOLDOWN_SECONDS", 60))
AUTO_SUPERUSER_EMAILS = frozenset(
    e.strip().lower() for e in os.environ.get("AUTO_SUPERUSER_EMAILS", "viewall@dailyminty.com").split(",") if e.strip()
)
LOGO = Path(settings.BASE_DIR) / "static" / "img" / "new_logo.png"

_LAST_SENT: dict[str, datetime] = {}
_fingerprint: str | None = None


def _record_sent(invitation_id) -> None:
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=RESEND_COOLDOWN_SECONDS)
    for key in [k for k, v in _LAST_SENT.items() if v < cutoff]:
        _LAST_SENT.pop(key, None)
    _LAST_SENT[str(invitation_id)] = now


def reset_cooldowns() -> None:
    """Forget every send (tests)."""
    _LAST_SENT.clear()


def resend_cooldown_remaining(invitation) -> int:
    """Seconds before ``invitation`` may be resent (0 now; unknown after a restart is 0)."""
    last = _LAST_SENT.get(str(getattr(invitation, "id", invitation)))
    if last is None:
        return 0
    remaining = RESEND_COOLDOWN_SECONDS - (datetime.now(UTC) - last).total_seconds()
    return int(remaining) + 1 if remaining > 0 else 0


def _expired(invitation: Invitation) -> bool:
    return invitation.expires_at is not None and datetime.now(UTC) > invitation.expires_at


def create_invitation(entity_id, email: str, role: str, invited_by, first_name: str = "",
                      last_name: str = "") -> tuple[Invitation | None, str | None]:
    """The new invitation, or ``(None, sentence)``."""
    if not Entity.objects.filter(id=entity_id, archived_at__isnull=True).exists():
        return None, "Entity not found."
    existing = User.objects.filter(email=email).first() or User.objects.filter(username=email).first()
    if existing is not None and email.strip().lower() in AUTO_SUPERUSER_EMAILS:
        if existing.system_role != "superadmin" or not existing.approved:
            existing.system_role, existing.approved = "superadmin", True
            existing.save(update_fields=["system_role", "approved"])
    if existing is not None and UserEntity.objects.filter(user_id=existing.id, entity_id=entity_id,
                                                          approved=True).exists():
        return None, "This user is already a member of this entity."
    with transaction.atomic():
        pending = Invitation.objects.select_for_update().filter(entity_id=entity_id, email=email,
                                                                status="pending").first()
        if pending is not None:
            if not _expired(pending):
                return None, "An invitation is already pending for this email."
            pending.status = "expired"
            pending.save(update_fields=["status"])
        invitation = Invitation.objects.create(
            entity_id=entity_id, email=email, role=role, token=secrets.token_urlsafe(48), status="pending",
            invited_by=invited_by, first_name=(first_name or "").strip() or None,
            last_name=(last_name or "").strip() or None,
            expires_at=datetime.now(UTC) + timedelta(days=INVITATION_TTL_DAYS))
    return invitation, None


def invite_error_for_email(token: str, verified_email: str) -> str | None:
    """Why ``token`` can't be used by the person who just proved ``verified_email``, else None (an
    invitation must name that address: the code went to it)."""
    token, email = (token or "").strip(), (verified_email or "").strip().lower()
    if not token:
        return None
    invitation = Invitation.objects.filter(token=token, status__in=("pending", "accepted")).first()
    if invitation is None:
        return "This invitation is invalid or has already been used."
    if invitation.email.strip().lower() != email:
        return "This invite went to a different account — mind signing in as that one?"
    return None


def accept_invitation(token: str, user_id) -> tuple[str | None, str | None]:
    """``(entity_id, None)`` once ``user_id`` belongs to the company, or ``(None, sentence)``.
    Idempotent: a re-accept by the same person succeeds. Accepting IS the approval."""
    invitation = Invitation.objects.filter(token=token, status__in=("pending", "accepted")).first()
    if invitation is None:
        logger.info("invitation.accept.invalid user=%s", user_id)
        return None, "Invitation is invalid or has already been used."
    if str(invitation.status) == "pending" and _expired(invitation):
        Invitation.objects.filter(id=invitation.id).update(status="expired")
        logger.info("invitation.accept.expired invitation=%s user=%s", invitation.id, user_id)
        return None, "This invitation has expired. Please request a new one."
    user = User.objects.filter(id=user_id).first()
    if user is None:
        return None, "User not found."
    owned = {(user.email or "").lower(), (user.xero_email or "").lower(), (user.username or "").lower()}
    if invitation.email.lower() not in owned:
        logger.warning("invitation.accept.email_mismatch invitation=%s user=%s", invitation.id, user_id)
        return None, "This invite went to a different account — mind signing in as that one?"
    if invitation.email.strip().lower() in AUTO_SUPERUSER_EMAILS and (
            user.system_role != "superadmin" or not user.approved):
        User.objects.filter(id=user.id).update(system_role="superadmin", approved=True)
    if str(invitation.status) == "accepted":
        if UserEntity.objects.filter(user_id=user.id, entity_id=invitation.entity_id).exists():
            return str(invitation.entity_id), None
        logger.warning("invitation.accept.conflict invitation=%s user=%s", invitation.id, user_id)
        return None, "Invitation is invalid or has already been used."
    if not Entity.objects.filter(id=invitation.entity_id, archived_at__isnull=True).exists():
        return None, "The entity no longer exists."
    now = datetime.now(UTC)
    with transaction.atomic():
        updated = UserEntity.objects.filter(user_id=user.id, entity_id=invitation.entity_id).update(
            role=invitation.role, approved=True, joined_at=now)
        if not updated:
            UserEntity.objects.create(user_id=user.id, entity_id=invitation.entity_id, role=invitation.role,
                                      approved=True, joined_at=now)
        User.objects.filter(id=user.id).update(approved=True)
        Invitation.objects.filter(id=invitation.id).update(status="accepted", accepted_at=now)
    logger.info("invitation.accept.ok invitation=%s entity=%s role=%s user=%s", invitation.id,
                invitation.entity_id, invitation.role, user_id)
    return str(invitation.entity_id), None


def pending_invitations(entity_id) -> list[Invitation]:
    return list(Invitation.objects.filter(entity_id=entity_id, status="pending").order_by("-created_at"))


def cancel_invitation(invitation_id) -> tuple[bool, str | None]:
    updated = Invitation.objects.filter(id=invitation_id, status="pending").update(status="revoked")
    return (True, None) if updated else (False, "Invitation not found or already processed.")


def resend_invitation(invitation_id, actor_id=None) -> tuple[Invitation | None, str | None, int]:
    """``(invitation, None, 0)`` with a fresh token and expiry, or ``(None, sentence, retry_after)``."""
    invitation = Invitation.objects.filter(id=invitation_id, status="pending").first()
    if invitation is None:
        return None, "Invitation not found or already processed.", 0
    remaining = resend_cooldown_remaining(invitation)
    if remaining > 0:
        logger.info("Resend of invitation %s blocked by cooldown; %ss remaining (actor=%s)", invitation.id, remaining,
                    actor_id)
        return None, f"Please wait {remaining}s before resending this invitation.", remaining
    invitation.token = secrets.token_urlsafe(48)
    invitation.expires_at = datetime.now(UTC) + timedelta(days=INVITATION_TTL_DAYS)
    invitation.save(update_fields=["token", "expires_at"])
    logger.info("Invitation %s resent for entity %s (actor=%s); previous link invalidated", invitation.id,
                invitation.entity_id, actor_id)
    return invitation, None, 0


def _logo_url() -> str:
    global _fingerprint
    if _fingerprint is None:
        try:
            _fingerprint = hashlib.md5(LOGO.read_bytes()).hexdigest()[:10]
        except OSError as exc:
            logger.warning("Could not fingerprint the invitation logo: %s", exc)
            _fingerprint = ""
    url = f"{settings.ACCOUNTS_API_URL}/static/img/new_logo.png"
    return f"{url}?v={_fingerprint}" if _fingerprint else url


def send_invitation_email(invitation: Invitation, first_name: str = "", last_name: str = "") -> bool:
    """Send the invitation; True on success, False (logged) otherwise."""
    try:
        entity = Entity.objects.filter(id=invitation.entity_id).first()
        entity_name = entity.name if entity else "Unknown Entity"
        inviter = User.objects.filter(id=invitation.invited_by).first() if invitation.invited_by else None
        inviter_name = (" ".join(p.strip() for p in (inviter.first_name, inviter.last_name) if p and p.strip())
                        if inviter else "") or "A team member"
        accept_url = f"{settings.ACCOUNTS_API_URL}/invitation/accept/{invitation.token}"
        names = {k: v for k, v in (("fn", first_name or invitation.first_name or ""),
                                   ("ln", last_name or invitation.last_name or "")) if v}
        if names:
            accept_url = f"{accept_url}?{urlencode(names)}"
        message = EmailMessage(
            subject=f"You've been invited to {' '.join(entity_name.split())} on Minty",
            from_email=settings.INVITE_FROM, to=[invitation.email],
            body=_build_invitation_html(entity_name=entity_name, role=str(invitation.role), inviter_name=inviter_name,
                                        accept_url=accept_url, logo_url=_logo_url()),
        )
        message.content_subtype = "html"
        message.send()
        _record_sent(invitation.id)
        logger.info("Invitation email sent for entity %s (invitation %s)", entity_name, invitation.id)
        return True
    except Exception as exc:
        logger.error("Failed to send invitation email for invitation %s: %s", invitation.id, exc)
        return False


# ---- Flask's HTML, unchanged ------------------------------------------------------------------


def _build_invitation_html(
    entity_name: str,
    role: str,
    inviter_name: str,
    accept_url: str,
    logo_url: str = "",
) -> str:
    # Every value is escaped, once, here. A company or a person can be named anything -
    # "<b>Acme</b>", or a script - and the email is HTML; the URLs carry "&".
    role_display = html.escape(role.replace("_", " ").title())
    entity_name = html.escape(entity_name)
    inviter_name = html.escape(inviter_name)
    accept_url = html.escape(accept_url)
    logo_url = html.escape(logo_url)
    return f"""\
<!DOCTYPE html>
<html lang="en" xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<meta name="x-apple-disable-message-reformatting">
<title>You&#39;ve been invited to Minty</title>
<!--[if mso]>
<xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml>
<![endif]-->
<style>
  body,table,td,a {{ -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }}
  table,td {{ mso-table-lspace:0pt; mso-table-rspace:0pt; }}
  img {{ -ms-interpolation-mode:bicubic; border:0; line-height:100%; outline:none; text-decoration:none; }}
  body {{ width:100% !important; min-width:100%; }}
  @media only screen and (max-width:600px) {{
    .m-wrap   {{ padding:20px 12px !important; }}
    .m-card   {{ width:100% !important; max-width:100% !important; border-radius:12px !important; }}
    .m-header {{ padding:20px 16px !important; }}
    .m-pad    {{ padding-left:24px !important; padding-right:24px !important; }}
    .m-body   {{ padding-top:28px !important; }}
    .m-cta    {{ padding:24px !important; }}
    .m-h1     {{ font-size:20px !important; }}
    .m-btn    {{ display:block !important;
                padding-left:16px !important; padding-right:16px !important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;font-family:'Inter',Arial,Helvetica,sans-serif;background:#f0f4f8;">
  <div style="display:none;font-size:1px;color:#f0f4f8;line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;">
    {inviter_name} invited you to join {entity_name} on Minty.
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
         style="background:#f0f4f8;">
    <tr><td align="center" class="m-wrap" style="padding:40px 12px;">

      <!--[if mso]>
      <table role="presentation" width="520" cellpadding="0" cellspacing="0" border="0"><tr><td>
      <![endif]-->
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="m-card"
             style="width:100%;max-width:520px;background:#ffffff;border-radius:16px;
                    overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">

        <!-- Header: the Minty mark -->
        <tr><td class="m-header"
                style="background:#ffffff;
                       padding:24px 40px;text-align:center;">
          <img src="{logo_url}" alt="Minty" width="102"
               style="display:block;border:0;width:102px;max-width:102px;height:auto;margin:0 auto;" />
        </td></tr>

        <!-- Body -->
        <tr><td class="m-pad m-body" style="padding:36px 40px 0 40px;">
          <h1 class="m-h1" style="color:#2d3748;font-size:22px;margin:0 0 8px 0;text-align:center;">
            You&#39;ve Been Invited!
          </h1>
          <p style="color:#718096;font-size:14px;text-align:center;margin:0 0 28px 0;">
            Join your team on Minty and start collaborating.
          </p>
        </td></tr>

        <tr><td class="m-pad" style="padding:0 40px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                 style="background:#f7fafc;border-radius:10px;">
            <tr><td style="padding:20px 24px;color:#4a5568;font-size:14px;line-height:1.7;
                           word-break:break-word;">
              <p style="margin:0 0 6px 0;">
                <strong>{inviter_name}</strong> has invited you to join
              </p>
              <p style="margin:0 0 6px 0;font-size:18px;">
                <strong style="color:#54D3DA;">{entity_name}</strong>
              </p>
              <p style="margin:0;color:#718096;">
                Role: <strong>{role_display}</strong>
              </p>
            </td></tr>
          </table>
        </td></tr>

        <!-- CTA Button -->
        <tr><td class="m-cta" style="text-align:center;padding:32px 40px;">
          <a href="{accept_url}" class="m-btn"
             style="display:inline-block;background:#54D3DA;color:#ffffff;text-decoration:none;
                    padding:14px 48px;border-radius:8px;font-weight:600;font-size:16px;
                    line-height:1.2;text-align:center;
                    box-shadow:0 2px 8px rgba(84,211,218,0.35);">
            Accept Invitation
          </a>
        </td></tr>

        <!-- Footer -->
        <tr><td class="m-pad"
                style="padding:16px 40px 24px 40px;text-align:center;border-top:1px solid #e2e8f0;">
          <p style="color:#a0aec0;font-size:12px;line-height:1.5;margin:0;">
            If you did not expect this invitation you can safely ignore this email.<br />
            &copy; {_current_year()} Daily Minty Limited
          </p>
        </td></tr>

      </table>
      <!--[if mso]>
      </td></tr></table>
      <![endif]-->

    </td></tr>
  </table>
</body>
</html>"""


def _current_year() -> int:
    return datetime.now(UTC).year


def logo(request):
    """``GET /static/img/new_logo.png``: the invitation email's logo, served here so the emails sent
    before and after the move keep their image once Flask stops (Flask serves the same file today)."""
    from django.http import FileResponse, HttpResponseNotAllowed

    if request.method not in ("GET", "HEAD"):
        return HttpResponseNotAllowed(["GET", "HEAD"])
    response = FileResponse(LOGO.open("rb"), content_type="image/png")
    response["Cache-Control"] = "public, max-age=604800"
    return response
