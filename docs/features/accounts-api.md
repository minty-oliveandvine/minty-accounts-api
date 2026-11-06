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

Checked locally on 2026-11-05: through 8002 to Flask on 8000, the following matched Flask
directly, headers and body:
- `/auth/notices`, `/legal/current`;
- `/profile` and `/entity?…` (302 with `Set-Cookie`);
- `/api/me/entities` (401) and an unknown path (404);
- a refused `POST /auth/email/request-code` (400).

The OPTIONS preflight differed only by `runserver` adding `Content-Length: 0` to the 204.
