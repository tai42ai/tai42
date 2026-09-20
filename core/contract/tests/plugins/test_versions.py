"""Tests for PEP 440 version + specifier-set validation on ``PluginSpec``."""

from __future__ import annotations

from typing import Any

import pytest


def _spec_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "spec_version": 1,
        "namespace": "tai42",
        "name": "toolbox",
        "display_name": "TAI Toolbox",
        "package": "tai42-toolbox",
        "version": "0.1.0",
        "description": "Generic tools and tool extensions.",
        "icon": "assets/toolbox.svg",
        "license": "Apache-2.0",
        "repository": "https://github.com/tai42ai/tai42/tree/main/plugins/toolbox",
        "contract": ">=0.1,<0.2",
        "categories": ["utilities"],
        "tags": ["uuid", "http"],
        "permissions": {"network": True},
        "provides": [
            {
                "kind": "tool",
                "name": "generate_uuid",
                "module": "tai42_toolbox.tools.generate_uuid",
                "description": "Generate a random UUID.",
                "tags": ["uuid"],
            }
        ],
    }
    base.update(overrides)
    return base


def test_version_must_be_pep440():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    assert PluginSpec(**_spec_kwargs(version="1.0.0rc1")).version == "1.0.0rc1"
    with pytest.raises(ValidationError, match="PEP 440"):
        PluginSpec(**_spec_kwargs(version="not-a-version"))


@pytest.mark.parametrize(
    "value",
    [">=0.1,<0.2", "==0.1.*", "~=1.2", "===0.1.0+anything", ">=0.1", "!=1.0.*"],
)
def test_contract_range_accepts_common_forms(value: str):
    from tai42_contract.plugins import PluginSpec

    assert PluginSpec(**_spec_kwargs(contract=value)).contract == value


@pytest.mark.parametrize(
    "value",
    [
        "0.1",  # no operator
        ">=",  # no version
        ">=0.1,,<0.2",  # empty clause
        "<0.1.*",  # wildcard outside ==/!=
        "==0.1.0rc1.*",  # wildcard stem is not a plain release
        "~=1",  # single release segment
        ">1.0+local",  # local version on an ordered comparison
        ">=zzz",  # unparseable version
        "",  # empty set
    ],
)
def test_contract_range_rejects_malformed(value: str):
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    with pytest.raises(ValidationError):
        PluginSpec(**_spec_kwargs(contract=value))


def test_contract_rejects_surrounding_and_embedded_whitespace():
    from pydantic import ValidationError

    from tai42_contract.plugins import PluginSpec

    for bad in (">=0.1\n", " >=0.1", ">=0.1,\n<2"):
        with pytest.raises(ValidationError, match="whitespace"):
            PluginSpec(**_spec_kwargs(contract=bad))


@pytest.mark.parametrize("value", [">=0.1, <0.2", ">=0.1,<0.2"])
def test_contract_accepts_conventional_inner_whitespace(value: str):
    from tai42_contract.plugins import PluginSpec

    # Conventional whitespace around a comma (PEP 440's ">=0.1, <0.2") stays
    # allowed and is stored verbatim.
    assert PluginSpec(**_spec_kwargs(contract=value)).contract == value
