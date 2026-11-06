# minty-accounts-api

Minty's accounts service: Django 5.2 on Python 3.13, local port **8002**, at Flask's old public
address (`accounts-api.dailyminty.com`). Sign-in by code and with Xero, the browser session and
every hand-off into the apps, the hub's company and profile APIs, the onboarding routes, the Xero
token service and the one Xero sync. It replaced Flask (`Minty`) in the Flask retirement
(2026-11-05 to 11-06); the fall-through to Flask that carried the rest during the move was
removed in Phase 7, so a path it does not route is a 404. What it serves:
`docs/features/accounts-api.md`.

## Run it locally

```
uv venv --python 3.13 .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
copy .env.example .env      # then fill SECRET_KEY (the same as Flask's)
.venv/Scripts/python.exe manage.py runserver 127.0.0.1:8002
```

`SUBSCRIPTION_API_URL` uses `127.0.0.1`, not `localhost` (Windows tries `::1` first and each call
waits 2 s).

## Configuration

| Variable | Required | What |
|---|---|---|
| `SECRET_KEY` | yes | The same key as Flask and the other APIs |
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
