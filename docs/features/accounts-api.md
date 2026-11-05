# The accounts API and its fall-through to Flask

## What it does (Phase 1, 2026-11-05)

minty-accounts-api stands where Flask stood, at `accounts-api.dailyminty.com` (development:
`dev-accounts-api.`). Web apps, the other APIs and Xero's callback URL keep calling the same
address. Flask runs on behind it, reached only at `FLASK_FALLBACK_URL`.

| Path | Answered by |
|---|---|
| `/health` | Django: `{"status": "ok", "service": "minty-accounts-api"}`. It never calls Flask |
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

## Later phases

Each phase moves a group of Flask's routes into `config/urls.py`: a path that resolves in Django
stops falling through. The order and the boundaries are in the Flask retirement plan; Phase 2 is
the Xero token service (`POST /api/internal/xero/token`).

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

`tests/test_settings.py` covers the refusal to start without `SECRET_KEY` or
`FLASK_FALLBACK_URL`.

Checked locally on 2026-11-05: through 8002 to Flask on 8000, the following matched Flask
directly, headers and body:
- `/auth/notices`, `/legal/current`;
- `/profile` and `/entity?…` (302 with `Set-Cookie`);
- `/api/me/entities` (401) and an unknown path (404);
- a refused `POST /auth/email/request-code` (400).

The OPTIONS preflight differed only by `runserver` adding `Content-Length: 0` to the 204.
