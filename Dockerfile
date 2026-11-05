FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# No database and no migrate: Phase 1 serves /health and falls through to Flask for the rest.
# Threads, not only processes: a request waiting on Flask (a Xero call, up to 150 s) must not
# hold the whole worker. Timeout above the fall-through's 150 s read timeout. Shell form so a
# host-injected PORT is honoured; exec keeps gunicorn as the signalled process.
CMD exec gunicorn -w 2 --threads 8 --timeout 180 -b "0.0.0.0:${PORT:-8000}" config.wsgi:application
