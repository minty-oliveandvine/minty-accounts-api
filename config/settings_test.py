"""Tests: the two required values set to placeholders, so no .env is needed (CI has none)."""

import os

os.environ.setdefault("SECRET_KEY", "test-only-not-a-secret")
os.environ.setdefault("FLASK_FALLBACK_URL", "http://127.0.0.1:9")

from config.settings import *  # noqa: E402,F403
