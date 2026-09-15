"""The dry-run verdict door: report whether a preset draft would be accepted.

Runs the SAME pre-store checks the corresponding write route would.
"""

from __future__ import annotations

import sys
from typing import Any

from tai42_contract.manifest import ExtensionElement
from tai42_contract.presets import PresetBody
from tai42_contract.presets.errors import PresetNotFoundError
from tai42_contract.states.binding import StateBinding
from tai42_contract.template import TemplatedText
from tai42_kit.db import component_store_configured

from tai42_skeleton.app import instance
from tai42_skeleton.db import SKELETON_COMPONENT, not_configured_message
from tai42_skeleton.operations import BadRequestError, NotSupportedError, operation
from tai42_skeleton.operations.presets.authoring import (
    _combo_registry_error,
    _dry_run_bind_error,
    _input_schema_authoring_error,
    _output_schema_error,
    _state_binding_error,
    _write_validator_error,
)
from tai42_skeleton.operations.presets.create import _NOT_CONFIGURED_CODE, _NOT_CONFIGURED_NOUN
from tai42_skeleton.operations.presets.models import PresetValidate
from tai42_skeleton.operations.presets.readers import (
    read_create_extensions,
    read_edit_extensions,
    read_input_schema,
    read_output_schema,
    read_state_binding,
)
from tai42_skeleton.operations.presets.views import _verdict
from tai42_skeleton.operations.response_models_group_a import PresetValidateVerdict
from tai42_skeleton.presets.manager import is_valid_preset_name

# This submodule's own package generation, captured at import time (a reload builds a
# fresh package + submodules together, so each generation's submodule reads its OWN
# package). ``_agent_tool_names`` / ``_agent_authoring_error`` are read through it at
# call time, so a test's ``monkeypatch.setattr`` on the package attribute is honored.
_pkg = sys.modules["tai42_skeleton.operations.presets"]


async def _validate_create(
    name: str,
    base_tool: str,
    description: str | None,
    fixed_kwargs: dict[str, Any],
    extensions: list[list[ExtensionElement]],
    output_schema: TemplatedText | dict[str, Any] | None,
    input_schema: TemplatedText | dict[str, Any] | None = None,
    state_binding: StateBinding | None = None,
) -> dict[str, Any]:
    """The create route's full pre-store verdict for a brand-new preset, as a verdict rather than a write.

    Runs the exact ordered checks create runs before its store write (name safety →
    description non-empty → quarantine → tool collision → agent-name collision → duplicate →
    base rules → agent authoring), then combo → schema → dry-run bake — as a
    ``valid``/``error`` verdict rather than a write. A draft may omit ``description``
    (``None``): the emptiness gate applies only to an explicitly provided value, so an
    unfilled draft validates its structure and defers the required-description rule to
    the real create's edge.
    """
    if not is_valid_preset_name(name):
        return _verdict(f"invalid preset name {name!r}: must match ^[A-Za-z0-9_-]{{1,64}}$")
    if description is not None and not description.strip():
        return _verdict("a preset description must not be empty")
    mgr = instance.app.preset_manager
    if mgr.is_quarantined(name):
        return _verdict(f"a quarantined preset {name!r} exists — delete the quarantined record first")
    if await mgr.name_conflicts(name):
        return _verdict(f"preset name {name!r} collides with an existing tool")
    if name in _pkg._agent_tool_names():
        return _verdict(f"preset name {name!r} collides with an agent tool name")
    if mgr.is_registered(name):
        return _verdict(f"preset {name!r} already exists")
    if base_tool in mgr.registered_names():
        return _verdict(f"base tool {base_tool!r} is itself a preset")
    if base_tool not in await instance.app.tools.get_tools():
        return _verdict(f"base tool {base_tool!r} is not a registered tool")
    authoring_error = await _pkg._agent_authoring_error(base_tool, fixed_kwargs)
    if authoring_error is not None:
        return _verdict(authoring_error)
    return await _verdict_bind_chain(
        base_tool,
        fixed_kwargs,
        name=name,
        description=description or "",
        output_schema=output_schema,
        input_schema=input_schema,
        extensions=extensions,
        state_binding=state_binding,
    )


async def _verdict_bind_chain(
    base_tool: str,
    fixed_kwargs: dict[str, Any],
    *,
    name: str,
    description: str,
    output_schema: TemplatedText | dict[str, Any] | None,
    input_schema: TemplatedText | dict[str, Any] | None = None,
    extensions: list[list[ExtensionElement]] | None = None,
    state_binding: StateBinding | None = None,
) -> dict[str, Any]:
    """The shared tail both modes run: combo → schema → bake → input support → validator → state binding.

    Returns a verdict — the SAME chain the real create/save doors run, so the dry run never
    reports valid on a draft the write door would 400. ``extensions`` defaults to no combos
    for the bind chain's combo/schema checks; ``state_binding`` is validated (WITHOUT
    attaching) exactly as create/save validate-and-attach it.
    """
    combos: list[list[ExtensionElement]] = extensions or []
    combo_error = _combo_registry_error(combos)
    if combo_error is not None:
        return _verdict(combo_error)
    schema_error = await _output_schema_error(base_tool, output_schema, combos)
    if schema_error is not None:
        return _verdict(schema_error)
    bind_error = await _dry_run_bind_error(
        base_tool,
        fixed_kwargs,
        name=name,
        description=description,
        output_schema=output_schema,
        input_schema=input_schema,
    )
    if bind_error is not None:
        return _verdict(bind_error)
    body = PresetBody(
        base_tool=base_tool,
        description=description,
        fixed_kwargs=fixed_kwargs,
        extensions=combos,
        output_schema=output_schema,
        input_schema=input_schema,
    )
    # A set ``input_schema`` over a base tool with no registered support is the same loud
    # authoring error the write door raises — mirror it as an invalid verdict, never a
    # silently-ignored schema the dry run passes.
    input_schema_error = _input_schema_authoring_error(body)
    if input_schema_error is not None:
        return _verdict(input_schema_error)
    write_validator_error = await _write_validator_error(body)
    if write_validator_error is not None:
        return _verdict(write_validator_error)
    state_binding_error = await _state_binding_error(state_binding)
    if state_binding_error is not None:
        return _verdict(state_binding_error)
    return _verdict(None)


@operation(
    summary="Validate a preset draft (dry-run)",
    tags=["presets"],
    errors=[BadRequestError, NotSupportedError],
    request_model=PresetValidate,
    response_model=PresetValidateVerdict,
)
async def validate_preset(
    name: str,
    base_tool: str | None = None,
    description: str | None = None,
    fixed_kwargs: dict[str, Any] | None = None,
    extensions_present: bool = False,
    extensions_value: Any = None,
    output_schema_present: bool = False,
    output_schema_value: Any = None,
    input_schema_present: bool = False,
    input_schema_value: Any = None,
    state_binding_present: bool = False,
    state_binding_value: Any = None,
) -> dict[str, Any]:
    """Report whether a preset draft would be accepted, running the SAME pre-store verdict the write route would.

    CREATE mode when no preset named ``name`` exists, VERSION mode when one does
    (mode-resolved by a store lookup). Both verdicts return 200; only a malformed body is a
    400.
    """
    # Mode resolution needs the store; refuse cleanly on a store-less deploy exactly
    # as the create route does before anything else.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})

    store = instance.app.presets.store
    try:
        await store.get_preset(name)
        active = await store.get_active_body(name)
    except PresetNotFoundError:
        active = None

    if active is None:
        # CREATE mode — base_tool is required (mirrors create's own 400), and the
        # extension combos read under create semantics (explicit ``[]`` is rejected).
        if base_tool is None:
            raise BadRequestError("body must contain a non-empty string 'base_tool'")
        extensions = read_create_extensions(extensions_present, extensions_value)
        output_schema = read_output_schema(output_schema_value) if output_schema_present else None
        input_schema = read_input_schema(input_schema_value) if input_schema_present else None
        state_binding = read_state_binding(state_binding_value) if state_binding_present else None
        return await _validate_create(
            name,
            base_tool,
            description,
            fixed_kwargs or {},
            extensions,
            output_schema,
            input_schema,
            state_binding,
        )

    # VERSION mode. The corresponding write route is save_version, whose FIRST
    # pre-store gate rejects a quarantined record — mirror that verdict (never a
    # partial check) so a quarantined-but-still-bindable preset validates as invalid,
    # not as valid.
    if instance.app.preset_manager.is_quarantined(name):
        return _verdict(f"preset {name!r} is conflicted and is delete-only")

    # base_tool carries forward and is not a version field; a provided value that
    # differs is a loud verdict, never ignored.
    if base_tool is not None and base_tool != active.base_tool:
        return _verdict("base_tool differs from the preset's active base tool; a version cannot change the base tool")
    # ``description`` IS a version field: None carries forward, an explicit string
    # sets it, and the resulting value must be non-empty (mirrors the store view).
    new_description = active.description if description is None else description
    if not new_description.strip():
        return _verdict("a preset description must not be empty")
    edit_extensions = read_edit_extensions(extensions_present, extensions_value)
    new_extensions = active.extensions if edit_extensions is None else edit_extensions
    new_output_schema = read_output_schema(output_schema_value) if output_schema_present else active.output_schema
    # ``input_schema`` is a version field under the SAME presence-flag carry-forward as
    # output_schema: PRESENT (even ``null``) is the deliberate value, ABSENT carries the
    # active value forward — mirroring the save-version door exactly.
    new_input_schema = read_input_schema(input_schema_value) if input_schema_present else active.input_schema
    # ``state_binding`` is a version field under the SAME presence-flag carry-forward:
    # PRESENT (even ``null``) is the deliberate value (``null`` clears the binding), ABSENT
    # carries the active binding forward — mirroring the save-version door exactly.
    new_state_binding = read_state_binding(state_binding_value) if state_binding_present else active.state_binding

    new_fixed_kwargs = active.fixed_kwargs if fixed_kwargs is None else fixed_kwargs
    # An authored-agent (``fixed_kwargs``) edit runs the full authoring validation
    # over the carried-forward base tool, exactly as save-version does — only when
    # fixed_kwargs was provided.
    if fixed_kwargs is not None:
        authoring_error = await _pkg._agent_authoring_error(active.base_tool, new_fixed_kwargs)
        if authoring_error is not None:
            return _verdict(authoring_error)
    return await _verdict_bind_chain(
        active.base_tool,
        new_fixed_kwargs,
        name=name,
        description=new_description,
        output_schema=new_output_schema,
        input_schema=new_input_schema,
        extensions=new_extensions,
        state_binding=new_state_binding,
    )
