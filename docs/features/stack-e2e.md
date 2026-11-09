# The whole stack and its e2e job

`docker/stack/` stands up seven repos and one Postgres; `scripts/e2e_seed.py` creates the people
and companies the browser suites sign in as; `.github/workflows/e2e.yml` (manual, "Run workflow")
calls the shared `stack-e2e.yml@v1` in the public `minty-oliveandvine/.github`, which does both
and then runs one repo's Playwright specs. All three moved here from Minty on 2026-11-06 (the
Flask retirement, Phase 7): this service issues the tokens now, so Flask is not in the stack and
Minty is never checked out. How to run the stack by hand: `docker/stack/README.md`.

## What the job does

1. Refuses a suite it cannot seed: `minty-web` and `minty-payment-request-web` only. `Minty` has
   no Playwright suite (Flask's pages went 2026-10-20); `minty-onboarding-web` needs a disposable
   `onboarding` company the seed does not create yet (`E2E_ENTITY_ID`).
2. Checks out this repo (the caller's commit; any other caller gets `ref`) and the other six
   (`minty-subscription-api`, `-payment-request-api`, `-onboarding-api`, `minty-web`,
   `-payment-request-web`, `-onboarding-web`) at `ref`, side by side as `docker/stack` expects.
   All seven are public, so no token.
3. Writes the stack's `.env` with throwaway values: a generated `SECRET_KEY` (only has to be the
   same for every service), Postgres password and `E2E_MINTY_PASSWORD` (masked), and
   placeholders for what this service refuses to start without (`XERO_CLIENT_ID`/`_SECRET`,
   `INVITE_FROM`). `APP_ENV=development`, so with no `SMTP_URL` mail is only printed.
4. `docker compose -f docker-compose.yml up -d --wait db` (`-f` on every call skips the dev
   override, so the backends run under gunicorn).
5. **The schema, from minty-db**, in this repo's image (which pins it):
   `python -m minty_db migrate` runs the packaged `01_schema_rebased.sql`, renames
   `pettycash_test` to `pettycashv3` and records `0001`; `python manage.py migrate sessions` adds
   `django_session`. The job checks both exist.
6. **The catalogue**, minty-db's packaged `seed_catalogue.sql` (3 plans, the policy, HKD) piped
   into `psql`, then checked: without it no trial can start (found 2026-10-07, when a trial start
   sat until Playwright's 45 s timeout).
7. `up -d --build --wait` for the rest (every service has a healthcheck; the three other APIs
   wait for this one).
8. `scripts/e2e_seed.py --print` inside this container; its `E2E_*` block (ids and emails, never
   the password) goes into the job's environment.
9. The suite's `npm ci`, Chromium, then `npm run test:e2e [spec]` with `E2E_BASE_URL` (3000 or
   3020), `E2E_ACCOUNTS_API_URL` (and the old name `E2E_PETTY_CASH_URL`, until both suites read
   the new one) = `http://localhost:8002`, `E2E_SUBSCRIPTION_API_URL` 8000,
   `E2E_PAYMENT_REQUEST_API_URL` 8020, `E2E_ONBOARDING_API_URL` 8030.
10. On failure: the containers' last 400 log lines and the Playwright report (7 days). Always:
    `down -v`.

## The seed

`scripts/e2e_seed.py` is Flask's seed rewritten on minty-db's models, with the same effect: two
users (`e2e@minty.test` with its Terms consent removed, `e2e-terms@minty.test` with the live
Terms recorded, source `gate`), `E2E Petty Cash Shop` (both modules on, local Xero placeholders
unless really connected, reports wiped, sales methods Visa/Alipay/Foodpanda) and
`E2E Subscription Shop` (both modules off, every subscription row deleted). One transaction;
idempotent; only its own rows. `E2E_MINTY_PASSWORD` is required (exit 2 without it). Tested by
`tests/test_e2e_seed.py` (SQLite and Postgres).

## Not done

- The job has never run in this form; no local Docker daemon here (Docker Desktop needs WSL2).
  The database half (steps 5, 6, 8) was rehearsed against a fresh local Postgres on 2026-11-06.
- `E2E_JWT_SECRET` is not passed to the specs (it never was), so the specs that mint their own
  module token skip in this job.
- Petty Cash (minty-petty-cash-api / -web) is not in the stack.
