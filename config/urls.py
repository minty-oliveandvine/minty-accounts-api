"""What Django answers itself. Every other path falls through to Flask (core/fallthrough.py)."""

from django.http import JsonResponse
from django.urls import path

from hub import company, contact, entities, invitations, onboarding, profile, public, terms
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
    # Phase 5 (2026-11-06): the public routes, Flask's paths.
    path("auth/notices", public.auth_notices, name="auth_notices"),
    path("legal/current", public.legal_current, name="legal_current"),
    path("legal/invite-terms-status", public.invite_terms_status, name="invite_terms_status"),
    # Phase 5B: the hub's own routes (bearer, hub CORS).
    path("api/me/entities", entities.my_entities, name="my_entities"),
    path("api/me/profile", profile.my_profile, name="my_profile"),
    path("api/me/terms", terms.terms_status, name="terms_status"),
    path("api/me/terms/accept", terms.terms_accept, name="terms_accept"),
    path("api/me/contact", contact.my_contact, name="my_contact"),
    # Phase 5C: a company's Users and Entity & Integration tabs.
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
    path("static/img/new_logo.png", invitations.logo, name="invitation_logo"),
]
