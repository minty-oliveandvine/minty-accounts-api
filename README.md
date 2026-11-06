# minty-accounts-api

Minty's accounts service: Django 5.2 on Python 3.13, local port **8002**. It is the Flask
retirement's front door (`Minty`, plan: the Flask retirement's Phase 1, 2026-11-05). It takes
Flask's public address (`accounts-api.dailyminty.com`), answers what it has been given, and
hands everything else to Flask unchanged.

**Status: Phase 3 - the Xero token service and the one Xero sync.** Django owns `/health`,
`POST /api/internal/xero/token` (`xero/`, moved from Flask 2026-11-05: the other backends' only
way to a Xero access token, refreshed behind Flask's advisory lock) and
`POST /api/internal/xero/sync` (2026-11-06: a company's Xero accounts and contacts into Minty's
tables, for every service). Every other path goes
to Flask through `core/fallthrough.py`: the method, raw path, query, body and headers, and back
Flask's status, headers (both `Set-Cookie`s) and body, streamed and never decompressed;
redirects are returned, not followed. Each later phase moves another group of Flask's routes
into Django (`config/urls.py`); a path that resolves there stops falling through. Nothing is
deployed until every phase is finished (owner, 2026-11-05).

## Run it locally

```
uv venv --python 3.13 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
copy .env.example .env      # then fill SECRET_KEY (the same as Flask's)
.venv/Scripts/python.exe manage.py runserver 127.0.0.1:8002
```

Flask must be running on 8000. `FLASK_FALLBACK_URL` uses `127.0.0.1`, not `localhost`
(Windows tries `::1` first and each hop waits 2 s).

## Configuration

| Variable | Required | What |
|---|---|---|
| `SECRET_KEY` | yes | The same key as Flask and the other APIs |
| `FLASK_FALLBACK_URL` | yes | Flask's own (internal) address; only the request's path and query are appended |
| `DATABASE_URL` | yes | The shared schema, `?schema=pettycashv3` (`config/dburl.py`) |
| `XERO_CLIENT_ID`, `XERO_CLIENT_SECRET` | yes | Xero's OAuth client, Flask's same app |
| `XERO_API_BASE_URL` | no (`https://api.xero.com/api.xro/2.0`) | Xero's accounting API |
| `ALLOWED_HOSTS` | no (`*`) | Comma-separated |
| `DJANGO_DEBUG` | no | `1` turns Django's debug on; never from `APP_ENV` |
| `LOG_LEVEL` | no (`INFO`) | |

The service refuses to start without any required value.

## Checks

```
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m pytest
```

That is the SQLite pass; the token service's tests need Postgres (advisory locks) and skip
there. Set `MINTY_TEST_PG_URI` (and a unique `MINTY_TEST_PG_DBNAME`) for the Postgres pass,
built from minty-db's schema file.

CI is the shared `python-api.yml@v1` (`.github/workflows/ci.yml`). Feature detail:
`docs/features/accounts-api.md`.
