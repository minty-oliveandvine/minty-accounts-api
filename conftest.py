"""Repo-root pytest hooks: minty-db's two plugins.

``minty_db.testing.pg``: with MINTY_TEST_PG_URI set, the test database is built from minty-db's
packaged ``01_schema_rebased.sql``, so the models run against the real schema; unset, the SQLite
path in ``config/settings_test.py`` applies.

``minty_db.testing.uuid_guard``: ids are ``uuid.UUID`` while token claims and JSON carry text;
under test, this service's code comparing a UUID with uuid text raises instead of quietly not
matching.
"""

pytest_plugins = ["minty_db.testing.pg", "minty_db.testing.uuid_guard"]


import pytest  # noqa: E402
import requests  # noqa: E402


@pytest.fixture(autouse=True)
def _no_subscription_api(monkeypatch):
    """No test reaches a real minty-subscription-api; a test that needs answers patches
    ``core.subscription_client.requests.request`` itself."""

    def blocked(*args, **kwargs):
        raise requests.ConnectionError("real HTTP to minty-subscription-api blocked in tests")

    monkeypatch.setattr("core.subscription_client.requests.request", blocked)
