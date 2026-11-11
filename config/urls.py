"""Every path this service answers; anything else is a 404 (no fall-through to Flask since Phase 7)."""

from django.http import JsonResponse
from django.urls import path

import hub.addresses  # noqa: F401 - registers the <entity:...> converter
from hub import (
    company,
    contact,
    email_signin,
    entities,
    front_door,
    handoffs,
    invitations,
    onboarding,
    profile,
    public,
    redirects,
    terms,
    xero_oauth,
)
from xero.views import internal_xero_access_token, internal_xero_sync


def health(request):
    """Liveness only: never a call to Flask (its own /health is behind us) or the database."""
    return JsonResponse({"status": "ok", "service": "minty-accounts-api"})


urlpatterns = [
    path("health", health, name="health"),
    # Phase 2 (2026-11-05): Flask's path, moved. The other backends call it at the same address.
    path("api/internal/xero/token", internal_xero_access_token, name="internal_xero_access_token"),
    # Phase 3 (2026-11-06): the one Xero account/contact sync, new (Flask's ran in each caller).
    path("api/internal/xero/sync", internal_xero_sync, name="internal_xero_sync"),
    path("api/handoff/redeem", handoffs.redeem_code, name="redeem_handoff_code"),
    # Phase 5 (2026-11-06): the public routes, Flask's paths.
    path("auth/notices", public.auth_notices, name="auth_notices"),
    path("legal/current", public.legal_current, name="legal_current"),
    path("legal/invite-terms-status", public.invite_terms_status, name="invite_terms_status"),
    # Phase 5B: the hub's own routes (bearer, hub CORS).
    path("api/me/entities", entities.my_entities, name="my_entities"),
    path("api/me/entities/<str:entity_id>/archive", entities.archive_entity, name="archive_entity"),
    path("api/me/entities/<str:entity_id>/restore", entities.restore_entity, name="restore_entity"),
    path("api/me/profile", profile.my_profile, name="my_profile"),
    path("api/me/terms", terms.terms_status, name="terms_status"),
    path("api/me/terms/accept", terms.terms_accept, name="terms_accept"),
    path("api/me/contact", contact.my_contact, name="my_contact"),
    # Phase 5C: a company's Users, General and Integration tabs (General split out 2026-11-10).
    path("api/me/company/users", company.company_users, name="company_users"),
    path("api/me/company/users/<str:member_id>", company.company_member, name="company_member"),
    path("api/me/company/invitations", company.company_invite, name="company_invite"),
    path("api/me/company/invitations/<str:invitation_id>/cancel", company.invitation_cancel, name="invitation_cancel"),
    path("api/me/company/invitations/<str:invitation_id>/resend", company.invitation_resend, name="invitation_resend"),
    path("api/me/company/integration", company.company_integration, name="company_integration"),
    path("api/me/company/xero/disconnect", company.company_xero_disconnect, name="company_xero_disconnect"),
    path("api/me/company/xero/release", company.company_xero_release, name="company_xero_release"),
    # Phase 5D: the onboarding wizard's routes minty-onboarding-api and minty-subscription-api forward to.
    path("api/onboarding/modules", onboarding.onboarding_modules, name="onboarding_modules"),
    path("api/onboarding/invite", onboarding.onboarding_invite, name="onboarding_invite"),
    path("api/onboarding/invite/resend", onboarding.onboarding_invite_resend, name="onboarding_invite_resend"),
    path("api/onboarding/invite/cancel", onboarding.onboarding_invite_cancel, name="onboarding_invite_cancel"),
    path("api/onboarding/xero/disconnect", onboarding.onboarding_xero_disconnect, name="onboarding_xero_disconnect"),
    path("api/onboarding/xero/release", onboarding.onboarding_xero_release, name="onboarding_xero_release"),
    # Phase 6: the browser session - signing in by code and the front door.
    path("", front_door.home, name="home"),
    path("index", front_door.index, name="index"),
    path("register", front_door.register, name="register"),
    path("auth/email/request-code", email_signin.email_request_code, name="email_request_code"),
    path("auth/email/verify-code", email_signin.email_verify_code, name="email_verify_code"),
    path("auth/email/handoff", email_signin.email_handoff, name="email_handoff"),
    path("invitation/accept/<str:token>", front_door.invitation_accept_page, name="invitation_accept"),
    # Phase 6D: Xero OAuth - sign in with Xero, connect and reconnect a company (state nonce).
    path("xero_auth", xero_oauth.xero_auth, name="xero_auth"),
    path("xero_connect", xero_oauth.xero_connect, name="xero_connect"),
    path("xero_reconnect", xero_oauth.xero_reconnect, name="xero_reconnect"),
    path("callback", xero_oauth.xero_callback, name="xero_callback"),
    # Phase 6C: into the apps, and out (POST only).
    path("entity", handoffs.entity_list, name="entity_list"),
    path("entity/create", handoffs.entity_create, name="entity_create"),
    path("entity/<entity:entity_id>/petty-cash", handoffs.report_dashboard, name="report_dashboard"),
    path("entity/<entity:entity_id>/modules", handoffs.module_selector, name="module_selector"),
    path("entity/<entity:entity_id>/enter", handoffs.module_reenter, name="module_reenter"),
    path("entity/<entity:entity_id>/payment-request", handoffs.go_to_payment_requests, name="go_to_payment_requests"),
    path("handoff/minty-web", handoffs.handoff_minty_web, name="handoff_minty_web"),
    path("handoff/petty-cash-web", handoffs.handoff_petty_cash_web, name="handoff_petty_cash_web"),
    path("profile", handoffs.open_profile, name="open_profile"),
    path("logout", handoffs.logout, name="logout"),
    path("leave-entity", handoffs.leave_entity, name="leave_entity"),
    # Phase 6E: addresses that only send a person on.
    path("entity/<entity:entity_id>/settings/general", redirects.settings_general, name="settings_general"),
    path("entity/<entity:entity_id>/settings/integration", redirects.settings_integration, name="settings_integration"),
    path("entity/<entity:entity_id>/settings/users", redirects.settings_users, name="settings_users"),
    path("entity/<entity:entity_id>/settings/petty-cash", redirects.settings_petty_cash, name="settings_petty_cash"),
    path("entity/<entity:entity_id>/settings/modules", redirects.settings_modules, name="settings_modules"),
    path("entity/<entity:entity_id>/settings/payment-request", redirects.settings_payment_request,
         name="settings_payment_request"),
    path("entity/<entity:entity_id>/petty-cash/reports", redirects.report_history, name="report_history"),
    path("entity/<entity:entity_id>/xero-not-connected", redirects.xero_not_connected, name="xero_not_connected"),
    path("entity/<entity:entity_id>/reports", redirects.reports_moved, name="legacy_reports"),
    path("entity/<entity:entity_id>/reports/<path:rest>", redirects.reports_moved, name="legacy_reports_page"),
    path("Minty_Report/<path:share_path>/", redirects.minty_report_share, name="minty_report_share"),
    *[path(route, redirects._moved(sub, methods), name=name) for route, name, sub, methods in redirects.LEGACY],
    path("entity/<entity:entity_id>", redirects._moved("petty-cash"), name="legacy_dashboard"),
    path("static/img/new_logo.png", invitations.logo, name="invitation_logo"),
]
