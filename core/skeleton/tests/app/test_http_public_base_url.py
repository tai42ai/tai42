"""``tai42_app.http.public_base_url()``: a plugin reads the deployment's public address through the handle.

The reader is a plugin module that knows nothing of the skeleton's settings: it sees the
normalised ``TAI_PUBLIC_BASE_URL``, ``None`` while it is unset, and the new value once the
settings are reloaded from an applied stored env.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from tests._helpers import recapture_deployment_env, set_public_base_url
from tests.app._fixtures import public_address_reader

from tai42_skeleton.app import applied_env
from tai42_skeleton.config.deployment_env import DeploymentOwnership


@pytest.fixture
def _process_env_restored() -> Iterator[None]:
    """An applied stored env writes real process env: restore it and the applied-key tracker after the test."""
    snapshot = dict(os.environ)
    loaded_before = set(applied_env._loaded_env_keys)
    yield
    os.environ.clear()
    os.environ.update(snapshot)
    applied_env._loaded_env_keys = loaded_before


def test_a_plugin_reads_the_normalised_public_address(monkeypatch: pytest.MonkeyPatch) -> None:
    set_public_base_url(monkeypatch, "HTTPS://Edge.Example.com:443/base/")
    assert public_address_reader.callback_url() == "https://edge.example.com/base/hooks/acme"


def test_a_plugin_reads_none_while_the_public_address_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    set_public_base_url(monkeypatch, None)
    assert public_address_reader.callback_url() is None


@pytest.mark.usefixtures("_process_env_restored")
def test_a_plugin_reads_the_new_public_address_after_a_settings_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    set_public_base_url(monkeypatch, "https://first.example.com")
    recapture_deployment_env()
    assert public_address_reader.callback_url() == "https://first.example.com/hooks/acme"

    # The deployment env does not set it, so the reload's applied stored env does.
    monkeypatch.delenv("TAI_PUBLIC_BASE_URL")
    recapture_deployment_env()
    applied_env.apply_env_and_reset_settings(
        {"TAI_PUBLIC_BASE_URL": "https://second.example.com/"}, DeploymentOwnership()
    )

    assert public_address_reader.callback_url() == "https://second.example.com/hooks/acme"
