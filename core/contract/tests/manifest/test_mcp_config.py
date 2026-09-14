"""Validator tests for the manifest ``MCPConfig`` — value normalization and the
exactly-one-transport rule."""

from __future__ import annotations

from typing import cast

import pytest

from tai42_contract.manifest import MCPConfig

# === manifest — MCPConfig ===================================================


# -- normalize_dict_values (before) ------------------------------------------


def test_mcpconfig_dict_none_becomes_empty():
    cfg = MCPConfig(command="run", env=cast("dict[str, str]", None))
    assert cfg.env == {}


def test_mcpconfig_dict_stringifies_values():
    # Intentionally passes non-str values; the validator stringifies them.
    cfg = MCPConfig(command="run", env=cast("dict[str, str]", {"A": 1, "B": 2}))
    assert cfg.env == {"A": "1", "B": "2"}


def test_mcpconfig_dict_none_value_raises_naming_key():
    # A None value is malformed: fail loud naming the key, never silently drop it.
    with pytest.raises(ValueError, match="'B' must not be None"):
        MCPConfig(command="run", env=cast("dict[str, str]", {"A": 1, "B": None}))


def test_mcpconfig_dict_non_dict_raises():
    with pytest.raises(ValueError, match="Expected a dictionary"):
        # Intentionally passes a non-dict to exercise the validator's type guard.
        MCPConfig(command="run", env=cast("dict[str, str]", "not-a-dict"))


# -- empty-transport normalization + args None-normalization -----------------


def test_mcpconfig_empty_transport_normalizes_to_none():
    # An empty transport string is "not set": normalized to None so the gate and
    # the is_* predicates agree.
    cfg = MCPConfig(uds="")
    assert cfg.uds is None
    assert MCPConfig(url="").url is None
    assert MCPConfig(command="").command is None


def test_mcpconfig_args_none_normalizes_to_empty_list():
    cfg = MCPConfig(command="run", args=cast("list[str]", None))
    assert cfg.args == []


# -- _exactly_one_transport --------------------------------------------------


def test_mcpconfig_zero_transport_valid():
    assert MCPConfig().url is None


def test_mcpconfig_single_transport_valid():
    assert MCPConfig(url="https://x").url == "https://x"


def test_mcpconfig_two_transports_raise():
    with pytest.raises(ValueError, match="exactly one of url/uds/command"):
        MCPConfig(url="https://x", command="run")


def test_mcpconfig_url_with_args_raises():
    with pytest.raises(ValueError, match="``args`` is launcher-only"):
        MCPConfig(url="https://x", args=["a"])


def test_mcpconfig_url_with_env_raises():
    with pytest.raises(ValueError, match="``env`` is launcher-only"):
        MCPConfig(url="https://x", env={"A": "1"})


def test_mcpconfig_command_with_headers_raises():
    with pytest.raises(ValueError, match="``headers`` is HTTP-only"):
        MCPConfig(command="run", headers={"H": "v"})


def test_mcpconfig_command_with_args_env_valid():
    cfg = MCPConfig(command="run", args=["a"], env={"A": "1"})
    assert cfg.command == "run"


def test_mcpconfig_url_with_headers_valid():
    cfg = MCPConfig(url="https://x", headers={"H": "v"})
    assert cfg.headers == {"H": "v"}
