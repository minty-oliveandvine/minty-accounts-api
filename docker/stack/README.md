# Run the whole Minty system

One command starts seven repos and their shared database. It moved here from
`Minty/docker/stack` on 2026-11-06 (the Flask retirement, Phase 7): minty-accounts-api took
Flask's place as the token issuer, so Flask is not part of it. The same files drive the
`stack-e2e.yml` job (section 9).

| Service (hostname) | Repo | URL | What it is |
|---------|------|-----|------------|
| `accounts-api` | `minty-accounts-api` (this repo) | <http://localhost:8002> | Sign-in, session, hand-offs, hub APIs, Xero tokens — Django |
| `minty-web` | `minty-web` | <http://localhost:3000> | The hub — Next.js |
| `subscription-api` | `minty-subscription-api` | <http://localhost:8000> | Subscriptions API — Django |
| `payment-request-web` | `minty-payment-request-web` | <http://localhost:3020> | Payment Request — Next.js UI |
| `payment-request-api` | `minty-payment-request-api` | <http://localhost:8020> | Payment Request — Django API |
| `onboarding-web` | `minty-onboarding-web` | <http://localhost:3030> | Onboarding — Next.js UI |
| `onboarding-api` | `minty-onboarding-api` | <http://localhost:8030> | Onboarding API — Django |
| `db` | — | `localhost:5433` | PostgreSQL 15, shared by every backend (`db:5432` inside the network) |

Ports follow one slot per domain: `30N0` for a web app, `80N0` for its API; accounts-api keeps
its own local port, `8002`. Petty Cash (minty-petty-cash-api / -web) is not in the stack. Every
variable each service reads is in `Minty/docs/ENVIRONMENT.md`.

---

## 1. Before you start

- **Docker Desktop** installed and running. Compose **v2.24 or newer**
  (`docker compose version`).
- **All seven repos checked out side by side**:

  ```
  C:\Github\
    ├── minty-accounts-api\          ← you are here
    ├── minty-web\
    ├── minty-subscription-api\
    ├── minty-payment-request-web\
    ├── minty-payment-request-api\
    ├── minty-onboarding-web\
    └── minty-onboarding-api\
  ```

  If your layout differs, set the `*_PATH` variables in `.env` (step 2).

You do **not** need Python, Node, or Postgres installed. Minty (Flask) is not needed at all.

---

## 2. Create your settings file (one time)

```bash
cp .env.example .env      # PowerShell: copy .env.example .env
```

Set **`SECRET_KEY`** to a real value (`python -c "import secrets; print(secrets.token_hex(32))"`).
accounts-api signs the JWT that hands a signed-in user over to another module, and the other
APIs verify it with the same key. If they differ, every module handoff fails with a 401.

accounts-api refuses to start without `XERO_CLIENT_ID`, `XERO_CLIENT_SECRET` and `INVITE_FROM`.
Your `../../.env` (this repo's) normally has them and is loaded underneath the stack's `.env`;
without one, set them in the stack's `.env` (placeholders are fine if nobody signs in with Xero -
the CI job does exactly that).

---

## 3. Getting a schema (before the first start)

The database starts **empty**, and no service migrates on start. Build it from **minty-db**
(the schema's source of truth, already installed in accounts-api's image), then give
accounts-api its session table:

```bash
docker compose up -d --wait db
docker compose run --rm --no-deps accounts-api python -m minty_db migrate
docker compose run --rm --no-deps accounts-api python manage.py migrate sessions
```

`python -m minty_db migrate` builds `pettycashv3` from the packaged `01_schema_rebased.sql` and
records `0001`; `migrate sessions` creates `django_session` (the schema file's note B6:
framework tables stay with the framework).

A schema built from the file has **no subscription catalogue** (`billing_plan`,
`billing_policy`), so no trial can start. Load minty-db's `seed_catalogue.sql` (idempotent, never
updates a row):

```bash
docker compose run --rm --no-deps -T accounts-api \
  python -c "import sys, minty_db; sys.stdout.write(minty_db.CATALOGUE_SQL.read_text(encoding='utf-8'))" \
  | docker compose exec -T db psql -v ON_ERROR_STOP=1 -U minty_ref_user -d minty_ref
```

Or restore a dump of a working database instead (`docker compose exec -T db psql -U
minty_ref_user -d minty_ref < your_dump.sql`); then run only `migrate sessions` if it has no
`django_session`.

---

## 4. Start everything

```bash
docker compose up --build
```

The first run takes several minutes. Startup order: `db`, then `accounts-api` (healthy on
`/health`), then the three Django APIs (they call it server to server), then the Next.js apps.

---

## 5. The test identities

`scripts/e2e_seed.py` creates (or resets) the people and companies the browser suites sign in
as, and prints the `E2E_*` block they read:

```bash
docker compose exec -T -e E2E_MINTY_PASSWORD=anything accounts-api \
  python scripts/e2e_seed.py --print
```

It only touches rows it created (`e2e@minty.test`, `e2e-terms@minty.test`, `E2E Petty Cash
Shop`, `E2E Subscription Shop`). Its docstring says exactly what each run does.

---

## 6. Editing code while it runs

The Next.js apps, `accounts-api`, `payment-request-api` and `subscription-api` bind-mount their
repo (the Python ones through `docker-compose.override.yml`), so **save a file and it reloads.**
Rebuild only when dependencies change: `docker compose up --build <service>`.

To run the backends under gunicorn, as deployed, skip the override:
`docker compose -f docker-compose.yml up`.

| I want to… | Command |
|------------|---------|
| Follow one service's logs | `docker compose logs -f accounts-api` |
| Shell inside a container | `docker compose exec accounts-api sh` |
| Stop everything | `docker compose down` |
| Stop **and wipe the database** | `docker compose down -v` |

---

## 7. How the wiring works

- **Browser-facing** URLs (every URL handed to a Next.js app, accounts-api's own
  `ACCOUNTS_API_URL`, subscription-api's `ACCOUNTS_API_PUBLIC_URL`, the `*_WEB_URL`
  redirect/CORS origins) are `http://localhost:<host port>`.
- **Server-to-server** URLs (`ACCOUNTS_API_URL` on the three other Django APIs,
  `SUBSCRIPTION_API_URL` on accounts-api, `DATABASE_URL`) use the compose service name
  (`http://accounts-api:8002`, `http://subscription-api:8000`, `db:5432`).

**Xero:** accounts-api is the only service that refreshes Xero tokens; the other APIs ask it at
`ACCOUNTS_API_URL/api/internal/xero/token`. Never give them their own Xero credentials. The
redirect URI registered in the Xero app must be `http://localhost:8002/callback`.

---

## 8. Troubleshooting

- **"Cannot connect to the Docker daemon"** → Docker Desktop isn't running.
- **"port is already allocated"** → change the matching `*_HOST_PORT` in `.env`.
- **accounts-api exits with "... is required and is not set"** → section 2.
- **Every page errors on a missing table** → the database is empty: section 3.
- **Module handoff bounces you to sign-in / 401s** → `SECRET_KEY` differs between backends;
  compare `docker compose exec accounts-api printenv SECRET_KEY` with another API's.
- **A frontend can't reach an API** → its URLs must be `localhost` URLs; the Next.js apps read
  them at dev-server start, so restart the service after a change.

---

## 9. The CI job (`stack-e2e.yml`)

This repo's `.github/workflows/e2e.yml` (Run workflow) calls the shared
`minty-oliveandvine/.github/.github/workflows/stack-e2e.yml@v1`, which checks the seven repos
out side by side, runs sections 3-5 above with per-run throwaway values (no production secret),
then one repo's Playwright specs (`minty-web` or `minty-payment-request-web`). Detail:
`docs/features/stack-e2e.md`.
