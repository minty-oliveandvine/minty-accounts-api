"""The fall-through: every request Django does not route goes to Flask, unchanged.

Phase 1 of the Flask retirement (2026-11-05): this service takes Flask's public address and
serves nothing of its own yet but ``/health``. Each later phase moves a group of Flask's routes
into Django; until a path resolves in ``config/urls.py``, it lands here.

It runs at WSGI level, in front of Django (``config/wsgi.py``), not as a Django view, so:

- Flask's duplicate ``Set-Cookie`` headers pass through exactly (Django's response object holds
  one value per header name);
- Django's CSRF, CORS and session machinery never touch Flask's traffic.

What it forwards: the method, the raw path and query, the body, and every header except the
hop-by-hop ones. ``Host`` is the upstream's; the incoming ``X-Forwarded-For`` is kept and this
hop's peer appended (Flask's consent record reads the client from its FIRST value). What it
returns: Flask's status, headers and body exactly, streamed, never decompressed. It never
follows a redirect. The upstream is fixed (``FLASK_FALLBACK_URL``) and only the path is
appended, so it cannot be turned into an open proxy. An unreachable Flask is a 502 with the
house sentence, logged with the trace id - never a silent fallback.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any
from urllib.parse import quote

import urllib3
from django.urls import Resolver404, resolve
from minty_shared import logctx

logger = logging.getLogger("minty-accounts-api")

#: Connection-scoped headers (RFC 9110 7.6.1): never forwarded in either direction.
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
})
#: 5 s to reach Flask; 150 s for it to answer (Petty Cash's Xero publish runs through Flask's
#: internal routes, and petty-cash-api itself allows 130 s).
TIMEOUT = urllib3.Timeout(connect=5, read=150)
#: Larger than any upload a Minty route takes (a 10 MB receipt and its form).
MAX_BODY = 32 * 1024 * 1024
CHUNK = 64 * 1024

_SERVER_ERROR = b'{"error": "Something went wrong on our side. Please try again."}'
_TOO_LARGE = b'{"error": "That upload is too large."}'


def django_owns(path: str) -> bool:
    """Does ``config/urls.py`` route this path? Then Django answers it, not Flask."""
    try:
        resolve(path)
    except Resolver404:
        return False
    return True


def _target(environ: dict[str, Any]) -> str:
    """The path and query exactly as the client sent them.

    gunicorn gives ``RAW_URI``; elsewhere (runserver) it is rebuilt from ``PATH_INFO``, which
    WSGI hands over latin-1 decoded. An absolute-form target is never forwarded.
    """
    raw = environ.get("RAW_URI") or environ.get("REQUEST_URI") or ""
    if raw.startswith("/"):
        return raw
    path = quote(environ.get("PATH_INFO", "/").encode("latin-1"), safe="/:@!$&'()*+,;=-._~%")
    query = environ.get("QUERY_STRING", "")
    return f"{path}?{query}" if query else path


def _request_headers(environ: dict[str, Any], trace: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if key.startswith("HTTP_"):
            name = key[5:].replace("_", "-").title()
            if name.lower() not in HOP_BY_HOP and name.lower() != "host":
                headers[name] = value
    for key, name in (("CONTENT_TYPE", "Content-Type"), ("CONTENT_LENGTH", "Content-Length")):
        if environ.get(key):
            headers[name] = environ[key]
    peer = environ.get("REMOTE_ADDR", "")
    forwarded_for = headers.get("X-Forwarded-For")
    if peer:
        headers["X-Forwarded-For"] = f"{forwarded_for}, {peer}" if forwarded_for else peer
    headers.setdefault("X-Forwarded-Host", environ.get("HTTP_HOST", ""))
    headers.setdefault("X-Forwarded-Proto", environ.get("wsgi.url_scheme", "http"))
    headers.update(logctx.outgoing_headers())  # X-Request-ID = this request's trace
    headers[logctx.TRACE_HEADER] = trace
    return headers


class BodyTooLarge(Exception):
    pass


def _read_body(environ: dict[str, Any]) -> bytes:
    stream = environ["wsgi.input"]
    length = environ.get("CONTENT_LENGTH")
    if length:
        size = int(length)
        if size > MAX_BODY:
            raise BodyTooLarge(size)
        return stream.read(size) if size else b""
    if environ.get("wsgi.input_terminated"):  # chunked: gunicorn ends the stream for us
        body = stream.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            raise BodyTooLarge(len(body))
        return body
    return b""


def _stream(response: urllib3.BaseHTTPResponse) -> Iterator[bytes]:
    try:
        yield from response.stream(CHUNK, decode_content=False)
    finally:
        response.release_conn()


def make_application(
    django_app: Callable[..., Iterable[bytes]],
    upstream: str,
    pool: urllib3.PoolManager | None = None,
) -> Callable[..., Iterable[bytes]]:
    """The WSGI application: Django for what it routes, Flask (``upstream``) for the rest.

    urllib3, not requests: a requests ``Session`` reads a redirect's body itself even when told
    not to follow it, keeps every ``Set-Cookie`` in a jar it shares across requests, and reads
    proxies and ``.netrc`` from the environment. urllib3 does none of that.
    """
    if not upstream.startswith(("http://", "https://")):
        raise ValueError(f"FLASK_FALLBACK_URL must be an http(s) origin, got {upstream!r}")
    base = upstream.rstrip("/")
    # No retries: a POST must reach Flask once or not at all.
    http = pool or urllib3.PoolManager(retries=False, timeout=TIMEOUT)

    def application(environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        path = environ.get("PATH_INFO") or "/"
        if django_owns(path):
            return django_app(environ, start_response)

        trace = logctx.start_trace(environ.get("HTTP_X_REQUEST_ID"))
        method = environ.get("REQUEST_METHOD", "GET")
        started = time.monotonic()
        try:
            body = _read_body(environ)
        except BodyTooLarge as exc:
            logger.warning("fall-through: %s %s refused, body of %s bytes", method, path, exc)
            start_response("413 Content Too Large",
                           [("Content-Type", "application/json"), (logctx.TRACE_HEADER, trace)])
            return [_TOO_LARGE]
        try:
            response = http.request(
                method, base + _target(environ), headers=_request_headers(environ, trace),
                body=body or None, redirect=False, preload_content=False, decode_content=False,
            )
        except urllib3.exceptions.HTTPError as exc:
            logger.error("fall-through: %s %s - Flask unreachable: %s", method, path, exc)
            start_response("502 Bad Gateway",
                           [("Content-Type", "application/json"), (logctx.TRACE_HEADER, trace)])
            return [_SERVER_ERROR]

        headers = [(k, v) for k, v in response.headers.items() if k.lower() not in HOP_BY_HOP]
        start_response(f"{response.status} {response.reason or ''}".rstrip(), headers)
        logger.info("fall-through: %s %s -> Flask %s in %d ms", method, path, response.status,
                    (time.monotonic() - started) * 1000)
        return _stream(response)

    return application
