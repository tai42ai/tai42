"""The shared preset-authoring chokepoint gates (ruling 14 + input-schema authoring).

The registration-tier fence and the input-schema authoring rejection land at the SAME
sites the write validator does (create/save/rollback/rename), so they inherit the
identical door coverage. These pin the two gate helpers every authoring door flows
through.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from tai42_contract.app import tai42_app
from tai42_contract.presets import PresetBody, PresetInputSchemaSupport

import tai42_skeleton.operations.presets as presets_ops
from tai42_skeleton.app.instance import build_app
from tai42_skeleton.operations.errors import ForbiddenError
from tai42_skeleton.operations.presets import _enforce_registration_tier, _input_schema_authoring_error


@dataclass
class _Caller:
    is_admin: bool


@pytest.fixture
def bound_app():
    app = build_app()
    tai42_app.bind(app)
    return app


def _body(base_tool: str, *, input_schema: dict | None = None) -> PresetBody:
    return PresetBody(base_tool=base_tool, description="d", fixed_kwargs={}, extensions=[], input_schema=input_schema)


async def test_tier_gate_admits_admin_and_refuses_non_admin(bound_app, monkeypatch: pytest.MonkeyPatch) -> None:
    bound_app.presets.register_registration_tier("fenced_base", "fenced")

    async def _admin() -> _Caller:
        return _Caller(is_admin=True)

    async def _non_admin() -> _Caller:
        return _Caller(is_admin=False)

    monkeypatch.setattr(presets_ops, "resolve_caller", _admin)
    await _enforce_registration_tier("fenced_base")  # admin: no raise

    monkeypatch.setattr(presets_ops, "resolve_caller", _non_admin)
    with pytest.raises(ForbiddenError):
        await _enforce_registration_tier("fenced_base")


async def test_tier_gate_is_a_noop_for_an_undeclared_base(bound_app, monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail() -> _Caller:
        raise AssertionError("resolve_caller must not be consulted for an undeclared base tool")

    monkeypatch.setattr(presets_ops, "resolve_caller", _fail)
    # No tier declared → the presets' own default write action, no extra gate.
    await _enforce_registration_tier("plain_base")


def test_input_schema_authoring_rejected_without_support(bound_app) -> None:
    # ``input_schema`` set over a base tool with no registered support → loud message.
    error = _input_schema_authoring_error(_body("no_support_base", input_schema={"type": "object"}))
    assert error is not None
    assert "does not accept a preset input_schema" in error


def test_input_schema_accepted_with_support(bound_app) -> None:
    bound_app.presets.register_input_schema_support("supported_base", PresetInputSchemaSupport(payload_arg="input"))
    assert _input_schema_authoring_error(_body("supported_base", input_schema={"type": "object"})) is None


def test_no_input_schema_is_never_an_error(bound_app) -> None:
    assert _input_schema_authoring_error(_body("no_support_base", input_schema=None)) is None
