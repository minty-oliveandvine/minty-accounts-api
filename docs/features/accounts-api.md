# The accounts API

## What it does (Phases 1-7, 2026-11-05 to 11-06)

minty-accounts-api stands where Flask stood, at `accounts-api.dailyminty.com` (development:
`dev-accounts-api.`). Web apps, the other APIs and Xero's callback URL keep calling the same
address. Until Phase 7 it fell through to Flask for every path it did not serve; that
fall-through is gone (2026-11-06), so a path not below is a 404, and Flask is no longer used.

| Path | Answered by |
|---|---|
| `/health` | Django: `{"status": "ok", "service": "minty-accounts-api"}`. It never calls Flask |
| `POST /api/internal/xero/token` | Django (Phase 2): the Xero token service, below |
| `POST /api/internal/xero/sync` | Django (Phase 3): the one Xero account/contact sync, below |
| `POST /api/handoff/redeem` | Django (gap 4, 2026-11-09, `hub/handoffs.py`): an app's landing swaps its one-time code for its token |
| `GET /auth/notices`, `GET /legal/current`, `POST /legal/invite-terms-status` | Django (Phase 5A, `hub/public.py`) |
| `GET /api/me/entities`, `GET/PATCH /api/me/profile`, `GET /api/me/terms`, `POST /api/me/terms/accept`, `POST /api/me/contact` | Django (Phase 5B, `hub/`) |
| `GET /api/me/company/users`, `PATCH/DELETE /api/me/company/users/<id>`, `POST /api/me/company/invitations`, `POST .../invitations/<id>/cancel` and `/resend`, `GET/PATCH /api/me/company/integration` | Django (Phase 5C, `hub/company.py`) |
| `POST /api/me/company/xero/disconnect`, `POST /api/me/company/xero/release` | Django (Phase 5D, `hub/company.py`, `xero/services/disconnect.py`) |
| `POST /api/me/entities/<id>/archive`, `POST /api/me/entities/<id>/restore` | Django (2026-11-10, `hub/entities.py`): archived companies, below |
| `POST /api/onboarding/modules`, `GET/POST /api/onboarding/invite`, `POST /api/onboarding/invite/resend` and `/cancel`, `POST /api/onboarding/xero/disconnect` and `/release` | Django (Phase 5D, `hub/onboarding.py`) |
| `GET /static/img/new_logo.png` | Django (Phase 5C): the invitation email's logo, the same file Flask serves |
| `/`, `/index`, `/register`, `POST /auth/email/request-code`, `/verify-code`, `GET /auth/email/handoff`, `/invitation/accept/<token>` | Django (Phase 6B, `hub/email_signin.py`, `hub/front_door.py`) |
| `/entity`, `/entity/create`, `/entity/<co>/petty-cash`, `/modules`, `/enter`, `/payment-request`, `/handoff/minty-web`, `/handoff/petty-cash-web`, `/profile`, `POST /logout`, `POST /leave-entity` | Django (Phase 6C, `hub/handoffs.py`) |
| `/xero_auth`, `/xero_connect`, `/xero_reconnect`, `/callback` | Django (Phase 6D, `hub/xero_oauth.py`, `xero/services/connect.py`) |
| `/entity/<co>/settings/*`, `/petty-cash/reports`, `/xero-not-connected`, `/Minty_Report/<path>/` and the old addresses (308) | Django (Phase 6E, `hub/redirects.py`) |
| anything else | 404 (no fall-through to Flask since Phase 7) |

## The fall-through (removed in Phase 7, 2026-11-06)

While the phases moved Flask's routes over (2026-11-05 to 11-06), `core/fallthrough.py` sat in
front of Django at the WSGI level and forwarded every path Django did not route to Flask
(`FLASK_FALLBACK_URL`), unchanged both ways. Once every path anything calls was here, it was
removed with its tests and `FLASK_FALLBACK_URL`: on development the Flask service behind it had
been suspended first and nothing failed, and the routes no caller uses (`/check/token`,
`/refresh_token`, `/minty/api/users/*`, payment-methods, bank-transactions, `xero-sync-status`,
`xero-data`, `billing-relogin`) were never ported. Flask's code stays in the Minty repo, unused.

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

**The Xero copies** (Xero optional, minty-db v0.5's EXPAND, 2026-11-10; Minty
`docs/modernisation/xero_optional_database.md`):
- Every account and contact Xero sent is upserted into `xero_account` / `xero_contact`, keyed by
  company, organisation and Xero id, with Xero's values verbatim. The code is read as the schema's
  `account_code_from_xero` reads it: none for a bank account whose code field holds its name.
- The Minty row with that Xero id is linked to its copy (`xero_account_link_id` /
  `xero_contact_link_id`).
- An account copy Xero no longer sends is marked `DELETED`, never removed: a link or a published
  record may name it.
- A row the company made in Minty (no Xero id) is left alone by the sync.

**Disconnect keeps the organisation (2026-11-10, the design's section 4):** the shared disconnect
(`xero/services/disconnect.py`) sets `status = 'disconnected'` (the trigger makes `xero_status =
'disconnected'`) and clears only the connector and, when it was their last connected company, their
tokens. The organisation, the synced chart and contacts, the bill codes and the Petty Cash mapping
all stay, so the company works on in Minty and a reconnect to the same organisation finds them.
Everything that acts at Xero already goes by `status`. A kept organisation is no claim on it:
`connect.live_org_claimant` skips a `disconnected` claimant without touching it, a ghost claimant
(`reconcile_ghost`) is marked `disconnected` and keeps its organisation, and the hub's "move it
here" counts a `disconnected` company as already free. A connect to another organisation replaces
the copy (`mirror_on_connect`). Onboarding's own Disconnect still clears the organisation (an
`onboarding` company holding one reads as connected). Elsewhere: minty-petty-cash-api's Settings
`xero_connected` and minty-payment-request-api's token lookup refuse a `disconnected` company;
minty-web hides Disconnect once it is disconnected.

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
  address is 422. `user.can_view_subscriptions` (2026-11-12) tells the shared sidebar whether to
  offer Manage subscriptions. It is true for anyone with an approved membership of accountant or
  higher, or who pays for any company (`entity_module_subscription.payer_user_id`). The list's rows
  and who may change them are minty-subscription-api's (`can_manage`).
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
  through `ACCOUNTS_API_URL`, so they need no change.
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
4. **No token in an address (owner, 2026-11-09).** Each hand-off into an app carries a one-time
   code (`hub/parked.py`, gap 1's parking, now shared: single use, 2 minutes, bound to the app's
   origin) that the app's landing swaps at `POST /api/handoff/redeem`; `/enter` works on the
   session alone. authentication.md section 6 has the detail.

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

## The whole stack

`docker/stack/`, `scripts/e2e_seed.py` and the stack e2e job moved here from Minty in Phase 7
(2026-11-06): `stack-e2e.md`.

## Tests

`tests/test_settings.py` covers the refusal to start without each required value;
`tests/test_e2e_seed.py` the stack's seed.

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

## Archived companies (owner, 2026-11-10)

`entities.archived_at` (with `archived_by_user_id`) archives a company. It is a column, not a
`status` value, because the Xero code rewrites `status` (connect, disconnect, the settings page's
sync) and the Xero-optional CONTRACT rebuilds it as `('onboarding','live')`.

- **The list.** `GET /api/me/entities` leaves archived companies out for everyone but a superuser.
  A superuser gets them all, each with `archived_at`, and can narrow the list with
  `?archived=all|active|archived` (default `all`; anything else is a 422). Every answer says
  `is_superuser`, so minty-web knows when to offer the filter.
- **The doors.** An archived company is "no such company" everywhere, superusers included:
  `entity_context` (every `/api/me/company/*` tab and the Petty Cash hand-off), `handoffs._entity`
  (the module chooser, Payment Requests, minty-web and the address redirects), `redirects._gate`,
  invitations (none made; an open one can't be accepted and stays pending), onboarding's Xero
  routes, the Xero connect's company lookup, and `resolve_entity_access_token` (no Xero token for
  the other APIs).
- **Archive.** `POST /api/me/entities/<id>/archive`, **superusers only** (owner, 2026-11-10: a
  company's admins can't archive it; 403 otherwise; 404 for no such company). Archiving an
  archived company keeps its first stamp. It is refused (409, "This company still has a paid
  subscription. Cancel its subscription first, then archive it.") while minty-subscription-api's `entity_facts` lists any `paid_modules` (active or
  past due). A trial, or a cancellation running out its paid time, doesn't block. No answer, or an
  answer without `paid_modules`, archives nothing (503).
- **Restore.** `POST /api/me/entities/<id>/restore`, superusers only (403 otherwise; 404 for no
  such company). It clears both columns.
- The other APIs refuse archived companies at their own doors; the v2→v3 load carries the old
  app's `status = 'deleted'` as `archived_at`.

## Matching a Minty company's accounts to Xero (Settings Phase 2, 2026-11-11)

The design is Minty's `docs/modernisation/xero_optional_database.md` §4 (decisions 5/5a/6/9/12/13/18);
Figma 353:999's SET-1b and frames 3, 3b, 4, 5 are the look. Before this, a connect's mirror deleted every
account with no Xero id (a Minty company's whole chart, the Petty Cash/Bank/Director rows included) and
every supplier with no organisation, leaving expenses, daily-closing accounts and sales methods
pointing at nothing.

- **Connect** (`hub/xero_oauth.py` `_record`): a company with `setup_mode = 'minty'` and its own accounts
  is not mirrored. `matching.begin` fills only the Xero copies (`xero_account` / `xero_contact`) and the
  automatic picks (same code AND type, `account_match_draft.picked_by` NULL); the organisation is
  recorded, the company stays `disconnected` and `minty`, and the person lands on the hub's
  `/settings/integration/match` (accounts-api's `settings_integration_match` redirect). No background
  sync yet; `_sync_connection` never marks such a company connected (`matching.pending`).
- **The page** (`hub/xero_matching.py` over `xero/services/matching.py`, `XERO_SETTINGS_VIEW` to read,
  `XERO_SETTINGS_UPDATE` otherwise): `GET /api/me/company/xero/matching` (rows by group, each with its
  pick, a suggestion - a bank by its number, else the same type with a similar name - a same-code
  other-type conflict, whether a record uses it), `PATCH` `{account_id, xero_account_id | null}` (one pick;
  saved at once; a bank only to a bank, else the same class; daily closing's petty cash and bank never
  one Xero account - refused here and at Finish, `matching.SAME_BANK`), `POST .../refresh` (Xero's accounts again),
  `POST .../finish`, `POST .../contacts`.
- **Used** (`xero/services/usage.py`, owner 2026-11-11, extending the doc's Q8): an expense line, a bill
  line (by id, or by code on an older line), any daily-closing account, or an account combined into it.
  (A sales method named an account for a day, minty-db's unreleased v0.6; withdrawn the same day.)
- **Finish = the replace**, one transaction: 409 while a used account has no pick (or a pick names a
  Xero account that is gone, or one already linked here); accounts picked to one Xero account combined
  (survivor: Xero's code, else most records, else oldest; expenses, bill lines, daily closing, sales
  methods, `account_module`, `entity_account_xero` re-pointed; the others archived with
  `merged_into_id`); each picked account takes Xero's identity IN PLACE (Xero id, code, name, type, link)
  so every reference stays, gets a Petty Cash module row if it had none (X2/X3's tick), and its bill code
  and unpublished bill lines follow a new code; unused unmatched accounts archived (never deleted); all
  logged in `account_change_log` (one batch); drafts deleted; `setup_mode = 'xero'` and
  `xero_publish_from` = today (Hong Kong; owner: only records from then on go to Xero).
- **Suppliers** (after the replace commits): each own supplier linked to the Xero contact of its name
  (case and surrounding spaces ignored; the active one first, else the first by Xero id - the doc's Q9
  default), else created in Xero under its Minty name; one already stored here is merged into it.
  Logged in `contact_change_log`. Any Xero refuses are named; the company stays disconnected until
  `POST .../contacts` gets them all through, then it is `connected` and the first sync starts.
- **The sync never deletes what is used or Minty-made** (also 2026-11-11): `sync.apply_accounts`
  archives a used (or Minty-made) account Xero dropped and leaves rows with no Xero id alone;
  `_mirror_bills` keeps bill codes with no Xero id; the connect's contact clean-up and
  `mirror_on_connect` only remove unused, Xero-made leftovers.
- Not built (Phase 3): matching on a reconnect with accounts added while disconnected, a different
  organisation, a stale match refused at publish. Tests: `hub/tests/test_xero_matching.py`,
  `test_xero_oauth.py` (`test_a_minty_companys_connect_keeps_every_account_and_goes_to_matching`),
  `xero/tests/test_sync.py` (the archive-not-delete cases); Postgres only.
- **While matching** (`setup_mode` still `minty`), `sync.sync_company` - the sync every app asks for -
  refreshes only the Xero copies: importing Xero's chart as accounts put rows beside the company's own
  holding the Xero ids Finish links. Finish removes an unused Xero-made row that holds a picked Xero
  account (one a sync made before this) instead of refusing; a used one still refuses.
