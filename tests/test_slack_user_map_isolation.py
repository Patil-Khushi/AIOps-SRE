"""Regression for #174 — the Slack user-map env override must not bleed.

``demo/ui/server`` runs ``load_dotenv()`` at import, so once any test imports
it a developer's real ``AIOPS_SLACK_USER_MAP_JSON`` sits in the process-wide
environment for the rest of the session. ``load_slack_user_map`` re-reads that
env fresh on every call and merges it on top of whatever file map a test wrote
(see ``aiops/tools/chatops/adapters/_slack_user_map.py``), so handles resolve
to real member IDs instead of the test fixtures — green in isolation, red in
the full ``uv run pytest``.

The autouse ``_hermetic_slack_user_map_env`` fixture in ``tests/conftest.py``
clears that var per-test. These tests pin that guarantee so it can't be
silently deleted: the module-scoped ``_leak_slack_user_map_env`` fixture below
mimics the ``load_dotenv`` leak, and because it is broader-scoped than the
function-scoped hermetic fixture, pytest sets it up *first* — exactly the
ordering the real bleed has (import-time leak, per-test consumer). If the
conftest fixture is removed, both assertions below go red.
"""

from __future__ import annotations

import os

import pytest

from aiops.tools.chatops.adapters._slack_user_map import load_slack_user_map

# A real-looking override that would win over any test's file map if it bled
# through — including the keys both Slack adapter suites use.
_HOSTILE_ENV = '{"chinmay": "U_REAL_LEAK_0", "chinmay-kotkar": "U_REAL_LEAK_1"}'


@pytest.fixture(scope="module", autouse=True)
def _leak_slack_user_map_env():
    """Simulate ``load_dotenv`` leaking a real user-map into the process env.

    Module-scoped on purpose: higher-scoped autouse fixtures are instantiated
    before function-scoped ones, so this leak is already in place when the
    per-test hermetic conftest fixture runs — the same ordering the real bug
    has. Restored on teardown so it doesn't leak into other modules.
    """
    saved = os.environ.get("AIOPS_SLACK_USER_MAP_JSON")
    os.environ["AIOPS_SLACK_USER_MAP_JSON"] = _HOSTILE_ENV
    yield
    if saved is None:
        os.environ.pop("AIOPS_SLACK_USER_MAP_JSON", None)
    else:
        os.environ["AIOPS_SLACK_USER_MAP_JSON"] = saved


def test_hermetic_fixture_clears_leaked_env() -> None:
    """The module fixture leaked a hostile value; the autouse conftest fixture
    must have cleared it for this test. Red if that fixture is removed."""
    assert os.environ.get("AIOPS_SLACK_USER_MAP_JSON") is None


def test_leaked_env_does_not_override_file_user_map(tmp_path) -> None:
    """End-to-end: with the env cleared, the file map wins. A leaked env would
    merge on top and resolve ``chinmay`` to the real-looking id instead."""
    user_map_file = tmp_path / "slack_users.json"
    user_map_file.write_text('{"chinmay": "UPLACEHOLDER1"}', encoding="utf-8")

    resolved = load_slack_user_map(user_map_file)

    assert resolved["chinmay"] == "UPLACEHOLDER1"
