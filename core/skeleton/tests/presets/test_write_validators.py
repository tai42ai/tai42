"""The preset write-validator registry + the ``app.presets.register_write_validator``
facet."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from tai42_contract.presets import PresetBody

from tai42_skeleton.presets.write_validators import PresetWriteValidatorRegistry


async def _pass(body: PresetBody) -> Sequence[str]:
    return []


def test_register_get_round_trip() -> None:
    reg = PresetWriteValidatorRegistry()
    reg.register("weather", _pass)
    assert reg.get("weather") is _pass


def test_get_unknown_is_none() -> None:
    reg = PresetWriteValidatorRegistry()
    assert reg.get("nope") is None


def test_duplicate_base_tool_raises() -> None:
    reg = PresetWriteValidatorRegistry()
    reg.register("weather", _pass)
    with pytest.raises(ValueError, match="already registered"):
        reg.register("weather", _pass)


def test_reset_clears() -> None:
    reg = PresetWriteValidatorRegistry()
    reg.register("weather", _pass)
    reg.reset()
    assert reg.get("weather") is None


def test_facet_registers_and_resolves_through_app() -> None:
    from tai42_contract.app import tai42_app

    from tai42_skeleton.app.instance import build_app

    app = build_app()
    tai42_app.bind(app)
    app._write_validator_registry.reset()
    try:
        tai42_app.presets.register_write_validator("weather", _pass)
        assert app.presets.write_validator("weather") is _pass
        assert app.presets.write_validator("unregistered") is None
    finally:
        app._write_validator_registry.reset()
