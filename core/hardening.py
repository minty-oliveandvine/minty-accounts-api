"""Every Django answer's security headers - Flask's ``pettycash/core/http_hardening.py``
(Phase 6 of the Flask retirement, 2026-11-06). The fall-through passes Flask's own answers on
unchanged, so these apply to the paths Django serves.

- ``Referrer-Policy: same-origin``, ``X-Content-Type-Options: nosniff``,
  ``X-Frame-Options: SAMEORIGIN`` (set only when the view set none), and HSTS outside development.
- ``Cache-Control: no-store`` on an answer whose path holds a secret (an invitation's accept link).
- ``loggable_path``: the same path with the secret segment shown as ``[redacted]``, for the access
  line (``MINTY_LOGGABLE_PATH``). The query string is never logged at all.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from django.conf import settings
from django.http import HttpRequest, HttpResponse

#: Paths whose last segment is a secret.
_SECRET_PATHS = (re.compile(r"^(/invitation/accept/)[^/]+"),)


def loggable_path(path: str) -> str:
    for pattern in _SECRET_PATHS:
        path = pattern.sub(r"\1[redacted]", path)
    return path


def _holds_secret(path: str) -> bool:
    return any(pattern.match(path) for pattern in _SECRET_PATHS)


class HardeningMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]):
        self.get_response = get_response
        self.hsts = not settings.DEVELOPMENT

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if self.hsts:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if _holds_secret(request.path):
            response["Cache-Control"] = "no-store"
        return response
