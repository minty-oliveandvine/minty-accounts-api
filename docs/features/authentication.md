# Authentication and access — how a person gets in, and what they may touch

This is the system-wide description. minty-accounts-api is the only issuer of identity: every
other service — `minty-web`, `minty-payment-request-web`/`-api`, `minty-onboarding-web`/`-api`,
`minty-petty-cash-web`/`-api`, `minty-subscription-api` — verifies what it minted and never signs
anyone in itself. Each of those repos has a `docs/features/authentication.md` that covers its own
half and links back here.

It moved here from `Minty/docs/features/authentication.md` on 2026-11-06, when this service
replaced Flask (`docs/features/accounts-api.md`). The rules are Flask's, kept route for route;
where the move changed one, this page says so. Flask's own mechanics (Flask-Login, Flask-Session,
the blueprints) are in Minty's git history.

## 1. Identities

One `user` row per person (minty-db's `User`). The columns that matter:

| Column | Meaning |
|---|---|
| `username` | the email address the account was made with; also matched when an invitation checks who is accepting |
| `email`, `xero_email` | the two identity columns: every sign-in resolves a person by either, case-insensitively (`hub/email_signin.resolve_user_by_email`) |
| `password` | a random hash for code and Xero sign-ups. **Nothing reads it** (password sign-in and reset went 2026-10-20) |
| `approved` | the sign-in gate: an unapproved account is not signed in by code (403), a cookie naming one is emptied, and the hub's door refuses its token. Accepting an invitation sets it (`hub/invitations.accept_invitation`) |
| `system_role` | `normal` / `admin` / `superadmin` (`minty_db.enums.SystemRole`); the code still calls the last one *superuser* (`hub/policy.py`) |
| `signed_in_at`, `last_seen_at` | presence stamps (section 8) |

Membership of a company is a `user_entity` row: `(user_id, entity_id, role, approved)`.
The entity roles, lowest to highest: `entity_base` → `cashier` → `shop_manager` →
`accountant` → `admin` → `super_admin` (`hub/policy.Role`, with the `ROLE_ALIASES` map for the
older spellings such as `client` and `super admin`; an unknown spelling ranks lowest).

## 2. The ways in

Every way ends in `hub/session.sign_in` (section 3); the session is the same afterwards. There is
no password sign-in: a person gets in by an emailed code or through Xero.

**The sign-in PAGE is minty-web's `/login`** - log in and invitations
(`?invite=&email=&fn=&ln=`); **sign-up is its own page, `/signup`**, with the code on
`/login/confirm` (`minty-web/features/auth`, its `docs/features/authentication.md`). This service
draws no page. Every way TO the page goes through `hub/links.hub_login_url`: the front door
`GET /` (`hub/front_door.py`) and with it every signed-in-only route (`signed_in_or_door`),
`/register`, an invitation link, Xero's refusals. It keeps `next` only when it is a path on this
site (`hub/session.safe_internal_path`) and drains the flashed messages into a signed `?flash=`
(`hub/notices.sign_notices`), which the page reads back from the public `GET /auth/notices`
(`hub/public.py`). The signed-in-only routes' "Please log in to access this page." is category
`info`.

### 2.1 Email code — the main path (also the sign-up path)
`hub/email_signin.py`. The routes are cross-origin JSON with no cookie (the page sends
`credentials: "omit"`): open CORS, no CSRF.

1. `POST /auth/email/request-code` - in login mode (`mode: "login"`) an unknown address is
   refused **before** a code is sent (404 "Please sign up first"), by the same rule verify signs
   in by (`resolve_user_by_email`: `user.email` or `user.xero_email`; a `username` alone does
   not count). A 6-digit code, valid **60 s**, resend cooldown 60 s. Failed tries carry across
   resends (`email_otp.attempts`); at 5 the address is locked for **15 minutes** (429), anchored
   to the row's `created_at`. A code whose email fails to send is rolled back and the request
   answers 400, so the person can retry at once. Codes are stored in werkzeug's `pbkdf2:sha256`
   format.
2. `POST /auth/email/verify-code` - a correct code (single use: only the request that stamps
   `verified_at` gets through) either signs the existing person in, or - for a new address that
   came with a first and last name - makes an approved, passwordless account with its Terms
   agreement in the same transaction (source `signup_otp`, or `signup_invite` when an invitation
   rode along; only the LIVE version, ticked, is recorded). A new address with no name answers
   404 "please sign up". An unapproved account with no invitation is 403. The answer carries a
   hand-off URL, with the page's `next` (checked) parked in it.
3. `GET /auth/email/handoff?h=` - the same-origin landing that sets the cookie: the sign-in page
   talks to this service from another origin, so the verify answer carries the hand-off URL.
   **Single use (2026-11-06):** verify parks the hand-off as a pending `django_session` row
   (5 minutes) and the URL carries its key, signed (`HANDOFF_SALT`); the landing deletes the row
   and signs in only when the delete removed it, so a replay, a copied link or a second tab signs
   nobody in. An invitation is accepted first and lands on that company's address; otherwise the
   parked `next` (checked again), else `/index` - the company list - for everyone, a superadmin
   included.

The mail goes out through Django's SMTP backend from `SMTP_URL` (`minty_shared.config.django_email`),
From `INVITE_FROM`; every SMTP step times out after the URL's `?timeout=` seconds (10).

### 2.2 Sign in with Xero — `GET /xero_auth`
`hub/xero_oauth.py` (Xero's side: `xero/services/connect.py`). OpenID Connect against Xero with
scope `openid profile email offline_access` and `prompt=login`; the state is `auth` (or
`auth:invite:<token>`) plus the session's nonce (section 3.1). `?next=` is kept in the session
only when it is a path on this site. The callback (`/callback`) checks the nonce before anything
else, exchanges the code, reads the email from the id_token (its signature is not checked: it
comes straight from Xero's token endpoint over TLS, as in Flask), resolves the person
(`resolve_user_by_email`) or makes a passwordless one with the Xero name, stores the token bundle
(`user_token`) and signs them in. A switched-off account is not signed in. **This is identity
only** - a company's Xero *connection* is a different flow with different scopes (section 7).

### 2.3 Invitation
`hub/invitations.py`: someone sends an invite from the company's Users tab (minty-web;
`POST /api/me/company/invitations`, `hub/company.py`) or the onboarding wizard
(`/api/onboarding/invite`, `hub/onboarding.py`). The email's link,
`{ACCOUNTS_API_URL}/invitation/accept/<token>?fn=&ln=` (`hub/front_door.invitation_accept_page`),
bounces to the sign-in page (`hub_login_url`) with the token, the invited address and the names;
a used or expired link says so there, and someone else signed in is signed out first. The code or
Xero sign-in then carries it (`invite=` / state `auth:invite:`): the account is made if needed,
the address must be one the person owns, and `accept_invitation` writes the `user_entity` row,
approves the person and marks the invitation accepted. `POST /legal/invite-terms-status`
(`hub/public.py`; the token in the JSON body, never the URL) tells the sign-up screen whether
that person still owes Terms consent, failing safe to "required". The sign-in page clears the
invite from its address bar and hands it to `/login/confirm` in sessionStorage (minty-web).

### Sign-up
`GET/POST /register` makes nothing - it forwards to the hub's `/signup`
(`hub_login_url(mode="signup")`, so the mode is the PATH, never a query parameter), and sign-up
runs through the code path above.

### Email input: English only
Every typed email address is printable ASCII (0x21-0x7E) and nothing else (2026-10-01).

- **The fields** are the Next apps' (`type="text" inputmode="email"`, never `type="email"`:
  the browser's email input takes Hangul after the "@" as an international domain and hands
  `.value` back as punycode). minty-web's `lib/emailInput.ts` strips anything else as it is typed.
- **This service** refuses one loudly in the same words (`hub/rules.py`):
  `POST /auth/email/request-code` 400 (login mode too, before its 404), the invites 400 - an
  invite's address must also be an address with no markup (`invite_address_error`) - and
  My Profile (`PATCH /api/me/profile`, `hub/profile.py`) 422. The onboarding billing email and the
  payer portal's invite-admin are minty-subscription-api's, and the company's business email
  minty-onboarding-api's; each refuses them there.
- Stored addresses are not rewritten.

## 3. The session

`hub/session.py` and `config/settings.py`. Django's sessions (`django.contrib.sessions`, database
backend, table `django_session` - made by hand with `manage.py migrate sessions` on each database,
not in the schema file), lifetime **24 hours**, cookie `session` (Flask's name, so Flask's old
cookie was replaced and everyone signed in again once), `HttpOnly`, `SameSite=Lax`, `Secure`
everywhere but development. There is no `django.contrib.auth`: the session holds the person's id
and the sign-in's id (`login_sid`, the module tokens' `sid` claim), plus flashed messages, the
preview set, the Terms answer and the Xero nonce. Flask's `sessions` table is no longer used.

- `sign_in` cycles the session key (no fixation), starts a fresh `login_sid` and stamps presence.
- `signed_in_user`: a cookie naming a person who is gone or not approved is emptied.
- **No CSRF middleware.** The JSON routes are called cross-origin with a bearer token and no
  cookie. The two cookie POSTs, `/logout` and `/leave-entity` (`hub/handoffs.py`), must come from
  one of the front ends (`Origin`: the configured four, this service's own, `EXTRA_FRONTEND_ORIGINS`
  or a preview-set host; else 403) - on top of SameSite=Lax.
- There is **no idle logout**; what "still here" means is section 8.
- `POST /logout` drops what was flashed, stamps `signed_in_at = NULL`, empties the session and
  goes to `/`. `POST /leave-entity` takes the person off the presence list, keeps the session and
  goes to `/entity`. A GET to either is 405.
- No request refreshes a Xero token on the side any more (Flask's `after_request` hook did);
  tokens are refreshed when asked for (section 7).

### 3.1 Redirects, headers and logs

- **Redirect targets.** Every redirect to a value the request supplied - `?next=` on
  `/xero_auth` (checked when stored AND when popped after the Xero round trip),
  `/entity/<co>/enter`, `/handoff/minty-web`, `/handoff/petty-cash-web` and the code sign-in -
  goes through ONE rule, `hub/session.safe_internal_path`: a path on this site, no `//host`, no
  backslash, no control character (browsers drop tab/CR/LF, so `/%09/evil.com` arrives as
  `//evil.com`), no `..`, nothing `urlsplit` reads as a scheme or host. The Next apps share the
  same rule as `lib/safeNext.ts`.
- **Headers** (`core/hardening.py`, on every answer): `Referrer-Policy: same-origin`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, HSTS outside development, and
  `Cache-Control: no-store` on `/invitation/accept/<token>`. The Next apps send the same set from
  `next.config.ts`.
- **Logs never hold a secret from an address.** Each request is one line
  (`minty_shared`'s request middleware): the path only, never the query string, with the
  invitation token shown as `[redacted]` (`core/hardening.loggable_path`); runserver's own access
  line is switched off. gunicorn's access log goes to `/dev/null` on its command line
  (`Dockerfile`), which a host's `GUNICORN_CMD_ARGS` cannot override (Render's native runtime did
  log Flask's `/enter?token=` in October; every service now runs in Docker);
  `tests/test_settings.py` holds it.
- **No state change by GET**: sign-out, leaving a company, Xero disconnect and the invite Terms
  check (`POST`, the token in the body) all refuse GET.
- **The gaps the URL security round recorded (2026-10-05):**
  - FIXED 2026-11-06: the code sign-in's hand-off is single use (2.1); the Xero OAuth `state`
    carries a per-session nonce, spent at the callback and checked before any code exchange,
    so a forged `/callback` link signs nobody in and attaches no organisation (section 7);
    `/logout` and `/leave-entity` are POST only, from a front end.
  - FIXED 2026-11-09 (gap 4, owner): **no token rides in an address.** Into an app, accounts-api
    hands over a **one-time code** (`?code=`, parked like the code sign-in's hand-off,
    `hub/parked.py`: single use, 2 minutes, bound to the app's origin) that the app's landing swaps
    for its token at `POST /api/handoff/redeem`, in the browser (`@minty/shared/landing` v0.16).
    The way back carries **no token**: `/enter` works on the session, else the front door and
    back (owner: session only) - it no longer turns a replayable 30-minute token into a session.
    The `redesign` Workers were not brought along (owner): their old landing cannot swap a code,
    so signing in to them stops working until `redesign` catches up.

### 3.2 Addresses: `/entity/<shortid>/<name>/...`

Every company address hangs off **`<co>` = `<shortid>/<name>`**: the first 8 characters of the
company's id, then its name as a readable segment, e.g. `/entity/360812e1/harbour-and-vine-limited`.
The short id decides; the name is only for reading (`hub/addresses.py`):

- **The converter** (`<entity:...>` in `config/urls.py`) matches `<shortid>/<name>` or a full uuid
  and resolves it to the uuid. **A GET whose address used a full uuid, an old name or capitals is
  308'd to the canonical one** (query kept; `canonical_redirect`), so a rename never breaks a link
  and the other apps, which know only uuids, can link `/entity/<uuid>/...`.
- **An unknown company is not a 404 there**: it resolves to a uuid no company has, and the usual
  sign-in and membership checks answer, so an address tells a stranger nothing.
- **Slug** (`slugify_name`, mirrored by minty-web's `lib/companyRef.ts`): lowercase, `&` -> "and",
  letters and digits of any script kept, anything else one `-`, 60 characters, `company` when empty.
- **The scheme** (this service draws none of these pages; each address hands over):

  | Address | Goes to |
  |---|---|
  | `/entity/<co>/petty-cash` (the bare `/entity/<co>` 308s there), `/entity/<co>/petty-cash/reports` (query kept) | minty-petty-cash-web's dashboard and `/reports`, through `/handoff/petty-cash-web` |
  | `/entity/<co>/settings/users`, `/general`, `/integration`, `/modules` | minty-web's settings tabs |
  | `/entity/<co>/settings/petty-cash` | minty-web's Petty Cash Settings tab (`/settings/petty-cash`; in the hub since 2026-11-10, owner) |
  | `/entity/<co>/settings/payment-request` | minty-web's Payment Settings tab (`/settings/payment`; in the hub since 2026-11-10, owner) |
  | `/entity/<co>/payment-request` | the payments app |
  | `/entity/<co>/enter`, `/xero-not-connected` | the return from an app; the integration tab |

  `hub/handoffs.py` and `hub/redirects.py` keep Flask's guards: signed in, a member (or
  superuser), the tab's permission, the module switched on. `/payment-request?request=<id>` lands
  on one request: a uuid, or its Payment No. as plain text, kept only when 1-255 characters with
  no control character, no backslash and no empty/`.`/`..` piece between slashes
  (`_request_segment`; anything else lands on the list), quoted into ONE path segment.
- **Old addresses keep working** (`hub/redirects.LEGACY`): each pre-2026-10-05 address 308s to the
  readable one, keeping the query (and for the settings forms the method and body);
  `/entity/<co>/reports[/...]` 308s to `/entity/<co>/petty-cash/reports[/...]`, `/Minty_Report/<path>/` to
  minty-petty-cash-web's `/share/<path>`. Any path not routed is a 404 (no fall-through to Flask
  since 2026-11-06). The JSON APIs, OAuth, `/profile` and `/handoff/*` keep the uuid.

## 4. The terms gate

`hub/terms_gate.py` (middleware, after the session): a person signed in through the cookie who
has not agreed to the live Terms version is redirected to `/entity`; a JSON caller gets 403
`terms_acceptance_required`. `/entity` hands the browser to minty-web, whose own gate draws the
panel over every page and records the agreement through `POST /api/me/terms/accept`
(`hub/terms.py`; bearer, `source = "hub"`; 409 `version_changed` / `document_mismatch`;
idempotent). So **minty-web being up is part of the sign-in path** for anyone who owes an
acceptance, and the page they were trying to reach is not remembered.

- Never gated (`ALLOWED`, by URL name): `health`, the front door and `/register`, the notices, the
  three code-sign-in routes, `/logout`, the invitation link and its logo, `/entity`, the public
  legal routes, minty-web's two Terms routes, and Xero's `/callback` (its start routes stay gated,
  so only a flow begun while agreed lands there; Flask gated it and lost a connect already granted
  at Xero). Nobody signed in, and the bearer routes (no cookie), are untouched.
- The answer is cached in the session per version, so a new version re-asks everyone.
- A live version with no manifest entry gates nobody, logged as an error; a failure of the gate
  itself lets the request through, loudly (an outage is worse than a briefly skipped backstop).
- The versions are `hub/registry.py`'s (`CURRENT_TERMS_VERSION`, `CURRENT_PRIVACY_VERSION`,
  `REQUIRE_TERMS_AT_SIGNUP`); minty-web holds the text.

## 5. Roles and permissions

`hub/policy.py` is the matrix: minty-onboarding-api's `core/policy.py` copied unchanged, itself a
port of Flask's `permission_policy`. The same rules sit in every service until they move into
minty-db; change them together.

- `has_permission(user, permission, entity_id)` resolves the **effective role**: the approved
  `user_entity.role`, or `super_admin` for a system superuser - but a superuser with **no**
  membership on the company is *read-only* (`is_superuser_readonly`): only
  `READONLY_ALLOWED_PERMISSIONS` pass. There is no global hook as in Flask: every write route
  asks `has_permission` itself, which refuses them.
- `PERMISSION_RULES` maps each `Permission` to a minimum role. The ones people ask about:
  `USER_INVITE` and `USER_ROLE_ASSIGN` need **shop_manager**; `REPORT_PUBLISH`,
  `XERO_SETTINGS_UPDATE`, `ENTITY_UPDATE` and `USER_ROLE_DELETE` need **accountant**;
  `ENTITY_CREATE` is everyone's.
- A refusal answers a JSON caller 403 with the route's own sentence; a browser gets the message
  flashed and a redirect to `/entity`, which carries it to minty-web's company list as a notice.
- A company's members are managed per company - minty-web's Users tab over `/api/me/company/*`
  (`hub/company.py`): each action its permission, then the rank rule (nobody gives a role above
  their own or manages someone who matches or outranks them), then the payer and only-admin
  guards (`docs/features/accounts-api.md`, 5C).
- Tightened in the move (2026-11-06): connecting a company to Xero needs `XERO_SETTINGS_UPDATE` on
  it (Flask asked only `ENTITY_CREATE`); choosing onboarding modules needs an **approved** member.
- Flask's superuser routes (`/minty/api/users/*`) were not moved: nothing called them.

## 6. Hand-off tokens — how the other apps know who you are

This service mints two kinds of HS256 JWT over the **shared `SECRET_KEY`** (the same value on this
service and minty-subscription-api, minty-petty-cash-api, minty-payment-request-api and
minty-onboarding-api; a mismatch 401s every call). `hub/handoffs.py`, with Flask's claims exactly:

| Token | Minted by | Lifetime | Claims | Verified by |
|---|---|---|---|---|
| module | `module_token`, at every hand-off into minty-web, the payments app and Petty Cash web | 30 min | `user_id`, `entity_id` (empty when unscoped), `xero_org_id`, `role`, `system_role`, `module: "billing"`, `sid` (the sign-in's `login_sid`), `billing_enabled`, `petty_cash_enabled`, `is_view_only` (a system super admin in a company they are not a member of), `iat`, `exp` | minty-payment-request-api `core/auth.BearerAuth`, minty-subscription-api, minty-petty-cash-api (module tokens only), this service's hub door |
| onboarding | `onboarding_token`, when the wizard is launched (`/entity/create`, a company still onboarding) | 60 min | `user_id`, `scope: "onboarding"`, `iat`, `exp` | minty-onboarding-api `core/auth.OnboardingBearerAuth`; this service's `/api/onboarding/*` (`hub/onboarding.py`) |

- **Into the apps** (signed in, else the front door with `next`): the browser goes to the app's
  `/landing?next=&entity_id=&entity_name=&code=`, or the onboarding app's
  `/?code=&first=&last=&entity_name=&entity_id=&fresh=` - a one-time code for the token, never
  the token (gap 4; the swap is below).
  - `/entity`: minty-web's company list (`/entities`) with an unscoped token, carrying what was
    flashed. `/entity/create`: a fresh onboarding wizard.
  - `/entity/<co>/modules`: a company still onboarding to the wizard; one module on to that app;
    both to minty-web's module choice. `/entity/<co>/payment-request` straight in.
  - `GET /handoff/minty-web?next=&entity_id=` and `GET /profile?entity_id=` (the payments app's
    old `/profile*` links; minty-web has no My Profile page since 2026-11-03, so it lands on
    `/entities`, where My Profile opens in the sidebar): a fresh token at the click.
  - minty-payment-request-web keeps the token in its `billing_token` cookie and re-mints it
    through minty-payment-request-api's `POST /api/auth/token/refresh`.
- **minty-petty-cash-web** is entered the minty-web way. `GET /handoff/petty-cash-web?next=&entity_id=`
  mints a module token for ONE company the person may see, past onboarding (else the wizard), with
  Petty Cash on; `entity_id` may be the app's `<shortid>/<name>` form. The app keeps it in its
  own `petty_cash_*` cookies for exactly the token's lifetime; a lapsed token comes back the same
  way. Its calls go to minty-petty-cash-api, which takes only a module token.
- **The hub's door** (`hub/transport.py`): minty-web, the payments app and Petty Cash read the
  hub routes (`/api/me/entities`, `/api/me/profile`, `/api/me/terms*`, `/api/me/contact`,
  `/api/me/company/*`) with that token - bearer only (`minty_shared.tokens.decode_bearer`, 60 s
  skew), any Minty token, no scope check; a token naming an unknown or unapproved account is 401
  `{"error": "unauthorized"}`. The company travels as `?entity=` and is checked per route, never
  read from the token. CORS echoes the three front ends, their `EXTRA_FRONTEND_ORIGINS` and their
  preview sets, refusals included.
- **Coming back** is `GET /entity/<co>/enter?next=`: a browser signed in here is sent straight on
  (`next` is a path on this site, else the company's Petty Cash); with no session it goes to the
  front door and returns here once signed in. No token is read from the address (gap 4).
- **One-time codes** (gap 4): `POST /api/handoff/redeem` `{"code"}` answers `{"token"}` once, to
  the origin the code was handed to; used, lapsed or forged is 400, another origin 403 (and the
  code is spent), always one sentence; CORS for the four front ends, `Cache-Control: no-store`.
  Neither code nor token is logged.
  Flask's `/entity/<co>/billing-relogin` was not moved (no caller).
- The entitlement claims in the token are a hint only: minty-payment-request-api re-reads
  `entity_function_map` (`GET /api/auth/entitlements`) and the database decides what a module shows.
- The e2e suites of the Next apps mint these tokens themselves with the same secret (their
  `e2e/README.md` explains why nothing is bypassed by that).

### 6.1 Branch preview sets — handing back into a preview (2026-10-13)

A branch's previews are one SET of hosts, `https://pr-<branch>-<app>.dailyminty.com` for `my`,
`payment-request`, `onboarding`, `pettycash` (and `www`), kept complete by the `.github` repo's
`vercel-preview-set.yml` (minty-infra `CUTOVER.md` section 6). Where **`PREVIEW_ORIGIN_REGEX`** is
set, this service hands a person back into the set they came from instead of the configured front
ends (`hub/links.py`):

- A set's front ends name themselves with `?return_origin=<its origin>` on every trip here
  (minty-web `lib/handoff.ts`, minty-payment-request-web `withReturnOrigin` from `@minty/shared`).
  `PreviewMiddleware`, before every view, keeps the set's slug in the session.
- **Each trip decides its own hand-off, so production and a preview work side by side.** Previews
  call production's backends, so a preview tab and a production tab share one session
  (`handoff_origin`, `_named`):
  - `return_origin` naming a set - that set; naming a configured origin - the configured front
    end, and the stored set is left;
  - a navigation from another of our hosts with no `return_origin` (`same-site`), from the
    address bar or a bookmark (`none`), or with no fetch metadata - the configured front end, and
    the stored set is left;
  - only a hop that can't name one reads the stored set: a `same-origin` navigation and Xero's
    OAuth return (`cross-site`), which hands back to the side the person last came from - with two
    tabs interleaved, that can be the other tab's side;
  - fetches (`Sec-Fetch-Mode` other than `navigate`) neither read nor leave it.
- **Only a same-site (or same-origin) trip names a set, and a stored set lapses after 30 minutes**
  (`SET_TTL_SECONDS`): a link in an email or on another site (`none`/`cross-site`) cannot park a
  signed-in person's session in a set and route later hand-offs' tokens into a preview. Each
  naming trip renews it. Signing out empties the session, the set with it (Flask kept it).
- The hand-offs that follow this rule: `hub_login_url`, the minty-web, payments and Petty Cash
  `/landing`, the onboarding launch and the Xero return. Outside a request (mail) there is no set.
- CORS never reads the session: the hub routes echo a set origin for `my`/`payment-request`/
  `pettycash`, the onboarding routes for `onboarding`, and refuse every other.
- The stored set is re-checked against the regex on every read, so unsetting the variable turns
  it all off at once. A value that could let in any host outside `https://pr-…\.dailyminty\.com$`
  refuses to boot (`minty_shared.config.preview_origin_regex`, every backend's rule).
- `next` stays path-only: the set chooses the host, never the URL.

**Which environment carries it is minty-infra's `var.preview_backend`: production until the
November launch (owner, 2026-10-13), development after** - so until then a preview signs in with
production tokens against production data.

### 6.2 The alternate set — a second copy of the front ends (`EXTRA_FRONTEND_ORIGINS`, 2026-10-28)

For the `redesign` Workers on `*.workers.dev`, **`EXTRA_FRONTEND_ORIGINS`** lists one more set
origin by origin: `my=https://…,payment-request=https://…,onboarding=https://…,pettycash=https://…`
(any subset). It reuses 6.1's machinery under the stored slug `~alt` (`hub/links.ALT_SLUG`):

- A `return_origin` exactly equal to one of them names the alternate set **from any trip**,
  cross-site included: a workers.dev page is never same-site with dailyminty.com, and the hosts
  are exact and ours.
- Hand-offs go to the listed origin for that app; an app it does not list keeps its configured
  origin. The 30-minute lapse and "a configured origin leaves it" apply as in 6.1.
- CORS echoes a listed origin only on its own app's surfaces (hub: `my`/`payment-request`/
  `pettycash`; onboarding routes: `onboarding`).
- Unset, a stored alternate set is ignored at once. A malformed value (an unknown app, a repeat,
  anything but a bare lowercase `https://host`) refuses to boot (`config/settings.py`).
- minty-onboarding-web sends no `return_origin`, so its own trips here (the Xero return) land on
  the configured onboarding app.

## 7. Xero OAuth tokens (the company's accounting connection)

Different thing from sign-in: a **per-person** `user_token` row (access token, refresh token,
expiry), written when that person connects or reconnects a company (`/xero_connect`,
`/xero_reconnect`; the same minimal scope set, `hub/xero_oauth.CONNECT_SCOPE`) or signs in with
Xero. The company remembers who connected it (`entities.connected_by_user_id`), and that is whose
token every Xero call for the company uses.

- **Connecting** (`hub/xero_oauth.py`, `xero/services/connect.py`): the state carries the company,
  its name and who clicked, plus the nonce; the callback checks the Xero login belongs to that
  person, keeps one Xero organisation to one company, needs the organisation's financial year
  end, mirrors its chart and contacts, and hands a refused grant back at Xero. A connect needs
  `XERO_SETTINGS_UPDATE` on a named company; with no company named, nothing is connected.
  Detail: `accounts-api.md`, Xero OAuth.
- **Only this service refreshes** (`xero/services/tokens.py`). Xero rotates the refresh token on
  every use and invalidates the old one, so two refreshers brick the connection. Refreshes are
  serialised per bearer with a Postgres advisory lock held across the HTTP call, and the token
  bundle a connect writes takes the same lock.
- The other backends never refresh: they call `POST /api/internal/xero/token` with a 60-second
  assertion JWT (`scope: xero-access-token`, `entity_id` in the **signed claims**, never the body)
  and get a live access token back; 409 means "reconnect required". The company's accounts and
  contacts come through `POST /api/internal/xero/sync` (scope `xero-sync`). Both are derived from
  the caller's `ACCOUNTS_API_URL` (formerly `PETTY_CASH_URL`).
- minty-petty-cash-api publishes reports, makes the deposit correction, lists the publish reasons
  and draws Petty Cash Settings' Xero cards itself since Phase 4 (2026-11-06), with tokens from
  the service above; Flask's `/api/internal/xero/{publish,deposit-correction,publish-reasons,
  petty-cash-settings}` are gone.
- Disconnecting is `xero/services/disconnect.py`: the grant revoked at Xero (best effort), the
  connector's tokens cleared when it was their last connected company, the company marked
  `disconnected` with its organisation and Xero copy kept (2026-11-10; `accounts-api.md`).
- Tokens are never logged by this service's code. Flask's guard test
  (`tests/test_zz_no_token_logging.py`, a check over every logger call) has no counterpart here.

## 8. Who is signed in (presence)

`hub/session.py`: `signed_in_at` and `last_seen_at` are stamped at sign-in; `last_seen_at` again
at most once a minute per session on signed-in browser traffic (`SEEN_REFRESH_SECONDS`); opening
a company puts a person back on the list keeping the original sign-in time (`resume_presence`).
Signing out and leaving a company blank `signed_in_at`. A failed presence write is logged and
never breaks a sign-in. Nothing shows the list now (the Users tab's "Online Users" section is off),
and there is no presence-window setting. minty-payment-request-api's `POST /api/auth/logout` clears
the stamps the same way, so leaving from the payment module counts as leaving.

## 9. Configuration

| Variable | Used for |
|---|---|
| `SECRET_KEY` | the session data, the code sign-in's hand-off link, the `?flash=` notices, both hand-off JWTs, the internal token assertions — **shared with the four Django APIs** |
| `SMTP_URL`, `INVITE_FROM` | sign-in code and invitation mail (`INVITE_FROM` required at start; `SMTP_URL` outside development) |
| `XERO_CLIENT_ID`, `XERO_CLIENT_SECRET` | both OAuth flows and every refresh (redirect URI `ACCOUNTS_API_URL/callback`; `http://localhost:8000/callback` with it unset in development) |
| `ACCOUNTS_API_URL` (formerly `PETTY_CASH_URL`) | this service's own address (`accounts-api.dailyminty.com`): absolute links in mail, the code sign-in's hand-off URL and the Xero redirect URI. Required outside development |
| `PETTY_CASH_WEB_URL` | minty-petty-cash-web's origin (default `http://localhost:3010`): `/handoff/petty-cash-web`'s target, the share-link redirect, the hub routes' CORS |
| `MINTY_WEB_URL`, `PAYMENT_REQUEST_WEB_URL`, `ONBOARDING_WEB_URL` | the other front ends' origins - the hand-offs, CORS and the sign-out Origin check |
| `PREVIEW_ORIGIN_REGEX` | optional: the branch preview sets this service may hand back into (section 6.1); unset = off |
| `EXTRA_FRONTEND_ORIGINS` | optional: the alternate set's origins, one per app (section 6.2); unset = off |
| `REQUIRE_TERMS_AT_SIGNUP`, `CURRENT_TERMS_VERSION`, `CURRENT_PRIVACY_VERSION` | the Terms gate and sign-up consent (section 4) |

Every service's variables are listed in minty-infra's
[`docs/ENVIRONMENT.md`](https://github.com/minty-oliveandvine/minty-infra/blob/main/docs/ENVIRONMENT.md).

## 10. Where it is tested

`hub/tests/test_email_signin.py` (the front door, code sign-in and sign-up, the lock, consent,
the single-use hand-off, invitations by code), `hub/tests/test_session.py` (the session, presence,
flashes, the headers, the redacted path, the cookie's name and flags),
`hub/tests/test_handoffs.py` (the tokens and their claims, every hand-off, `/enter`, sign-out and
leaving as POSTs from a front end, the Terms gate), `hub/tests/test_xero_oauth.py` (scopes, the
nonce, sign-in and invitations with Xero, connect and reconnect), `hub/tests/test_redirects.py`
(the settings tabs, report history, share links, the old addresses),
`hub/tests/test_public_and_transport.py` (the public routes and the hub's door),
`hub/tests/test_me_routes.py`, `hub/tests/test_company.py`,
`hub/tests/test_xero_disconnect_and_onboarding.py` and `xero/tests/test_internal_token.py` (the
token service and its assertion). There is no route-walking check that every state-changing
route asks a permission (Flask's `tests/test_user_permission_matrix_coverage.py`). The sign-in
screens are tested in minty-web's e2e suite.
