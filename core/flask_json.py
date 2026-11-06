"""JSON in Flask's ``jsonify`` bytes (sorted keys, compact, a trailing newline): every route moved
here from Flask answers its callers exactly as Flask did."""

from __future__ import annotations

import json

from django.http import HttpResponse


def flask_json(payload, status: int = 200) -> HttpResponse:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    return HttpResponse(body, status=status, content_type="application/json")
