"""The preset document's save rules: the name law, the schema bodies, and the binding's two seams.

Each document rule raises ``PresetDefinitionError`` with the text a write door answers; the
binding's own errors propagate as the binding validation raises them.
"""

from __future__ import annotations

from typing import Any

import pytest
from tai42_contract.presets import PresetBody
from tai42_contract.states import StateBinding
from tai42_contract.states.errors import StateNotFoundError
from tai42_kit.utils.render import SchemaBodyError

from tai42_skeleton.presets import definition
from tai42_skeleton.presets.definition import PresetDefinitionError, check_preset_body, output_schema_body_error
from tai42_skeleton.tools import state_binding as state_binding_module

_BINDING = StateBinding.model_validate({"states": [{"state": "status", "subject_expr": {"content": ".x"}}]})


def _body(**over: Any) -> PresetBody:
    return PresetBody.model_validate({"base_tool": "echo", "description": "d", **over})


def _patch_seams(monkeypatch: pytest.MonkeyPatch, error: Exception | None = None) -> dict[str, list]:
    calls: dict[str, list] = {"attach": [], "validate": []}

    async def _attach(app, binding) -> None:
        calls["attach"].append(binding)
        if error is not None:
            raise error

    async def _validate(app, binding) -> None:
        calls["validate"].append(binding)
        if error is not None:
            raise error

    monkeypatch.setattr(state_binding_module, "validate_and_attach_binding", _attach)
    monkeypatch.setattr(state_binding_module, "validate_binding", _validate)
    return calls


async def test_a_name_outside_the_tool_name_alphabet_is_refused() -> None:
    with pytest.raises(PresetDefinitionError, match=r"^invalid preset name 'bad/name': must match"):
        await check_preset_body(_body(), name="bad/name", attach=True)


async def test_a_non_object_output_schema_is_refused_with_the_door_text() -> None:
    with pytest.raises(PresetDefinitionError) as caught:
        await check_preset_body(_body(output_schema={"type": "string"}), name="p", attach=True)
    assert str(caught.value) == 'output_schema must be an object schema ("type": "object")'


async def test_an_output_schema_that_fails_the_meta_schema_is_refused() -> None:
    error = await output_schema_body_error({"type": 5}, [])
    assert error is not None
    assert error.startswith("output_schema is not a valid JSON Schema: ")


async def test_an_output_schema_declared_twice_is_refused() -> None:
    error = await output_schema_body_error({"type": "object"}, [["output_schema"]])
    assert error is not None
    assert error.startswith("output_schema field conflicts with an explicit 'output_schema' extension entry")


async def test_an_unresolvable_schema_body_is_refused_with_its_resolution_text(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _unresolvable(field: str, value: object, **kwargs: object) -> None:
        if value is not None:
            raise SchemaBodyError(f"{field}: its stored id 'gone' could not be rendered")

    monkeypatch.setattr(definition, "resolve_schema_body", _unresolvable)
    assert await output_schema_body_error({"type": "object"}, []) == (
        "output_schema: its stored id 'gone' could not be rendered"
    )
    with pytest.raises(PresetDefinitionError, match=r"^preset 'p' input_schema: its stored id 'gone'"):
        await check_preset_body(_body(input_schema={"type": "object"}), name="p", attach=True)


async def test_a_binding_attaches_on_a_write_and_is_only_validated_on_a_dry_run(monkeypatch) -> None:
    calls = _patch_seams(monkeypatch)
    await check_preset_body(_body(state_binding=_BINDING.model_dump()), name="p", attach=True)
    await check_preset_body(_body(state_binding=_BINDING.model_dump()), name="p", attach=False)
    assert calls == {"attach": [_BINDING], "validate": [_BINDING]}


async def test_a_binding_error_propagates_unwrapped(monkeypatch) -> None:
    error = StateNotFoundError("state 'status' is not declared")
    _patch_seams(monkeypatch, error)
    with pytest.raises(StateNotFoundError) as caught:
        await check_preset_body(_body(state_binding=_BINDING.model_dump()), name="p", attach=True)
    assert caught.value is error


async def test_a_body_with_no_binding_reaches_neither_seam(monkeypatch) -> None:
    calls = _patch_seams(monkeypatch)
    await check_preset_body(_body(), name="p", attach=True)
    assert calls == {"attach": [], "validate": []}
