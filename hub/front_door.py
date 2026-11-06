"""The front door - Flask's ``auth/routes/home.py``, ``register.py``, ``dashboard.py`` and
Flask-Login's ``login_required`` (Phase 6 of the Flask retirement, 2026-11-06).

- ``GET /``: signed in, the company list (``/entity``); otherwise minty-web's sign-in page with
  ``next`` (where the person was being sent) and anything flashed on the way.
- ``GET /register``: sign-up is minty-web's ``/signup``; nothing is made here.
- ``GET /index``: the company list (old links and redirects still name it).
- ``signed_in_or_door(request)``: Flask-Login's gate - the signed-in person, or a redirect to ``/``
  with ``next`` (this request's path and query) and "Please log in to access this page." (info).
"""

from __future__ import annotations

from urllib.parse import urlencode

from django.http import HttpRequest, HttpResponse, HttpResponseNotAllowed, HttpResponseRedirect
from minty_db.models import User

from hub.links import hub_login_url
from hub.session import flash, refresh_presence, signed_in_user

LOGIN_MESSAGE = "Please log in to access this page."


def signed_in_or_door(request: HttpRequest) -> tuple[HttpResponse | None, User | None]:
    user = signed_in_user(request)
    if user is not None:
        refresh_presence(request, user)
        return None, user
    flash(request, LOGIN_MESSAGE, "info")
    return HttpResponseRedirect("/?" + urlencode({"next": request.get_full_path()})), None


def home(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return HttpResponseNotAllowed(["GET"])
    if signed_in_user(request) is not None:
        return HttpResponseRedirect("/entity")
    return HttpResponseRedirect(hub_login_url(request, next_path=request.GET.get("next")))


def register(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    return HttpResponseRedirect(hub_login_url(request, mode="signup"))


def index(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        return HttpResponseNotAllowed(["GET"])
    early, _user = signed_in_or_door(request)
    return early or HttpResponseRedirect("/entity")


def invitation_accept_page(request: HttpRequest, token: str) -> HttpResponse:
    """``GET /invitation/accept/<token>``: the invitation email's link, bounced to the hub's sign-in
    page for that address (the code sign-in then accepts it). A used or expired link says so on
    the sign-in page; someone else signed in is signed out first, never accepted as the invitee."""
    from datetime import UTC, datetime

    from minty_db.models import Invitation

    from hub.session import sign_out

    if request.method not in ("GET", "HEAD"):
        return HttpResponseNotAllowed(["GET"])
    invitation = Invitation.objects.filter(token=token, status="pending").first()
    if invitation is None:
        flash(request, "This invitation doesn't work anymore — it may have already been used.", "warning")
        return HttpResponseRedirect("/")
    if invitation.expires_at is not None and datetime.now(UTC) > invitation.expires_at:
        Invitation.objects.filter(id=invitation.id).update(status="expired")
        flash(request, "This invitation has expired — please ask for a new one.", "warning")
        return HttpResponseRedirect("/")
    user = signed_in_user(request)
    if user is not None:
        invited = invitation.email.strip().lower()
        owned = {(v or "").strip().lower() for v in (user.email, user.xero_email, user.username)} - {""}
        if invited not in owned:
            sign_out(request)
            flash(request, f"This invite went to {invitation.email} — mind signing in as that account?", "warning")
    return HttpResponseRedirect(hub_login_url(
        request, invite=token, email=invitation.email, first_name=(request.GET.get("fn") or "").strip(),
        last_name=(request.GET.get("ln") or "").strip()))
