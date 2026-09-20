"""Validate-before-commit gates and the agent-authoring checks every mutating door runs.

A bad edit is a loud 400 that never persists a version that cannot bind.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Annotated, Any

from pydantic import TypeAdapter, ValidationError
from tai42_contract.manifest import ExtensionElement
from tai42_contract.presets import CarryForward, PresetBody
from tai42_contract.states.binding import StateBinding
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data.json_schema_util import (
    InvalidJsonSchemaError,
    check_json_schema,
)
from tai42_kit.utils.render import SchemaBodyError, resolve_schema_body

from tai42_skeleton.app import instance
from tai42_skeleton.exceptions.exceptions import TaiValidationError
from tai42_skeleton.extensions.registry import extension_name
from tai42_skeleton.operations import BadRequestError
from tai42_skeleton.operations._authority import require_admin
from tai42_skeleton.operations.presets.references import _spec_reference_error

# This submodule's own package generation, captured at import time (a reload builds a
# fresh package + submodules together, so each generation's submodule reads its OWN
# package). ``resolve_caller`` is read through it at call time, so a test's
# ``monkeypatch.setattr`` on the package attribute is honored.
_pkg = sys.modules["tai42_skeleton.operations.presets"]


async def _agent_authoring_error(base_tool: str, fixed_kwargs: dict[str, Any]) -> str | None:
    """When ``base_tool`` names a registered agent, the first authoring violation, else ``None`` if valid.

    Returns ``None`` for a NON-agent base — a plain tool preset is governed by the create
    route's base rules alone. Each baked ``fixed_kwargs`` field must be preset-bakeable for
    the agent: a ``spec_runnable`` agent honors every ``ToolInput`` field, so all of them
    are bakeable; otherwise only the fields the agent declares in
    ``preset_bakeable_fields`` are. A baked field the runtime does not honor is
    rejected here rather than persisted as a silent no-op bake. An EMPTY
    ``fixed_kwargs`` bakes nothing, so there is nothing to gate.
    """
    agent = instance.app.agents.all_agents().get(base_tool)
    if agent is None:
        return None

    # ``fixed_kwargs`` bakes a PARTIAL spec (only the composable fields), so it is
    # validated field-by-field against the agent's ``ToolInput`` — a full-model
    # construction would spuriously fail on the run-time-only required fields the
    # author deliberately leaves unbaked. The bakeable set is every ``ToolInput``
    # field for a ``spec_runnable`` agent, else exactly the declared honored fields.
    model_fields = agent.ToolInput.model_fields
    bakeable = set(model_fields) if agent.spec_runnable else set(agent.preset_bakeable_fields)
    for key, value in fixed_kwargs.items():
        field = model_fields.get(key)
        if field is None:
            return f"fixed_kwargs field {key!r} is not a field of agent {base_tool!r}'s input"
        if key not in bakeable:
            return (
                f"fixed_kwargs field {key!r} is not preset-bakeable for agent {base_tool!r}: "
                "the agent is not spec_runnable and does not declare it in preset_bakeable_fields"
            )
        # Validate against the full annotation INCLUDING the field's pydantic
        # constraints (``Field(gt=..., min_length=..., pattern=...)``), which live in
        # ``field.metadata`` — validating the bare annotation alone would let a baked
        # value that violates a declared constraint pass author-time validation.
        annotation: Any = field.annotation
        for meta in field.metadata:
            annotation = Annotated[annotation, meta]
        try:
            TypeAdapter(annotation).validate_python(value)
        except ValidationError as exc:
            return f"fixed_kwargs field {key!r} is invalid for agent {base_tool!r}: {exc}"

    tools = set(await instance.app.tools.get_tools())
    preset_names = instance.app.preset_manager.registered_names()
    return _spec_reference_error(fixed_kwargs, tools, preset_names, "fixed_kwargs")


def _combo_registry_error(extensions: Sequence[Sequence[ExtensionElement]]) -> str | None:
    """The first extension combo that fails the LIVE registry, as a 400 message, or ``None`` if all are valid.

    A failure is an unknown name or a non-stackable-kind clash. Shared by
    create/save-version/rollback, via the public ``app.extensions.validate_combo`` accessor.
    """
    for combo in extensions:
        try:
            instance.app.extensions.validate_combo(combo)
        except TaiValidationError as exc:
            return str(exc)
    return None


async def _output_schema_error(
    base_tool: str,
    output_schema: TemplatedText | dict[str, Any] | None,
    extensions: Sequence[Sequence[ExtensionElement]],
) -> str | None:
    """The first author-time violation of an ``output_schema``, as a 400 message, or ``None`` if unset or valid.

    Shared by create/save-version/rollback so a bad schema is a 400 that never persists nor
    reaches the bind kernel.

    Rejects, in order: a by-id schema whose stored resource cannot be rendered or does not
    render to a JSON object (the ``TemplatedText | dict`` union is resolved here the same
    way the dry-run bake resolves it, so a bad by-id schema is a 400 at save); a schema that
    fails the draft-2020-12 meta-schema; a non-object schema (both dispatch paths require an
    object root); a clash with an explicit ``output_schema`` extension entry (the shape
    declared in two places); and an agent base whose run tool does not advertise
    ``response_format`` (voting_agent) — that base cannot force structured output, so reject
    at authoring rather than let the bake target a missing parameter at bind time.
    """
    if output_schema is None:
        return None
    try:
        resolved = await resolve_schema_body("output_schema", output_schema)
    except SchemaBodyError as exc:
        return str(exc)
    if resolved is None:
        raise AssertionError
    try:
        check_json_schema(resolved)
    except InvalidJsonSchemaError as exc:
        return f"output_schema is not a valid JSON Schema: {exc}"
    if resolved.get("type") != "object":
        return 'output_schema must be an object schema ("type": "object")'
    for combo in extensions:
        for element in combo:
            if extension_name(element) == "output_schema":
                return (
                    "output_schema field conflicts with an explicit 'output_schema' extension entry; "
                    "declare the output shape in exactly one place"
                )
    agent = instance.app.agents.all_agents().get(base_tool)
    if agent is not None and "response_format" not in agent.ToolInput.model_fields:
        return f"agent base {base_tool!r} does not support forced structured output (its input has no response_format)"
    return None


async def _dry_run_bind_error(
    base_tool: str,
    fixed_kwargs: dict[str, Any],
    *,
    name: str,
    description: str,
    output_schema: TemplatedText | dict[str, Any] | None = None,
    input_schema: TemplatedText | dict[str, Any] | None = None,
) -> str | None:
    """Bake the body through the kernel WITHOUT registering, returning a 400 message or ``None`` if it binds.

    Returns a 400 message if the bake raises (unknown base tool, a ``fixed_kwargs`` key
    that is not an argument of the base, an ``output_schema`` the base cannot carry, an
    ``input_schema`` over a base tool with no support). The dry run never touches the live
    registry, so a rejected edit leaves both the store and the bindings untouched.
    """
    try:
        await instance.app.presets.bind(
            base_tool,
            fixed_kwargs,
            name=name,
            description=description,
            output_schema=output_schema,
            input_schema=input_schema,
        )
    except Exception as exc:
        return f"preset {name!r} cannot bind: {exc}"
    return None


async def _write_validator_error(body: PresetBody) -> str | None:
    """The base tool's write-validator verdict for the FULL body about to persist.

    A 400 message joining its blocking issues (one per line), or ``None`` when the base tool
    has no registered validator or it passes. Any exception the validator raises propagates
    loudly — never swallowed, never treated as a pass.
    """
    validator = instance.app.presets.write_validator(body.base_tool)
    if validator is None:
        return None
    issues = await validator(body)
    if issues:
        return "\n".join(issues)
    return None


async def _state_binding_error(state_binding: StateBinding | None) -> str | None:
    """The binding's dry-run verdict: the same shape/state/template/jq/adapter checks, without the attach.

    Runs the SAME checks create and save-version run through
    ``validate_and_attach_binding``, but WITHOUT the attach — the validate door performs no
    attach. A rejection is a message (the write door would 4xx it); ``None`` when there is no
    binding or it validates.
    """
    if state_binding is None:
        return None
    from tai42_contract.states.errors import StatesError

    from tai42_skeleton.tools.state_binding import validate_binding

    try:
        await validate_binding(instance.app, state_binding)
    except (StatesError, ValueError) as exc:
        return f"invalid state_binding: {exc}"
    return None


async def _attach_body_binding(state_binding: StateBinding | CarryForward | None) -> None:
    """Validate + attach-on-use a NEWLY provided door binding at the write that activates it.

    The shared seam create, save-version and rollback all call so their binding handling
    never drifts. Its named templates attach idempotently (shared by every door/node that
    binds the state) and its expressions/adapters compile, so a bad binding is a loud 400
    that commits or re-points nothing. A carry-forward (already vetted at its own save) and
    an absent binding attach nothing.
    """
    if not isinstance(state_binding, StateBinding):
        return
    from tai42_contract.states.errors import StatesError

    from tai42_skeleton.tools.state_binding import validate_and_attach_binding

    try:
        await validate_and_attach_binding(instance.app, state_binding)
    except (StatesError, ValueError) as exc:
        raise BadRequestError(f"invalid state_binding: {exc}") from exc


async def _enforce_registration_tier(base_tool: str) -> None:
    """Enforce the base tool's authoring tier BEFORE any store write.

    A base tool declaring ``fenced`` (or ``secret``) requires the caller clears the admin
    fence to author (create/save/rollback/rename) a preset over it: resolve the acting
    principal and ``require_admin`` (a loud ``ForbiddenError`` for a non-admin). A base
    tool with no declaration keeps the presets' own default ``write`` action (no extra
    gate). ``resolve_caller`` returns an admin when access-control is disabled, so the
    fence bites only where the platform fences at all — the same semantics as a static
    ``action="fenced"`` route.
    """
    tier = instance.app.presets.registration_tier(base_tool)
    if tier in ("fenced", "secret"):
        require_admin(await _pkg.resolve_caller())


def _input_schema_authoring_error(body: PresetBody) -> str | None:
    """A 400 message when ``body`` sets an ``input_schema`` over a base tool with no support, else ``None``.

    Loud, never a silently-ignored schema — the ``_write_validator_error`` precedent.
    """
    if body.input_schema is None:
        return None
    if instance.app.presets.input_schema_support(body.base_tool) is None:
        return (
            f"base tool {body.base_tool!r} does not accept a preset input_schema "
            "(no input-schema support is registered for it)"
        )
    return None
