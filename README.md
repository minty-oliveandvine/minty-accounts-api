# minty-accounts-api

Minty's accounts service: Django 5.2 on Python 3.13, local port **8002**. It is the Flask
retirement's front door (`Minty`, plan: the Flask retirement's Phase 1, 2026-11-05). It takes
Flask's public address (`accounts-api.dailyminty.com`), answers what it has been given, and
hands everything else to Flask unchanged.

**Status: Phase 1 - the fall-through.** Django owns `/health` only. Every other path goes to
Flask through `core/fallthrough.py`: the method, raw path, query, body and headers, and back
Flask's status, headers (both `Set-Cookie`s) and body, streamed and never decompressed;
redirects are returned, not followed. Each later phase moves a group of Flask's routes into
Django (`config/urls.py`); a path that resolves there stops falling through. No database yet:
`minty-db` arrives with the first ported route (Phase 2, the Xero token service).

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
| `ALLOWED_HOSTS` | no (`*`) | Comma-separated |
| `DJANGO_DEBUG` | no | `1` turns Django's debug on; never from `APP_ENV` |
| `LOG_LEVEL` | no (`INFO`) | |

The service refuses to start without either required value.

## Checks

```
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m pytest
```

CI is the shared `python-api.yml@v1` (`.github/workflows/ci.yml`). Feature detail:
`docs/features/accounts-api.md`.
