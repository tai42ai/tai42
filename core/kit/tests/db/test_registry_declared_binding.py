"""The set-vs-default component-binding accessor.

:func:`component_binding_declared` exposes the distinction the plain
:func:`component_binding` cannot: it returns the LITERAL value of
``TAI_DB_BINDING_<SLUG>`` only when the var is explicitly set (including the
literal ``"default"``), and ``None`` when unset — where ``component_binding``
folds unset into the ``"default"`` fallback. Env is read fresh per call, so
tests set it with monkeypatch and assert on the resolved value without touching
a real Postgres.
"""

import os

import pytest

from tai42_kit.db import component_binding, component_binding_declared

# Prefixes any test may set — stripped before each test so ambient env cannot
# colour a resolution.
_TEST_ENV_PREFIXES = ("TAI_DB_BINDING_",)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(_TEST_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)


class TestComponentBindingDeclared:
    def test_none_when_unset(self):
        # Unset resolves to None — the observable "operator declared nothing"
        # state, where component_binding would fold to "default".
        assert component_binding_declared("skeleton") is None
        assert component_binding("skeleton") == "default"

    def test_returns_the_set_name(self, monkeypatch):
        monkeypatch.setenv("TAI_DB_BINDING_SKELETON", "analytics")
        assert component_binding_declared("skeleton") == "analytics"

    def test_returns_literal_default_when_explicitly_set(self, monkeypatch):
        # The set-vs-default distinction the plain accessor cannot make: an
        # operator who explicitly binds to "default" is observably different from
        # one who set nothing, and the literal value is returned verbatim.
        monkeypatch.setenv("TAI_DB_BINDING_SKELETON", "default")
        assert component_binding_declared("skeleton") == "default"
        # ...while an UNSET var also yields "default" from component_binding, so
        # only the declared accessor tells the two apart.
        assert component_binding("skeleton") == "default"

    def test_slug_derivation_matches_component_binding(self, monkeypatch):
        # A distribution name needing normalization (hyphens) slugs to the same
        # env var both accessors read — asserted by setting one var and observing
        # both resolve it.
        monkeypatch.setenv("TAI_DB_BINDING_TAI42_ACCOUNTS_POSTGRES", "accounts")
        assert component_binding_declared("tai42-accounts-postgres") == "accounts"
        assert component_binding("tai42-accounts-postgres") == "accounts"

    def test_fresh_read_per_call(self, monkeypatch):
        assert component_binding_declared("skeleton") is None
        monkeypatch.setenv("TAI_DB_BINDING_SKELETON", "warehouse")
        assert component_binding_declared("skeleton") == "warehouse"
