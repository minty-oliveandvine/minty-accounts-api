"""The fall-through to Flask: a real local HTTP server plays Flask, the WSGI app is called directly."""

import gzip
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from wsgiref.util import setup_testing_defaults

import pytest
from django.core.wsgi import get_wsgi_application

from core.fallthrough import MAX_BODY, make_application


class FakeFlask(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *args):  # keep test output quiet
        pass

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        FakeFlask.seen.append({
            "method": self.command,
            "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": self.rfile.read(length) if length else b"",
        })

    def do_GET(self):
        self._record()
        if self.path.startswith("/redirect"):
            body = b"<p>Redirecting...</p>"
            self.send_response(302)
            self.send_header("Location", "/somewhere-else")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/cookies"):
            body = b"two cookies"
            self.send_response(200)
            self.send_header("Set-Cookie", "session=abc; HttpOnly; Path=/")
            self.send_header("Set-Cookie", "remember=xyz; Path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/gzip"):
            body = gzip.compress(b"compressed body")
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps({"path": self.path}).encode()
        self.send_response(404 if self.path.startswith("/missing") else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self._record()
        body = b"posted"
        self.send_response(201)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def flask_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeFlask)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(scope="module")
def django_app():
    # In a fixture, never a test body: get_wsgi_application() re-runs Django's logging setup,
    # which replaces the root logger's handlers - caplog's among them.
    return get_wsgi_application()


@pytest.fixture
def app(flask_url, django_app):
    FakeFlask.seen.clear()
    return make_application(django_app, flask_url)


def call(app, path, method="GET", body=b"", headers=None, query=""):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "REMOTE_ADDR": "10.0.0.7",
        "HTTP_HOST": "accounts-api.example.test",
        "wsgi.input": io.BytesIO(body),
    }
    if body:
        environ["CONTENT_LENGTH"] = str(len(body))
        environ["CONTENT_TYPE"] = "application/json"
    for name, value in (headers or {}).items():
        environ["HTTP_" + name.upper().replace("-", "_")] = value
    setup_testing_defaults(environ)
    captured = {}

    def start_response(status, response_headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = response_headers

    chunks = app(environ, start_response)
    captured["body"] = b"".join(chunks)
    return captured


def header_values(result, name):
    return [v for k, v in result["headers"] if k.lower() == name.lower()]


# db: Django's request_finished closes an unusable database connection, which on the Postgres pass
# counts as database access (health itself reads nothing).
def test_health_is_django_and_never_reaches_flask(app, db):
    result = call(app, "/health")
    assert result["status"].startswith("200")
    assert json.loads(result["body"]) == {"status": "ok", "service": "minty-accounts-api"}
    assert FakeFlask.seen == []


def test_any_other_path_goes_to_flask_with_its_query(app):
    result = call(app, "/api/me/entities", query="page=2&q=a%20b")
    assert result["status"].startswith("200")
    assert FakeFlask.seen[0]["path"] == "/api/me/entities?page=2&q=a%20b"
    assert json.loads(result["body"]) == {"path": "/api/me/entities?page=2&q=a%20b"}


def test_flasks_status_comes_back_unchanged(app):
    assert call(app, "/missing")["status"].startswith("404")


def test_a_redirect_is_returned_not_followed(app):
    result = call(app, "/redirect")
    assert result["status"].startswith("302")
    assert header_values(result, "Location") == ["/somewhere-else"]
    # Its body too: requests' Session reads a redirect's body itself even unfollowed, which
    # left the client waiting for bytes that never came (2026-11-05, Flask's /profile).
    assert result["body"] == b"<p>Redirecting...</p>"
    assert len(FakeFlask.seen) == 1


def test_both_set_cookie_headers_come_back(app):
    result = call(app, "/cookies")
    assert header_values(result, "Set-Cookie") == [
        "session=abc; HttpOnly; Path=/",
        "remember=xyz; Path=/",
    ]


def test_one_persons_cookie_is_never_sent_with_the_next_request(app):
    call(app, "/cookies")
    call(app, "/next-person")
    assert "cookie" not in FakeFlask.seen[1]["headers"]


def test_the_clients_cookie_and_authorization_are_forwarded(app):
    call(app, "/api/me/profile", headers={"Cookie": "session=mine", "Authorization": "Bearer t"})
    seen = FakeFlask.seen[0]["headers"]
    assert seen["cookie"] == "session=mine"
    assert seen["authorization"] == "Bearer t"


def test_a_compressed_body_passes_through_still_compressed(app):
    result = call(app, "/gzip", headers={"Accept-Encoding": "gzip"})
    assert header_values(result, "Content-Encoding") == ["gzip"]
    assert gzip.decompress(result["body"]) == b"compressed body"


def test_the_body_and_method_are_forwarded(app):
    result = call(app, "/auth/email/request-code", method="POST", body=b'{"email": "a@b.c"}')
    assert result["status"].startswith("201")
    assert FakeFlask.seen[0]["method"] == "POST"
    assert FakeFlask.seen[0]["body"] == b'{"email": "a@b.c"}'
    assert FakeFlask.seen[0]["headers"]["content-type"] == "application/json"


def test_forwarded_headers_keep_the_client_and_public_host(app):
    call(app, "/x", headers={"X-Forwarded-For": "203.0.113.5", "X-Forwarded-Proto": "https"})
    seen = FakeFlask.seen[0]["headers"]
    assert seen["x-forwarded-for"] == "203.0.113.5, 10.0.0.7"
    assert seen["x-forwarded-host"] == "accounts-api.example.test"
    assert seen["x-forwarded-proto"] == "https"
    assert seen["host"].startswith("127.0.0.1:")


def test_the_trace_id_is_kept_or_started(app):
    call(app, "/x", headers={"X-Request-ID": "0123abcd"})
    call(app, "/y", headers={"X-Request-ID": "forged] [user=admin"})
    assert FakeFlask.seen[0]["headers"]["x-request-id"] == "0123abcd"
    # Not lowercase hex: replaced by a fresh one (logctx.start_trace), never echoed into logs.
    assert FakeFlask.seen[1]["headers"]["x-request-id"] not in ("", "forged] [user=admin")


def test_hop_by_hop_headers_are_not_forwarded(app):
    call(app, "/x", headers={"Connection": "keep-alive, X-Secret", "TE": "trailers"})
    seen = FakeFlask.seen[0]["headers"]
    assert seen.get("connection") != "keep-alive, X-Secret"
    assert "te" not in seen


def test_too_large_a_body_is_refused_without_calling_flask(app):
    # The declared length is what is checked, so no 32 MiB body is needed.
    environ = {"REQUEST_METHOD": "POST", "PATH_INFO": "/upload",
               "CONTENT_LENGTH": str(MAX_BODY + 1), "wsgi.input": io.BytesIO(b"")}
    setup_testing_defaults(environ)
    captured = {}
    body = b"".join(app(environ, lambda s, h, e=None: captured.update(status=s)))
    assert captured["status"].startswith("413")
    assert json.loads(body) == {"error": "That upload is too large."}
    assert FakeFlask.seen == []


def test_flask_down_is_a_502_with_the_house_sentence(django_app, caplog):
    down = make_application(django_app, "http://127.0.0.1:9")
    result = call(down, "/api/me/entities")
    assert result["status"].startswith("502")
    assert json.loads(result["body"]) == {
        "error": "Something went wrong on our side. Please try again."
    }
    assert "Flask unreachable" in caplog.text


def test_an_upstream_that_is_not_http_is_refused_at_start():
    with pytest.raises(ValueError):
        make_application(lambda environ, start_response: [], "localhost:8000")
