"""Provider registry: register_connector / lookup + reset hook."""

from __future__ import annotations

import re

import pytest
from tai42_contract.connectors.errors import OperatorMisconfiguredError

from tai42_skeleton.connectors.providers import registry
from tai42_skeleton.connectors.providers.registry import (
    SEED_CATEGORY_IDS,
    get_provider,
    list_providers,
    register_connector,
)
from tai42_skeleton.sql.schema import load_ddl

from .conftest import make_oauth_descriptor


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Snapshot and restore the module-global registry around each test."""
    reg = dict(registry._REGISTRY)
    registry._REGISTRY.clear()
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(reg)


def test_register_and_get():
    desc = make_oauth_descriptor(provider_id="acme")
    register_connector(desc)
    assert get_provider("acme") is desc
    assert desc in list_providers()


def test_register_duplicate_raises():
    register_connector(make_oauth_descriptor(provider_id="acme"))
    with pytest.raises(ValueError, match="already registered"):
        register_connector(make_oauth_descriptor(provider_id="acme"))


def test_register_rejects_non_seed_category():
    desc = make_oauth_descriptor(provider_id="acme")
    object.__setattr__(desc, "category", "not-a-seed-category")
    with pytest.raises(ValueError, match=r"not.*a seed category"):
        register_connector(desc)


def test_get_provider_unknown_raises_keyerror():
    with pytest.raises(KeyError, match="Unknown"):
        get_provider("nope")


def test_reset_registry_clears():
    register_connector(make_oauth_descriptor(provider_id="acme"))
    registry.reset_registry()
    with pytest.raises(KeyError):
        get_provider("acme")


def test_operator_misconfigured_error_carries_fields():
    err = OperatorMisconfiguredError(env_var="X_ENV", provider_id="acme")
    assert "X_ENV" in str(err)


def _seed_category_ids_from_ddl() -> set[str]:
    """The ids of the ``connector_category`` seed rows in the init SQL."""
    ddl = load_ddl()
    match = re.search(
        r"INSERT INTO connector_category[^;]*?VALUES(?P<rows>.*?);",
        ddl,
        re.DOTALL,
    )
    assert match is not None, "connector_category seed INSERT not found in init SQL"
    return set(re.findall(r"\(\s*'([^']+)'", match.group("rows")))


def test_seed_category_ids_match_init_sql():
    """``SEED_CATEGORY_IDS`` (the register-time category allow-list) must stay in
    lockstep with the ``connector_category`` rows the init SQL seeds — a provider's
    category is a foreign key into that table, so any drift would let a descriptor
    register against a category the database rejects (or vice versa)."""
    assert set(SEED_CATEGORY_IDS) == _seed_category_ids_from_ddl()
