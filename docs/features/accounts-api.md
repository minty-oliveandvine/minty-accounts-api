# The accounts API and its fall-through to Flask

## What it does (Phase 1, 2026-11-05)

minty-accounts-api stands where Flask stood, at `accounts-api.dailyminty.com` (development:
`dev-accounts-api.`). Web apps, the other APIs and Xero's callback URL keep calling the same
address. Flask runs on behind it, reached only at `FLASK_FALLBACK_URL`.

| Path | Answered by |
|---|---|
| `/health` | Django: `{"status": "ok", "service": "minty-accounts-api"}`. It never calls Flask |
| `POST /api/internal/xero/token` | Django (Phase 2): the Xero token service, below |
| `POST /api/internal/xero/sync` | Django (Phase 3): the one Xero account/contact sync, below |
| `GET /auth/notices`, `GET /legal/current`, `POST /legal/invite-terms-status` | Django (Phase 5A, `hub/public.py`) |
| `GET /api/me/entities`, `GET/PATCH /api/me/profile`, `GET /api/me/terms`, `POST /api/me/terms/accept`, `POST /api/me/contact` | Django (Phase 5B, `hub/`) |
| `GET /api/me/company/users`, `PATCH/DELETE /api/me/company/users/<id>`, `POST /api/me/company/invitations`, `POST .../invitations/<id>/cancel` and `/resend`, `GET/PATCH /api/me/company/integration` | Django (Phase 5C, `hub/company.py`) |
| `POST /api/me/company/xero/disconnect`, `POST /api/me/company/xero/release` | Django (Phase 5D, `hub/company.py`, `xero/services/disconnect.py`) |
| `POST /api/onboarding/modules`, `GET/POST /api/onboarding/invite`, `POST /api/onboarding/invite/resend` and `/cancel`, `POST /api/onboarding/xero/disconnect` and `/release` | Django (Phase 5D, `hub/onboarding.py`) |
| `GET /static/img/new_logo.png` | Django (Phase 5C): the invitation email's logo, the same file Flask serves |
| `/`, `/index`, `/register`, `POST /auth/email/request-code`, `/verify-code`, `GET /auth/email/handoff`, `/invitation/accept/<token>` | Django (Phase 6B, `hub/email_signin.py`, `hub/front_door.py`) |
| `/entity`, `/entity/create`, `/entity/<co>/petty-cash`, `/modules`, `/enter`, `/payment-request`, `/handoff/minty-web`, `/handoff/petty-cash-web`, `/profile`, `POST /logout`, `POST /leave-entity` | Django (Phase 6C, `hub/handoffs.py`) |
| `/xero_auth`, `/xero_connect`, `/xero_reconnect`, `/callback` | Django (Phase 6D, `hub/xero_oauth.py`, `xero/services/connect.py`) |
| `/entity/<co>/settings/*`, `/petty-cash/reports`, `/xero-not-connected`, `/Minty_Report/<path>/` and the old addresses (308) | Django (Phase 6E, `hub/redirects.py`) |
| anything else | Flask, through the fall-through |

## The fall-through (`core/fallthrough.py`)

It runs at the WSGI level in front of Django (`config/wsgi.py`), so Django's CSRF, session and
response machinery never touch Flask's traffic.

**Forwarded to Flask:**
- the method;
- the path and query exactly as sent (gunicorn's `RAW_URI`);
- the body (at most 32 MiB; more is a 413 that never reaches Flask);
- every header except the hop-by-hop ones and `Host`.

It also sets three headers:
- `X-Forwarded-For` keeps the client's value with this hop's peer appended;
- `X-Forwarded-Host` and `X-Forwarded-Proto` are kept, or set when absent;
- `X-Request-ID` keeps a valid incoming trace or starts one (`minty_shared.logctx`).

**Returned:** Flask's status, every header (duplicate `Set-Cookie` included) minus the
hop-by-hop ones, and the body as raw bytes, streamed. A compressed body stays compressed. A
redirect comes back as a redirect and is never followed.

**Never:**
- a retry (a POST reaches Flask once or not at all);
- a cookie kept between requests;
- a proxy or credentials read from the environment;
- an upstream other than `FLASK_FALLBACK_URL`. Only the path is appended, so the service is not
  an open proxy.

**Flask down:** a 502 with `{"error": "Something went wrong on our side. Please try again."}`,
logged with the trace. Timeouts are 5 s to connect and 150 s to answer: Petty Cash's Xero
publish runs through Flask, and petty-cash-api allows 130 s.

It uses urllib3 directly, not requests. A requests `Session` reads a redirect's body even when
told not to follow it, which left the client waiting for bytes that never came. It also keeps
`Set-Cookie`s in a jar shared across requests.

## Flask behind it

Flask builds every emailed or handed-off link from `ACCOUNTS_API_URL`, never from the request's
host:
- the sign-in hand-off (`_mint_handoff_url`, 2026-11-05);
- the invitation;
- the Xero `REDIRECT_URI`.

Behind the fall-through, a request arrives on Flask's internal address, and anyone can call that
address directly with any `Host`.

## The Xero token service (Phase 2, 2026-11-05)

`POST /api/internal/xero/token` (`xero/views.py`, `xero/services/tokens.py`) hands a
currently-valid Xero access token to minty-petty-cash-api, minty-onboarding-api and
minty-payment-request-api. They never refresh: Xero rotates the refresh token on every use, so
two refreshers would leave a dead token and break the connection until someone reconnects.

**Who may ask:** a Minty backend with a 60-second HS256 assertion
(`minty_shared.tokens.service_assertion`, the shared `SECRET_KEY`), `scope` =
`xero-access-token`, decoded with every backend's 60 s skew. The company is the assertion's
`entity_id`, never the body. Any failure is a 401.

**The answers** are Flask's `jsonify` bytes (sorted keys, compact, a trailing newline), because
the callers' `main` reads them:

| Status | Body |
|---|---|
| 200 | `{"access_token":"…","xero_org_id":"…"}` |
| 401 | `{"message":"unauthorized","status":"error"}` |
| 409 | `{"message":"No valid Xero token for this entity","status":"reconnect_required"}` |

A 409 means a person has to reconnect, or another refresh is still running; it is never a server
error. A company with no Xero org, no `connected_by_user_id`, a missing connector, or a connector
with no `user_token` bundle all answer 409, as does an `entity_id` that is not a uuid.

**The token** belongs to the person who connected the company (`entities.connected_by_user_id`)
and lives in `user_token`. It counts as fresh while it has more than 300 s left
(`access_token_expires_in` after `access_token_obtained_at`).

**The refresh lock:**
- Postgres `pg_try_advisory_lock(1481594447, hashtext('xero_refresh:<user id>'))`, polled every
  0.1 s for up to 5 s and held across the call to Xero. That call is what spends the refresh token.
- It is Flask's exact key, so while Flask still refreshes through its own service path, the two
  wait for each other.
- Under the lock the row is read again. A fresh token is handed out, even if the wait ran out.
- If the wait ran out and the token is stale: 409, and no refresh.
- Otherwise it refreshes.

**The refresh:**
- `POST https://identity.xero.com/connect/token` with Basic `XERO_CLIENT_ID:XERO_CLIENT_SECRET`
  and `grant_type=refresh_token`; timeout 5 s connect, 10 s read.
- A refusal, an error, or a 200 with no access token: 409, and nothing is written, so the same
  refresh token is presented next time.
- Success writes each part Xero sent and stamps `access_token_obtained_at` and
  `refresh_token_last_used_at` with now.
- A failed write after Xero rotated the token is not swallowed: it is a 500, logged. Flask
  returned a 409 there.

**Not covered by the lock:** Flask's other refreshers take none (`ensure_valid_token` from
`/refresh_token`, `billing_sync.py`, `suppliers.py`, the onboarding and settings services).
They move in Phase 4.

**Locally,** the databases are production copies, so a real refresh would rotate a production
token and break that company's connection. The local check on 2026-11-05 ran with outbound
HTTPS sent to a dead proxy. Through accounts-api and through Flask, the 401 (no bearer, wrong
scope) and the 409 (no Xero org) matched Flask's bytes (the local Flask runs in debug mode and
pretty-prints; production's are compact). A stale token went through the lock, its refresh was
blocked, and it answered 409 with nothing written.

## The Xero account and contact sync (Phase 3, 2026-11-06)

`POST /api/internal/xero/sync` (`xero/views.py`, `xero/services/sync.py`) brings a company's Xero
accounts and contacts into Minty's tables. There is one sync, here, for every service (owner's
choice, 2026-11-06). Petty Cash Settings asks for it now; onboarding and Payment Requests will as
their Xero routes move (Phase 4).

**Who may ask:** a 60-second assertion scoped `xero-sync`. The company comes from its `entity_id`.
The token comes in-process from the token service above, refreshed when due.

**Answers:**

| Status | Body |
|---|---|
| 200 | `{"accounts": counts or null, "contacts": counts or null}`. Null means Xero couldn't be asked for that half, and nothing changed. |
| 409 | `{"error", "reconnect": true}`: no usable token. |
| 401 | A bad assertion. |

It asks Xero for the ACTIVE accounts **once**. Flask asked three times each time the settings opened.

**Accounts:**
- New ones are added.
- Returning ones are re-activated.
- Renamed or recoded ones are refreshed. A type Petty Cash's chart excludes (bank, equity, sales,
  revenue, other income) keeps its old name, as in Flask.
- Ones Xero no longer has ACTIVE are deleted, and so is a stale ARCHIVED row. The delete cascades
  to `entity_account_xero` and clears the Petty Cash mapping that named the account.

**Account lists:**
- `entity_account_xero` (Petty Cash's codes: direct costs, expense, overheads, prepayment) gains a
  row per eligible account. The person's ticks are never changed here.
- `entity_bill_account_xero` (Payment Requests' codes, adding fixed) likewise, and loses rows whose
  account is gone.

**Contacts** come page by page (1,000 a page). New ones are added and renamed ones updated. Nothing
is removed: absence from Xero's answer is ambiguous. A failed page discards the whole fetch.

**Safety:**
- A failed or empty fetch changes nothing.
- Each half is one transaction under Flask's lock,
  `pg_advisory_xact_lock(hashtextextended('xero-sync:<entity>', 0))`. So it never overlaps Flask's
  own syncs (connect, onboarding, the nightly run) while they exist.

Tests: `xero/tests/test_sync.py` (Postgres only).

## The hub and the public routes (Phase 5, 2026-11-06; built and tested locally, not deployed)

Flask's bearer and public routes, moved with their paths, answers (Flask's `jsonify` bytes,
`core/flask_json.py`) and rules.

**The hub's door** (`hub/transport.py`):
- Any valid Minty token passes (HS256, 60 s skew, no scope check); the person must exist and be
  approved, else 401 `{"error": "unauthorized"}`.
- CORS answers minty-web, the payments app, Petty Cash, their preview sets and
  `EXTRA_FRONTEND_ORIGINS`. Refusals carry the headers too.

**Shared pieces:**
- `hub/policy.py`: the other services' permission rules, copied unchanged.
- `hub/registry.py`: Flask's legal manifest, copied unchanged.
- `hub/notices.py`: reads the `?flash=` and `?xero_conflict=` hand-overs, which Flask's session
  routes sign until Phase 6 (itsdangerous 2.2.0).

**5A, the public routes:**
- The sign-in page's notices.
- The live Terms and Privacy versions.
- Whether an invitee still owes a Terms acceptance. This one fails safe to "required".

**5B, the hub's own routes:**
- **The entity list.** Its trial and payment-failed badges come from minty-subscription-api's
  read-only facts (`core/subscription_client.py`). When that service can't answer within 2/4 s,
  the list loads without them.
- **My Profile.** A changed email moves the username when the username was the email; a taken
  address is 422.
- **The Terms gate.** The recorded fingerprint is the manifest's; a mismatch is 409
  `document_mismatch`; recording is idempotent, `source = hub`.
- **Talk to Minty.** It mails `CONTACT_EMAIL` from `CONTACT_FROM`, with Reply-To the customer's
  address. An unset inbox or sender is a 500, a failed send a 502, and the throttle is 5 an hour
  per person.

**5C, a company's Users and Entity & Integration tabs** (`hub/company.py`). The company always
comes from `?entity=`, never the body. Each action checks its own permission, then the rank rule:
nobody gives a role above their own or manages someone who matches or outranks them.
- **Users.** The members, the pending invitations, the roles this person may give, and who pays.
  The payer comes from minty-subscription-api; when it can't answer, the badge is just missing.
- **Invitations** (`hub/invitations.py`, Flask's invite service).
  - The address follows the invitations' email rule and both names are needed; the role is one of
    the four assignable roles.
  - A current member, or an unexpired pending invitation for the same address, is a 409. An
    expired one is marked `expired` and replaced.
  - A resend gets a new token, so the old link dies. It waits 60 s after the last send (429 with
    `retry_after`; the count is per worker, as in Flask), and a send that fails is a 502.
  - The email is Flask's HTML, sent from `INVITE_FROM`. Its accept link is
    `{ACCOUNTS_API_URL}/invitation/accept/<token>?fn=&ln=`, which stays Flask's page until Phase 6.
    The logo is `/static/img/new_logo.png`, the same file Flask serves, fingerprinted by content.
  - A failed first send keeps the invitation and answers `email_sent: false`.
- **A role change or a removal** passes Flask's guards, in Flask's order:
  - the payer can't be removed, or demoted out of admin;
  - a company never loses its only admin (deactivated accounts don't count);
  - nobody being offered the subscription handover can be changed.
  
  The payer guards **fail closed**: if minty-subscription-api can't answer, nothing changes (503).
  The handover guard **fails open**, as Flask documents it. A change that stays at or below admin
  never asks.
- **Entity & Integration.**
  - The name, country and currency, with the active countries and currencies to pick from.
  - The Xero state, checked live against Xero's `/connections` with the connector's token from this
    service's own token service. The organisation name is filled in when missing.
  - A save renames the company (admins only; names are unique per person; 422 sentences), and sets
    the country and currency. A country sent on its own brings its own currency, and the company's
    notes and coins follow the currency (`hub/cash_settings.py`, onboarding-api's copy).
  - A failed save is a 500 and changes nothing.
  - `?xero_conflict=` names the company holding the organisation; "move it here" is offered only in
    development.

**Mail and settings:**
- Mail goes through `SMTP_URL`, which is required outside development.
- `INVITE_FROM` is required.

**5D, Xero disconnect and the onboarding routes:**
- **The one disconnect** (`xero/services/disconnect.py`, Flask's):
  1. It revokes the company's grant at Xero, as a best effort. The connector's token comes from
     this service's own token service.
  2. It clears the connector's tokens when this was their last connected company.
  3. It drops the company's Xero cache: the account and contact mappings, the organisation's
     contacts, `account_info` and `entity_bill_account_xero`.
  4. It marks the company `disconnected`.

  Steps 2-4 are one transaction. A failure is a 502 that says the company may still show as
  connected.
- **The hub's Disconnect** answers the fresh Entity & Integration page. Its **release** frees the
  company holding the organisation. That release is authorized on the company being freed, refused
  for the company itself, and available in development only.
- **The onboarding routes** (`hub/onboarding.py`). minty-onboarding-api forwards its caller's token
  to them, and minty-subscription-api's "invite an admin" forwards to `/invite`. Both reach them
  through `PETTY_CASH_URL`, so they need no change.
  - Flask's onboarding door applies: any valid token, 401 `{"error": "Unauthorized"}`, and CORS for
    the onboarding app with `GET, POST, PUT, OPTIONS`.
  - The answers use Flask's words.
  - **Modules:** the chosen modules on and the rest off, recording who chose. Only an **approved**
    member may choose. Flask took any membership row; this is tightened on purpose.
  - **Invitations:** list, send, resend and cancel.
  - **Disconnect:** onboarding's own. The company stays `onboarding`, and its synced lists are kept
    (owner, 2026-11-06).
  - **Release:** the shared disconnect of another company, in development only.

Nothing in Phase 5 is left to move. The sign-in codes and their mail, the invitation's accept page
and every session route are Phase 6.

## The browser session (Phase 6, 2026-11-06; built locally, not deployed)

Everything that used Flask's session cookie, moved in one release (everyone signs in again once).

- **The session** is Django's (`django.contrib.sessions`, table `django_session`, created by hand
  with `manage.py migrate sessions` on each database - the schema file's note B6; the Postgres test
  database gets the same step in the root `conftest.py`). Cookie `session` (Flask's name, so the old
  cookie is replaced), 24 h, HttpOnly, SameSite=Lax, Secure outside development. There is no
  `django.contrib.auth`: `hub/session.py` keeps the person's id and the sign-in's id (`login_sid`,
  the module tokens' `sid`); `sign_in` cycles the key (no fixation) and stamps presence, as
  Flask-Login's signal did. Flashed messages wait in the session until a redirect signs them into
  `?flash=` (`hub/notices.sign_notices`, the same itsdangerous format Flask writes).
- **Headers** (`core/hardening.py`): Flask's `Referrer-Policy`, `nosniff`, `X-Frame-Options`, HSTS
  outside development; `/invitation/accept/<token>` is `no-store` and logged as `[redacted]`.
- **Preview sets** (`hub/links.py`): `?return_origin=` and the stored set, Flask's rules;
  `hub_login_url` builds minty-web's `/login` / `/signup` addresses.
- **Company addresses** (`hub/addresses.py`): the `<entity:...>` converter (`<shortid>/<name>` or a
  full uuid); a GET that named a company any other way is 308'd to the canonical address.
- **The Terms gate** (`hub/terms_gate.py`): a cookie-signed-in person who owes the live Terms is
  sent to `/entity` (minty-web draws the panel), JSON gets 403 `terms_acceptance_required`; the way
  in and out and the acceptance flow are open; cached per session per version; fails open, loudly.

**Signing in by code** (`hub/email_signin.py`, Flask's rules): 60 s codes, 60 s resend cooldown,
5 tries then a 15-minute lock carried across resends, login mode refuses an unknown address, a
failed send is rolled back. Codes keep werkzeug's `pbkdf2:sha256` format, so a code either side
issued verifies on the other. Sign-up makes the account and its Terms agreement in one transaction.

**Gaps fixed in the move (owner, 2026-11-06):**
1. **The hand-off is single use.** Verify-code parks it as a pending `django_session` row
   (5 minutes); the URL carries its key, signed. `/auth/email/handoff` deletes the row and signs in
   only if the delete removed it, so a replay, a copied link or a second tab signs nobody in.
2. **Xero OAuth `state` carries a per-session nonce** (6D, `/xero_auth`, `/xero_connect`,
   `/xero_reconnect`, `/callback`).
3. **`/logout` and `/leave-entity` are POST only** (GET is 405), and the POST must come from one of
   the front ends (`Origin`; the cookie is SameSite=Lax). The apps sign out with a form post.
4. Tokens still ride in query strings between the apps (`/landing?token=`, `/enter?token=`): a
   follow-up - a one-time code the Next apps exchange server-side, parked the same way as gap 1.

**Xero OAuth** (6D, `hub/xero_oauth.py`, `xero/services/connect.py`): sign in with Xero, and
connect or reconnect a company, with Flask's scopes, state shapes, callback branches,
one-organisation-one-company checks, grant revokes, financial year end and connect-time mirror. The
post-connect sync reuses `xero/services/sync.py`, on a thread. The callback answers a nonce it did
not issue before exchanging the code. Two more changes from Flask:
- **Connecting needs `XERO_SETTINGS_UPDATE` on the company.** Flask checked only `ENTITY_CREATE`
  (everyone) and wrote the organisation onto whatever company id the state named, overwriting its
  connection and pruning its accounts. Now `/xero_connect` refuses a named company the person may
  not connect, and the callback checks the Xero login's owner and revokes the new grant if they
  fail. Flask's last fallback, "the newest company of anyone", is gone: with nothing named, nothing
  is connected.
- A refused code exchange is a sentence, not a 500, and the token write on a connect is in the same
  transaction as the company's.
- A company name holding `:` keeps its name and the wrong-login check: the callback reads the
  initiator from the right of the state (Flask split from the left and skipped the check).
- A Xero login no Minty account holds is told so, and its grant is handed back at Xero (Flask said
  nothing and left it live). The onboarding wizard sees `xero=cancelled`, as for other failures.
- `/callback` is open past the Terms gate. Its start routes stay gated, so only a flow begun while
  agreed lands, and a Terms change mid-connect no longer throws away what Xero granted.

Like Flask, the id_token's signature is not checked against Xero's keys: the token comes straight
from Xero's token endpoint over TLS, in exchange for the code and the client secret.

**Hand-offs** (`hub/handoffs.py`): the module token (30 min, `module: "billing"`) and the onboarding
token (60 min, `scope: "onboarding"`) with Flask's claims exactly; `/enter` turns only a module
token for a member into a session. **Redirects** (`hub/redirects.py`): the settings tabs, report
history, share links and the old addresses, with Flask's guards.

**Not moved** (no caller anywhere, checked 2026-11-06): `/check/token`, `/refresh_token`,
`/minty/api/users/*`, `/api/entities/<id>/payment-methods*`, `/api/xero/bank-transactions/*`,
`/api/entity/<id>/xero-sync-status`, `/xero-data`, `/entity/<co>/billing-relogin`. They fall
through until Phase 7 removes the fall-through.

## Later phases

Each phase moves a group of Flask's routes into `config/urls.py`: a path that resolves in Django
stops falling through. The order and the boundaries are in the Flask retirement plan; Phase 3
moves Petty Cash's Xero work into minty-petty-cash-api.

## Tests

`tests/test_fallthrough.py` runs a local HTTP server in Flask's place and calls the WSGI
application directly. It covers:
- routing;
- status, query, method and body;
- redirects (body included);
- both `Set-Cookie`s, and no cookie carried to the next request;
- gzip passthrough;
- the forwarded headers, the trace and the hop-by-hop headers;
- the 413 and the 502.

`tests/test_settings.py` covers the refusal to start without each required value.

`xero/tests/test_internal_token.py` (Postgres only) has three groups:
- Flask's five characterisation tests, ported;
- the exact bytes, the company read from the claims, the 409 cases, POST only, and the route
  owning its path;
- the lock, held by a second session as Flask would hold it: a stale token is not refreshed, a
  fresh one is still handed out, and the lock is released afterwards.

`hub/tests/` restates Flask's hub tests for the moved routes (public routes and the door, the
entity list, My Profile, Terms, Talk to Minty, and the company tabs with their membership guards and
invitations). minty-subscription-api is faked at `core.subscription_client`; a root fixture blocks
any real call to it. Mail goes to Django's in-memory outbox.

Checked locally on 2026-11-05: through 8002 to Flask on 8000, the following matched Flask
directly, headers and body:
- `/auth/notices`, `/legal/current`;
- `/profile` and `/entity?…` (302 with `Set-Cookie`);
- `/api/me/entities` (401) and an unknown path (404);
- a refused `POST /auth/email/request-code` (400).

The OPTIONS preflight differed only by `runserver` adding `Content-Length: 0` to the 204.
