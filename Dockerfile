FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# No migrate at start: minty-db's schema and django_session are made by hand (docs/features).
# Threads, not only processes: a request waiting on Xero (a sync, up to a few minutes) must not
# hold the whole worker; the 180 s timeout covers the longest Xero call. Shell form so a
# host-injected PORT is honoured; exec keeps gunicorn as the signalled process.
# --access-logfile /dev/null: gunicorn's access line prints the whole URL, and these URLs carry
# secrets (/enter?token=, /callback?code=, /auth/email/handoff?h=, invitation links). The command
# line wins over GUNICORN_CMD_ARGS, so no host setting can turn it back on (Render did that to
# Flask, 2026-10-20). The request line (minty_shared's middleware) logs paths only.
CMD exec gunicorn -w 2 --threads 8 --timeout 180 --access-logfile /dev/null -b "0.0.0.0:${PORT:-8000}" config.wsgi:application
