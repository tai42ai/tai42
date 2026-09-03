"""Preset operations — author and read versioned presets over the live engine.

A preset is a base tool + baked ``fixed_kwargs`` + extension combos, persisted as a
versioned document and registered as a live named tool. These operations are the
single source of truth for the preset surface: the HTTP routes in
``routers/presets.py`` are thin adapters over them, and the MCP projection binds
each as a tool (so extensions wrap it and other presets can bake over it).

Every mutating door validates a body CAN bind BEFORE any store write (a bad edit is
a loud 400, never a committed version that can never bind), persists THEN registers,
compensates a residual register failure by re-pointing the store so store + live
never diverge, and fans the rebind/removal out on the worker bus — embedding that
per-worker fleet report in the mutation response (the ``fanout`` field, the same shape
the template writers return) so a deployer has the read-your-writes barrier signal the
platform already computes, not merely a log line. The concrete app
singleton (``from tai42_skeleton.app import instance``) is reached for the store view,
the register/reload engine, the generic versioned store (the HARD delete the view
does not expose), and ``emit_list_changed``. Success values are returned bare — the
route adapter wraps them in ``{"data": ...}`` at the HTTP edge.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Annotated, Any

from pydantic import BaseModel, TypeAdapter, ValidationError
from tai42_contract.agent.base import PresetSpec
from tai42_contract.manifest import ExtensionElement
from tai42_contract.presets import CARRY_FORWARD, CarryForward, PresetBody, PresetSeed
from tai42_contract.presets.errors import (
    PresetExistsError,
    PresetNameConflictError,
    PresetNotFoundError,
    PresetVersionNotFoundError,
)
from tai42_contract.versioning.errors import DocumentVersionNotFoundError
from tai42_kit.db import component_store_configured
from tai42_kit.utils.data.json_schema_util import (
    InvalidJsonSchemaError,
    check_json_schema,
)

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import FleetResult, LocalApplyResult, OpOutcome
from tai42_skeleton.db import SKELETON_COMPONENT, not_configured_message
from tai42_skeleton.exceptions.exceptions import TaiValidationError
from tai42_skeleton.extensions.registry import extension_name
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    NotSupportedError,
    operation,
)
from tai42_skeleton.operations._authority import require_admin, resolve_caller
from tai42_skeleton.operations._broadcast import fleet_fanout, log_non_convergence, snapshot_membership
from tai42_skeleton.presets.manager import is_valid_preset_name

logger = logging.getLogger(__name__)

# The machine-readable code every preset OFF refusal carries when the
# versioned-document store is unconfigured; the message is rendered from the live
# binding at raise time. A 501 (a capability the deployment lacks), never a
# transient 503 — the store is absent, not momentarily down.
_NOT_CONFIGURED_CODE = "versioning-not-configured"
_NOT_CONFIGURED_NOUN = "versioned-document store"


# -- request models (the emitted spec's requestBody schemas) -----------------


class PresetCreate(BaseModel):
    """A preset-creation request. ``description`` is the bound tool's LLM-facing
    docstring — REQUIRED non-empty on every create. ``extensions`` is the list of
    extension combos (each element an extension name or a ``{"name", "config"}``
    mapping binding author config). ``output_schema`` is the optional author-set
    OUTPUT JSON Schema (an object schema)."""

    name: str
    base_tool: str
    description: str
    fixed_kwargs: dict[str, Any] = {}
    extensions: list[list[ExtensionElement]] = []
    output_schema: dict[str, Any] | None = None
    input_schema: dict[str, Any] | None = None


class PresetVersionSave(BaseModel):
    """A new-preset-version request. At least one field must be present; an
    omitted field carries forward, an explicit ``[]`` clears (the store sentinel
    rule). ``output_schema`` carries forward when omitted, clears on an explicit
    ``null``, and wins on an explicit object schema; ``input_schema`` follows the
    SAME carry-forward rule (omitted carries, ``null`` clears, an object schema
    wins). ``description`` carries forward when omitted and is SET by an explicit
    non-empty string (an explicit ``""`` is rejected — the resulting description is
    validated non-empty on every save)."""

    fixed_kwargs: dict[str, Any] | None = None
    extensions: list[list[ExtensionElement]] | None = None
    output_schema: dict[str, Any] | None = None
    input_schema: dict[str, Any] | None = None
    description: str | None = None


class PresetRollback(BaseModel):
    """A rollback request — the target version to make active."""

    version: int


class PresetRename(BaseModel):
    """A rename request — the new preset (tool) name."""

    new_name: str


class PresetValidate(BaseModel):
    """A preset validation (dry-run) request — the full create field set. When a
    preset named ``name`` already exists the door validates a NEW VERSION: then
    ``base_tool`` / ``description`` carry forward from the active body (a provided
    value that differs is rejected) and any absent field merges from it, exactly as
    the save-version route merges."""

    name: str
    base_tool: str | None = None
    description: str | None = None
    fixed_kwargs: dict[str, Any] | None = None
    extensions: list[list[ExtensionElement]] | None = None
    output_schema: dict[str, Any] | None = None
    input_schema: dict[str, Any] | None = None


class PresetVersionTags(BaseModel):
    """Replace a preset version's ``tags`` annotation (labels on an immutable
    version body — no rebind)."""

    tags: list[str]


# -- body-structure readers (raise the byte-stable 400 the routes surfaced) --

# These validate the SHAPE of the nested combo / output-schema payload. They are
# shared by the router's HTTP-edge extractors (which parse the create/save bodies)
# and the validate operation (whose create-vs-version reading is mode-dependent, so
# it happens inside the op after the store lookup that resolves the mode). A
# malformed structure raises :class:`BadRequestError`, mapped to the same 400 the
# route handler surfaced.


def read_element(element: Any) -> ExtensionElement:
    """One combo element, structurally validated: a non-empty extension NAME
    (bare string), or a ``{"name": <non-empty str>, "config": <dict>}`` mapping
    binding author config (``config`` REQUIRED — a config-less selection is the
    bare-string form, so a config-free dict is malformed) with no other keys.
    Anything else is a loud 400. Registration of the name is checked later against
    the live registry."""
    if isinstance(element, str):
        if not element:
            raise BadRequestError("an extension name must be a non-empty string")
        return element
    if isinstance(element, dict):
        name = element.get("name")
        if not isinstance(name, str) or not name:
            raise BadRequestError("an extension element must have a non-empty string 'name'")
        config = element.get("config")
        if not isinstance(config, dict):
            raise BadRequestError(f"extension element {name!r} must carry a 'config' mapping")
        extra = set(element) - {"name", "config"}
        if extra:
            raise BadRequestError(f"extension element {name!r} has unexpected keys: {sorted(extra)!r}")
        return {"name": name, "config": dict(config)}
    raise BadRequestError("each combo element must be an extension name or a {'name', 'config'} mapping")


def read_combos(extensions: Any) -> list[list[ExtensionElement]]:
    """A list of extension combos, each a non-empty list of combo elements. The
    empty INNER combo (``[[]]`` or any ``[]`` member) is rejected — mirrors the
    view's rule so a create/edit is guarded before any store write."""
    result: list[list[ExtensionElement]] = []
    for combo in extensions:
        if not isinstance(combo, list) or not combo:
            raise BadRequestError("each extension combo must be a non-empty list of extension elements")
        result.append([read_element(element) for element in combo])
    return result


def read_create_extensions(present: bool, value: Any) -> list[list[ExtensionElement]]:
    """Create's extension combos: an absent field means no extensions, an explicit
    ``extensions: []`` is REJECTED (nothing to clear on create), and an empty inner
    combo is rejected."""
    if not present:
        return []
    if not isinstance(value, list):
        raise BadRequestError("'extensions' must be a list of combos")
    if value == []:
        raise BadRequestError("explicit empty 'extensions' is rejected; omit the field for no extensions")
    return read_combos(value)


def read_edit_extensions(present: bool, value: Any) -> list[list[ExtensionElement]] | None:
    """Save-version's extension combos under the carry-forward sentinel: absent or
    ``null`` carries forward (``None``); ``[]`` clears; an empty inner combo is
    rejected."""
    if not present or value is None:
        return None
    if not isinstance(value, list):
        raise BadRequestError("'extensions' must be a list of combos")
    return read_combos(value)


def read_output_schema(value: Any) -> dict[str, Any] | None:
    """The optional author-set output schema from a request value: ``null`` →
    ``None``; a JSON object → itself; anything else → a loud 400."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BadRequestError("'output_schema' must be a JSON object (a JSON Schema)")
    return value


def read_input_schema(value: Any) -> dict[str, Any] | None:
    """The optional author-set input schema from a request value: ``null`` → ``None``;
    a JSON object → itself; anything else → a loud 400."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BadRequestError("'input_schema' must be a JSON object (a JSON Schema)")
    return value


# -- agent-authoring validation ----------------------------------------------

# An authored agent is a preset whose ``base_tool`` names an agent's run tool. On top
# of the create route's base-tool rules, an agent base adds these authoring checks: every
# baked ``fixed_kwargs`` field must be preset-bakeable for the agent (a ``spec_runnable``
# agent honors every ``ToolInput`` field; otherwise only the fields it declares in
# ``preset_bakeable_fields``), the baked ``fixed_kwargs`` must be valid partial
# ``ToolInput``, and every spec reference (``tool_names`` / inline ``presets`` /
# nested ``subagents``) must resolve at author time — recursively, at every depth.


def _agent_tool_names() -> set[str]:
    """Every registered agent's declared ``tool_name``.

    A ``tool_name`` can differ from the decorator-registration name: the run tool
    binds under the REGISTRATION name (so that name is already a live tool the
    ``name_conflicts`` guard catches), but the ``tool_name`` may not be a bound tool.
    Keeping the authored-agent name off BOTH sets keeps one unambiguous agent-name
    space."""
    return {agent.tool_name for agent in instance.app.agents.all_agents().values()}


def _spec_reference_error(
    node: dict[str, Any], tools: set[str], preset_names: frozenset[str], where: str
) -> str | None:
    """The first unresolved reference in one spec node, or ``None`` if all resolve.

    Checks the node's ``tool_names`` (each must be a registered tool), its inline
    ``presets`` (each a self-contained ``PresetSpec`` whose ``base_tool`` is a
    registered NON-preset tool — the same flat-preset rule as the create route), and
    recurses into every inline ``subagents`` spec, so a bad reference at ANY depth is
    caught loudly rather than silently dropped."""
    tool_names = node.get("tool_names", [])
    if not isinstance(tool_names, list):
        return f"{where}.tool_names must be a list of tool names"
    for tool_name in tool_names:
        if not isinstance(tool_name, str):
            return f"{where}.tool_names entries must be strings"
        if tool_name not in tools:
            return f"{where}.tool_names references unknown tool {tool_name!r}"

    presets = node.get("presets", [])
    if not isinstance(presets, list):
        return f"{where}.presets must be a list of preset specs"
    for i, entry in enumerate(presets):
        try:
            preset = PresetSpec.model_validate(entry)
        except ValidationError as exc:
            return f"{where}.presets[{i}] is not a valid preset spec: {exc}"
        # An inline preset becomes a bound tool whose docstring IS its description,
        # fed verbatim to the LLM — an empty one is a behavioral defect, refused at
        # authoring exactly like the create door refuses it (side door closed).
        if not preset.description.strip():
            return f"{where}.presets[{i}] description must not be empty"
        if preset.base_tool in preset_names:
            return f"{where}.presets[{i}] base_tool {preset.base_tool!r} is itself a preset"
        if preset.base_tool not in tools:
            return f"{where}.presets[{i}] base_tool {preset.base_tool!r} is not a registered tool"

    subagents = node.get("subagents", [])
    if not isinstance(subagents, list):
        return f"{where}.subagents must be a list of sub-agent specs"
    for i, entry in enumerate(subagents):
        if not isinstance(entry, dict):
            return f"{where}.subagents[{i}] must be an object"
        nested = _spec_reference_error(entry, tools, preset_names, f"{where}.subagents[{i}]")
        if nested is not None:
            return nested
    return None


def _referenced_tool_names(node: dict[str, Any]) -> set[str]:
    """Every tool name a spec node composes in its ``tool_names`` at any depth,
    recursing ``subagents`` — the SAME traversal :func:`_spec_reference_error` walks,
    read-only. Only ``tool_names`` can name a preset: inline ``presets`` entries and a
    ``base_tool`` are rejected at authoring if they name a preset, so neither is
    scanned here. This is the builtin ``fixed_kwargs`` walk the combined collector
    :func:`_preset_references` unions with the base tool's declared extractor."""
    names: set[str] = set()
    tool_names = node.get("tool_names", [])
    if isinstance(tool_names, list):
        names.update(name for name in tool_names if isinstance(name, str))
    subagents = node.get("subagents", [])
    if isinstance(subagents, list):
        for entry in subagents:
            if isinstance(entry, dict):
                names.update(_referenced_tool_names(entry))
    return names


def _preset_references(body: PresetBody) -> set[str]:
    """Every tool name a preset body composes as tools: the UNION of the builtin
    ``fixed_kwargs`` walk (:func:`_referenced_tool_names`) and the names the base
    tool's DECLARED ``tool_refs`` extractor reads from ``fixed_kwargs`` (only when one
    is registered for ``body.base_tool``). Population intersection, self-exclusion and
    sorting are the callers' concern. The extractor is called raw: an exception
    propagates loudly, and a non-string entry it returns is a plugin bug raised here —
    never silently dropped, never an empty-list fallback."""
    names = _referenced_tool_names(body.fixed_kwargs)
    extractor = instance.app.tools.tool_refs_extractor(body.base_tool)
    if extractor is not None:
        for entry in extractor(body.fixed_kwargs):
            if not isinstance(entry, str):
                raise TypeError(
                    f"tool_refs extractor for base tool {body.base_tool!r} returned a non-string reference {entry!r}"
                )
            names.add(entry)
    return names


def _reference_maps(bodies: dict[str, PresetBody]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """The ``uses`` and ``used_by`` maps over the active preset population, built in one
    pass. ``uses[X]`` is the sorted OTHER preset names X's active body composes as tools
    (the combined collector :func:`_preset_references`, intersected with the population
    so base and foreign tools never appear); ``used_by[X]`` is the sorted OTHER presets
    whose active bodies compose X. Self is never listed on either side. Every population
    name is a key on both maps, empty when it has no references."""
    population = set(bodies)
    uses: dict[str, set[str]] = {name: set() for name in bodies}
    used_by: dict[str, set[str]] = {name: set() for name in bodies}
    for name, body in bodies.items():
        for referenced in _preset_references(body) & population:
            if referenced == name:
                continue
            uses[name].add(referenced)
            used_by[referenced].add(name)
    return (
        {name: sorted(names) for name, names in uses.items()},
        {name: sorted(names) for name, names in used_by.items()},
    )


def _referencing_presets(old_name: str, bodies: dict[str, PresetBody]) -> list[str]:
    """Every OTHER preset whose ACTIVE body composes ``old_name`` as a tool (the
    combined collector :func:`_preset_references`, so a DECLARED reference counts too),
    sorted for a stable, fully-listed answer. This is the PRESET-BODY leg of the rename
    referee union — :func:`_rename_referees` unions it with every registered referee
    (platform wiring + plugin providers). Only active bodies are walked: a non-active
    historical version may still name the old tool, loud at authoring / run time if ever
    rolled back (delete's existing posture)."""
    return sorted(name for name, body in bodies.items() if name != old_name and old_name in _preset_references(body))


async def _rename_referees(name: str) -> list[str]:
    """Every live reference a rename of ``name`` would strand — the full union the rename
    gate blocks on and the referees preview door reports: the OTHER presets whose active
    body composes ``name`` (:func:`_referencing_presets`) plus every registered rename
    referee's descriptions (the platform-internal wiring referees + any plugin provider).
    Each referee is consulted for the OLD name; a referee RAISING propagates loudly — a
    rename never proceeds past an unreadable holder store (no silent bypass)."""
    holders = _referencing_presets(name, await instance.app.presets.list_active_bodies())
    for referee in instance.app.tools.rename_referees():
        holders.extend(await referee(name))
    return holders


async def _agent_authoring_error(base_tool: str, fixed_kwargs: dict[str, Any]) -> str | None:
    """When ``base_tool`` names a registered agent, the first authoring violation, or
    ``None`` if the spec is valid. Returns ``None`` for a NON-agent base — a plain
    tool preset is governed by the create route's base rules alone.

    Each baked ``fixed_kwargs`` field must be preset-bakeable for the agent: a
    ``spec_runnable`` agent honors every ``ToolInput`` field, so all of them are
    bakeable; otherwise only the fields the agent declares in
    ``preset_bakeable_fields`` are. A baked field the runtime does not honor is
    rejected here rather than persisted as a silent no-op bake. An EMPTY
    ``fixed_kwargs`` bakes nothing, so there is nothing to gate."""
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


# -- validate-before-commit helpers ------------------------------------------

# create/save-version/rollback all validate a body CAN bind before any store write,
# so a bad edit is a loud 400 rather than a committed version that can never bind
# (which would brick the preset into delete-only). Two checks: each extension combo
# against the live registry, and a dry-run of the bake with no registration.


def _combo_registry_error(extensions: Sequence[Sequence[ExtensionElement]]) -> str | None:
    """The first extension combo that fails the LIVE registry (unknown name or a
    non-stackable-kind clash), as a 400 message, or ``None`` if all combos are
    valid — shared by create/save-version/rollback, via the public
    ``app.extensions.validate_combo`` accessor."""
    for combo in extensions:
        try:
            instance.app.extensions.validate_combo(combo)
        except TaiValidationError as exc:
            return str(exc)
    return None


async def _output_schema_error(
    base_tool: str, output_schema: dict[str, Any] | None, extensions: Sequence[Sequence[ExtensionElement]]
) -> str | None:
    """The first author-time violation of an ``output_schema``, as a 400 message, or
    ``None`` if it is unset or valid — shared by create/save-version/rollback so a
    bad schema is a 400 that never persists nor reaches the bind kernel.

    Rejects, in order: a schema that fails the draft-2020-12 meta-schema; a
    non-object schema (both dispatch paths require an object root); a clash with an
    explicit ``output_schema`` extension entry (the shape declared in two places);
    and an agent base whose run tool does not advertise ``response_format``
    (voting_agent) — that base cannot force structured output, so reject at
    authoring rather than let the bake target a missing parameter at bind time."""
    if output_schema is None:
        return None
    try:
        check_json_schema(output_schema)
    except InvalidJsonSchemaError as exc:
        return f"output_schema is not a valid JSON Schema: {exc}"
    if output_schema.get("type") != "object":
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
    output_schema: dict[str, Any] | None = None,
    input_schema: dict[str, Any] | None = None,
) -> str | None:
    """Bake the body through the kernel WITHOUT registering, returning a 400 message
    if the bake raises (unknown base tool, a ``fixed_kwargs`` key that is not an
    argument of the base, an ``output_schema`` the base cannot carry, an ``input_schema``
    over a base tool with no support) or ``None`` if it binds. The dry run never touches
    the live registry, so a rejected edit leaves both the store and the bindings
    untouched."""
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
    """The base tool's write-validator verdict for the FULL body about to persist,
    as a 400 message joining its blocking issues (one per line), or ``None`` when
    the base tool has no registered validator or it passes. Any exception the
    validator raises propagates loudly — never swallowed, never treated as a pass."""
    validator = instance.app.presets.write_validator(body.base_tool)
    if validator is None:
        return None
    issues = await validator(body)
    if issues:
        return "\n".join(issues)
    return None


async def _enforce_registration_tier(base_tool: str) -> None:
    """Enforce the base tool's authoring tier BEFORE any store write — ruling 14.

    A base tool declaring ``fenced`` (or ``secret``) requires the caller clears the admin
    fence to author (create/save/rollback/rename) a preset over it: resolve the acting
    principal and ``require_admin`` (a loud ``ForbiddenError`` for a non-admin). A base
    tool with no declaration keeps the presets' own default ``write`` action (no extra
    gate). ``resolve_caller`` returns an admin when access-control is disabled, so the
    fence bites only where the platform fences at all — the same semantics as a static
    ``action="fenced"`` route."""
    tier = instance.app.presets.registration_tier(base_tool)
    if tier in ("fenced", "secret"):
        require_admin(await resolve_caller())


async def _validate_authoring(name: str, body: PresetBody) -> None:
    """Run the create/save authoring validators over a FULL body about to persist,
    raising the same loud :class:`BadRequestError` those doors raise: non-empty
    description, agent-authoring, combo registry, output-schema, dry-run bind,
    input-schema support, and the base tool's write validator. The seed re-point path
    (which writes straight to the versioned store, bypassing the create/save cores)
    calls this BEFORE its write so a re-point onto a base that rejects the carried
    body is refused loudly, never persisted and served unvalidated."""
    if not body.description.strip():
        raise BadRequestError("a preset description must not be empty")
    # A preset's base must be a registered NON-preset tool (a preset cannot be another
    # preset's base — chaining makes rehydration order-dependent). The dry-run bind
    # binds via ``get_tool`` and a preset IS a registered tool, so it would pass a
    # preset base; mirror create's guard here so the re-point path rejects it too.
    if body.base_tool in instance.app.preset_manager.registered_names():
        raise BadRequestError(f"base tool {body.base_tool!r} is itself a preset")
    authoring_error = await _agent_authoring_error(body.base_tool, body.fixed_kwargs)
    if authoring_error is not None:
        raise BadRequestError(authoring_error)
    combo_error = _combo_registry_error(body.extensions)
    if combo_error is not None:
        raise BadRequestError(combo_error)
    schema_error = await _output_schema_error(body.base_tool, body.output_schema, body.extensions)
    if schema_error is not None:
        raise BadRequestError(schema_error)
    bind_error = await _dry_run_bind_error(
        body.base_tool,
        body.fixed_kwargs,
        name=name,
        description=body.description,
        output_schema=body.output_schema,
        input_schema=body.input_schema,
    )
    if bind_error is not None:
        raise BadRequestError(bind_error)
    input_schema_error = _input_schema_authoring_error(body)
    if input_schema_error is not None:
        raise BadRequestError(input_schema_error)
    write_validator_error = await _write_validator_error(body)
    if write_validator_error is not None:
        raise BadRequestError(write_validator_error)


def _input_schema_authoring_error(body: PresetBody) -> str | None:
    """A 400 message when ``body`` sets an ``input_schema`` over a base tool with no
    registered input-schema support, else ``None``. Loud, never a silently-ignored
    schema — the ``_write_validator_error`` precedent."""
    if body.input_schema is None:
        return None
    if instance.app.presets.input_schema_support(body.base_tool) is None:
        return (
            f"base tool {body.base_tool!r} does not accept a preset input_schema "
            "(no input-schema support is registered for it)"
        )
    return None


# -- record views ------------------------------------------------------------


def _store_record_view(
    name: str,
    active_version: int,
    body: PresetBody,
    *,
    uses: list[str],
    used_by: list[str],
) -> dict[str, Any]:
    """A store-backed record row: identity + active-body fields + the
    ``conflicted`` flag (name in the quarantine map) with its ``conflicted_reason``
    (the human-readable cause, ``null`` when not conflicted), plus the ``uses`` /
    ``used_by`` cross-references (sorted OTHER active presets this body composes, and
    that compose it — see :func:`_reference_maps`). Takes the already-fetched active
    ``body`` and this row's two reference lists so the caller batches the reads (the
    list route) or reuses one read (the get route) rather than round-tripping per
    row."""
    mgr = instance.app.preset_manager
    return {
        "name": name,
        "base_tool": body.base_tool,
        "description": body.description,
        "active_version": active_version,
        "extensions": [list(combo) for combo in body.extensions],
        "output_schema": body.output_schema,
        "input_schema": body.input_schema,
        "conflicted": mgr.is_quarantined(name),
        "conflicted_reason": mgr.quarantine_reason(name),
        "uses": uses,
        "used_by": used_by,
    }


def _new_record_view(
    name: str,
    base_tool: str,
    description: str,
    extensions: list[list[ExtensionElement]],
    output_schema: dict[str, Any] | None,
    input_schema: dict[str, Any] | None,
    *,
    active_version: int,
    uses: list[str],
    used_by: list[str],
) -> dict[str, Any]:
    """The record shape a fresh create returns — the identity + active-body fields
    from the just-applied spec (a fresh preset is never conflicted), plus this row's
    ``uses`` / ``used_by`` cross-references (see :func:`_reference_maps`), computed by
    the caller from the post-write active-body population so the create response
    parses under the same record schema as the list / get rows."""
    return {
        "name": name,
        "base_tool": base_tool,
        "description": description,
        "active_version": active_version,
        "extensions": [list(combo) for combo in extensions],
        "output_schema": output_schema,
        "input_schema": input_schema,
        "conflicted": False,
        "conflicted_reason": None,
        "uses": uses,
        "used_by": used_by,
    }


async def _wire_snapshot(name: str) -> dict[str, Any] | None:
    """The serialized wire tool for ``name`` (``to_mcp_tool().model_dump()``), or
    ``None`` if the name is not currently bound — the client-visible listing state
    the emit guard diffs across a reload."""
    tools = await instance.app.tools.get_tools()
    tool = tools.get(name)
    return None if tool is None else tool.to_mcp_tool().model_dump()


# -- bus fan-out -------------------------------------------------------------

# The wire op names these fan-outs publish. Named once because the op-start census
# is read under the same name the publish carries, at a different point in the
# caller — two literals could drift into censusing one op and publishing another.
_RELOAD_OP = "reload_tool"
_REMOVE_OP = "remove_tool"


def _fleet_fanout_ready() -> bool:
    """Whether this worker can fan a preset op out to the fleet: its bus is built AND
    its slot identity is minted.

    ``False`` only during the boot startup handlers — the declared-preset-seed applier
    runs as an ``on_startup`` handler, BEFORE the bus subscription claims this worker's
    slot (the bus is built and its identity minted only after the handlers). Every worker
    runs that applier on its OWN boot, so a boot-time seed create/upgrade needs no fan-out
    — each worker applies it locally. A reload re-runs the applier with the bus already
    subscribed, so it fans out normally, as does every API-door mutation."""
    try:
        _ = instance.app.bus.identity
    except RuntimeError:
        return False
    return True


async def _census_at_start(op_name: str) -> dict[str, int] | None:
    """Pin the expected-confirmation membership BEFORE this worker's store write and
    local rebind — the untargeted-publisher discipline
    :func:`~tai42_skeleton.operations._broadcast.snapshot_membership` owns.

    These publishers apply locally and broadcast afterwards, so ``publish``'s own
    census is taken on the far side of the store write: a sibling whose presence
    faded across it would simply be absent from the expected set, and the op would
    report converged without it. The caller reads this at its true pre-apply point,
    which is why it is a parameter of the fan-out rather than taken inside it.

    ``None`` when the bus cannot yet fan out (the boot-time seed applier, pre-subscribe):
    :func:`_fanout_reload` / :func:`_fanout_remove` collapse to a local-only report."""
    if not _fleet_fanout_ready():
        return None
    return await snapshot_membership(instance.app.bus, op_name)


async def _fanout_reload(name: str, expected_at_start: dict[str, int] | None) -> FleetResult:
    """Broadcast a preset rebind on the worker bus so every worker re-reads the active
    body and rebinds ``name``. The op carries only ``kind`` + ``name``; each worker
    re-reads the store itself. The store write and local rebind have already landed by
    the time this runs (its self entry is a truthful ``applied``), so an unconfirmed
    sibling is surfaced as a loud non-convergence ERROR log, and re-running the
    mutation (or a ``reload_config``) is the recovery.

    The local apply is separate and compensation-rolled-back upstream, so this cannot
    ride ``broadcast()``'s apply-inside model — but it shares its non-convergence
    logging and its op-start census (``expected_at_start``, from
    :func:`_census_at_start`) so a stranded sibling is never silent.

    Returns the per-worker fleet report so the mutation door can embed it in its
    response (:func:`~tai42_skeleton.operations._broadcast.fleet_fanout` shapes it — the
    read-your-writes barrier a deployer needs) exactly as the template writers do. The
    sibling names a rename's second fan-out is judged against are derived from it with
    :func:`_addressed_siblings`; see :func:`_union_census`.

    Collapses to a ``local_only`` report when the bus cannot yet fan out (the boot-time
    seed applier, before this worker's slot is claimed) — the local rebind has already
    landed and every sibling seeds itself on its own boot, so there is no fleet to reach."""
    if not _fleet_fanout_ready():
        return FleetResult(op=_RELOAD_OP, local_only=True)
    report = await instance.app.bus.publish(
        {"op": _RELOAD_OP, "kind": "preset", "name": name},
        None,
        LocalApplyResult(outcome=OpOutcome.applied),
        expected_at_start=expected_at_start,
    )
    log_non_convergence(report)
    return report


def _addressed_siblings(report: FleetResult) -> set[str]:
    """The sibling names a broadcast actually ADDRESSED (self excluded) — the report IS
    that set, expected verdicts and gap rows alike. A rename's second fan-out needs it:
    see :func:`_union_census`."""
    self_name = instance.app.bus.identity.name
    return {result.name for result in report.results if result.name != self_name}


async def _fanout_remove(name: str, expected_at_start: dict[str, int] | None) -> FleetResult:
    """Broadcast a preset removal on the worker bus so every worker tears ``name``
    down. Same already-applied self entry, op-start census, and non-convergence
    logging as :func:`_fanout_reload`, and it likewise returns the per-worker fleet
    report so the delete door can embed it in its response. Collapses to a ``local_only``
    report when the bus cannot yet fan out (the boot-time seed applier, pre-subscribe)."""
    if not _fleet_fanout_ready():
        return FleetResult(op=_REMOVE_OP, local_only=True)
    report = await instance.app.bus.publish(
        {"op": _REMOVE_OP, "kind": "preset", "name": name},
        None,
        LocalApplyResult(outcome=OpOutcome.applied),
        expected_at_start=expected_at_start,
    )
    log_non_convergence(report)
    return report


# A worker the reload reached but no census of ours ever saw. Real generations are
# minted by INCR and start at 1, so this is below every life: the reply gate treats
# any reply as a successor's and the worker gets a computed verdict instead of a
# silent pass. That is the honest reading of "it was addressed, we do not know
# which life".
_LIFE_UNKNOWN = 0


def _union_census(
    at_start: dict[str, int] | None,
    addressed: set[str],
    after_reload: dict[str, int] | None,
) -> dict[str, int]:
    """The membership a rename's REMOVE fan-out is judged against.

    A rename publishes twice, and the second half cannot simply reuse the first
    half's snapshot: a worker that joined after the snapshot was taken is not in
    it, yet the reload broadcast reached it and told it to bind ``new_name``. Left
    unexpected by the remove, that worker keeps the old binding and the op reports
    converged anyway. So the expected set is the UNION of three readings, in
    descending order of authority over the generation:

    * the pre-rename snapshot — the life that was owed the rename from the start,
      and the one :meth:`~tai42_skeleton.app.bus.WorkerBus.publish` re-admits at;
    * a census taken after the reload broadcast — a joiner still live, at its
      current generation;
    * every name the reload REPORT named, which is the only reading that cannot
      miss a worker the reload actually addressed (a joiner that faded during the
      broadcast is in neither census), carried at :data:`_LIFE_UNKNOWN`.
    """
    union: dict[str, int] = dict(after_reload or {})
    union.update(at_start or {})
    for worker in addressed:
        union.setdefault(worker, _LIFE_UNKNOWN)
    return union


# -- list --------------------------------------------------------------------


@operation(summary="List presets", tags=["presets"])
async def list_presets() -> list[dict[str, Any]]:
    """One row per store-backed record (the presets plus the ``conflicted``
    quarantined ones) — the population the presets management table shows."""
    rows: list[dict[str, Any]] = []
    # A store-less deploy (no versioned store configured) has no presets — skip the
    # Postgres read and serve an empty list.
    if component_store_configured(SKELETON_COMPONENT):
        records = await instance.app.presets.store.list_presets()
        # One batched active-body read instead of a per-record round-trip (N+1).
        bodies = await instance.app.presets.list_active_bodies()
        # Both cross-reference maps in one pass over the population.
        uses_map, used_by_map = _reference_maps(bodies)
        # ``records`` and ``bodies`` are two separate reads; a preset deleted between
        # them is gone from ``bodies`` — skip it rather than KeyError, it is no longer
        # a live row to list.
        rows = [
            _store_record_view(
                rec.name,
                rec.active_version,
                bodies[rec.name],
                uses=uses_map[rec.name],
                used_by=used_by_map[rec.name],
            )
            for rec in records
            if rec.name in bodies
        ]
    return rows


# -- create ------------------------------------------------------------------


async def _create_preset_core(
    name: str,
    base_tool: str,
    description: str,
    fixed_kwargs: dict[str, Any],
    extensions: list[list[ExtensionElement]],
    output_schema: dict[str, Any] | None,
    input_schema: dict[str, Any] | None = None,
    *,
    tags: list[str] | None = None,
    enforce_tier: bool = True,
) -> tuple[Any, FleetResult]:
    """The reusable create path shared by the create door and the seed applier: ordered
    name pre-checks → base rule → agent-authoring → combo/schema/bind + input-schema +
    write-validator validation → (optional) registration-tier fence → store write THEN
    register (rolling the row fully back on a register failure) → one ``list_changed`` →
    the bus rebind fan-out. Returns the store record + the per-worker fleet report.

    ``enforce_tier`` runs the caller-authorization fence (create's door behavior); the
    seed applier passes ``False`` — a platform seed has no caller to fence and runs the
    identical content path otherwise (no logic duplicated between the two). ``tags`` labels
    version 1 in the SAME store commit (``None`` is the door's untagged create); the seed
    applier passes the shipped-default tag so the create-then-tag has no untagged window."""
    # A preset name is a live tool name + a ``{name}`` route segment, so it must be
    # tool-name-safe (a slash-bearing name would never match the routes; an
    # over-long one collides after client-tool truncation).
    if not is_valid_preset_name(name):
        raise BadRequestError(f"invalid preset name {name!r}: must match ^[A-Za-z0-9_-]{{1,64}}$")
    # The description is the bound tool's LLM-facing docstring — required non-empty on
    # every create path, so no path produces an empty-docstring preset tool.
    if not description.strip():
        raise BadRequestError("a preset description must not be empty")

    mgr = instance.app.preset_manager
    # Three ordered name pre-checks — the quarantine 409 wins for a name that would
    # also collide, then the collision guard, then the spec-map duplicate.
    if mgr.is_quarantined(name):
        raise ConflictError(f"a quarantined preset {name!r} exists — delete the quarantined record first")
    if await mgr.name_conflicts(name):
        raise ConflictError(f"preset name {name!r} collides with an existing tool")
    # The same collision guard, extended to the agent-name space: an agent's
    # registration name is already a live tool (caught above), but its ``tool_name``
    # may not be — keep the authored name off that set too so one agent-name space
    # stays unambiguous.
    if name in _agent_tool_names():
        raise BadRequestError(f"preset name {name!r} collides with an agent tool name")
    if mgr.is_registered(name):
        raise ConflictError(f"preset {name!r} already exists")

    # A preset's base must be a registered NON-preset tool (a preset cannot be
    # another preset's base — chaining would make rehydration order-dependent).
    if base_tool in mgr.registered_names():
        raise BadRequestError(f"base tool {base_tool!r} is itself a preset")
    if base_tool not in await instance.app.tools.get_tools():
        raise BadRequestError(f"base tool {base_tool!r} is not a registered tool")

    # When the base is an agent tool, this is an authored agent: every baked field
    # must be preset-bakeable for the agent and its baked spec must validate +
    # resolve every reference.
    authoring_error = await _agent_authoring_error(base_tool, fixed_kwargs)
    if authoring_error is not None:
        raise BadRequestError(authoring_error)

    # Validate-before-commit: reject an unknown/illegal extension combo and a body
    # that cannot bake, BEFORE any store write, so a bad create is a 400 that never
    # persists a row.
    combo_error = _combo_registry_error(extensions)
    if combo_error is not None:
        raise BadRequestError(combo_error)
    schema_error = await _output_schema_error(base_tool, output_schema, extensions)
    if schema_error is not None:
        raise BadRequestError(schema_error)
    bind_error = await _dry_run_bind_error(
        base_tool,
        fixed_kwargs,
        name=name,
        description=description,
        output_schema=output_schema,
        input_schema=input_schema,
    )
    if bind_error is not None:
        raise BadRequestError(bind_error)
    body = PresetBody(
        base_tool=base_tool,
        description=description,
        fixed_kwargs=fixed_kwargs,
        extensions=extensions,
        output_schema=output_schema,
        input_schema=input_schema,
    )
    # An ``input_schema`` over a base tool with no registered support is a loud 400 that
    # never persists a row (never a silently-ignored schema).
    input_schema_error = _input_schema_authoring_error(body)
    if input_schema_error is not None:
        raise BadRequestError(input_schema_error)
    # The base tool's own write validator over the full body about to persist — a
    # body its base tool rejects is a 400 that never persists a row.
    write_validator_error = await _write_validator_error(body)
    if write_validator_error is not None:
        raise BadRequestError(write_validator_error)
    # Registration-tier fence (ruling 14): a base tool declaring ``fenced``/``secret``
    # requires the caller clears the admin fence to author a preset over it. Skipped for
    # a platform seed (``enforce_tier=False``) — no caller to fence.
    if enforce_tier:
        await _enforce_registration_tier(base_tool)

    # A preset needs the durable store; on a store-less deploy (the skeleton
    # database unconfigured) refuse cleanly here — the same predicate the
    # list / delete / reconcile paths gate on — rather than let create open Postgres
    # and fail with an opaque 500.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})

    # Clean slate: a dangling overlay row for this name (left by a DIFFERENT tool that
    # once held it, kept across a plugin uninstall) must never be inherited by the
    # fresh preset, so drop it before the claim. A no-op when no row exists; and if the
    # create below rolls back, the deleted ghost belonged to a vanished tool and needs
    # no restoring. Guarded on the overlay store: with tool_meta OFF the cascade is a
    # no-op rather than a 500 opening an absent Postgres.
    # Pin the fleet BEFORE the first write below: the cascade, the store write and
    # the register are all this door's local apply, and the fan-out censuses only
    # when it publishes, on the far side of every one of them.
    census = await _census_at_start(_RELOAD_OP)

    if component_store_configured(SKELETON_COMPONENT):
        await instance.app.tool_meta.store.delete_meta(name)

    # The pre-checks already ran, so the store write is safe. Persist THEN register;
    # if register fails, roll the store row fully back through the generic HARD
    # delete so no stored-but-unregistered preset survives.
    spec = PresetSpec(name=name, description=description, base_tool=base_tool, fixed_kwargs=fixed_kwargs)
    try:
        record = await instance.app.presets.store.create_preset(
            spec, extensions=extensions, output_schema=output_schema, input_schema=input_schema, tags=tags
        )
    except PresetNameConflictError as exc:
        raise ConflictError(f"preset name {name!r} collides with an existing tool") from exc
    except PresetExistsError as exc:
        raise ConflictError(f"preset {name!r} already exists") from exc
    try:
        await mgr.register(
            name,
            base_tool,
            fixed_kwargs,
            extensions,
            description,
            output_schema,
            input_schema,
            version=record.active_version,
        )
    except Exception as register_exc:
        try:
            await instance.app.versioning.store.delete("preset", name)
        except Exception as delete_exc:
            logger.exception("failed to roll back store row for preset %r after a register failure", name)
            raise delete_exc from register_exc
        # A typed clobber error (the name was taken by a foreign tool or a sibling
        # preset in the window between the pre-checks and the register) maps to 409;
        # any other register failure re-raises loudly.
        if isinstance(register_exc, PresetExistsError):
            raise ConflictError(f"preset {name!r} already exists") from register_exc
        if isinstance(register_exc, PresetNameConflictError):
            raise ConflictError(f"preset name {name!r} collides with an existing tool") from register_exc
        raise register_exc
    await instance.app.emit_list_changed("tool")
    report = await _fanout_reload(name, census)
    return record, report


@operation(
    summary="Create a preset",
    tags=["presets"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, ConflictError, NotSupportedError],
    request_model=PresetCreate,
)
async def create_preset(
    name: str,
    base_tool: str,
    description: str,
    fixed_kwargs: dict[str, Any],
    extensions: list[list[ExtensionElement]],
    output_schema: dict[str, Any] | None,
    input_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a preset, ATOMIC: the shared :func:`_create_preset_core` runs the ordered
    name pre-checks, validation, store write THEN register (rolling the row fully back on
    a register failure), one ``list_changed``, and the bus rebind fan-out. The response
    embeds the per-worker fleet report under ``fanout``.

    A preset's NAME is its identity everywhere — it IS the live tool binding, and every
    reference keys on it deliberately; there are no surrogate ids."""
    record, report = await _create_preset_core(
        name, base_tool, description, fixed_kwargs, extensions, output_schema, input_schema
    )
    # Cross-references from the post-write population — the new body may already
    # compose other presets (``uses``), and a sibling authored against this name is
    # picked up too (``used_by``); one source of truth with the list / get rows.
    bodies = await instance.app.presets.list_active_bodies()
    uses_map, used_by_map = _reference_maps(bodies)
    view = _new_record_view(
        name,
        base_tool,
        description,
        extensions,
        output_schema,
        input_schema,
        active_version=record.active_version,
        uses=uses_map.get(name, []),
        used_by=used_by_map.get(name, []),
    )
    # Embed the per-worker rebind fan-out report (mirrors the template writers): a
    # deployer reads it as the read-your-writes barrier signal — proof the new binding
    # propagated to every serving worker, not only the one that served this call.
    view["fanout"] = fleet_fanout(report)
    return view


# -- get one -----------------------------------------------------------------


@operation(summary="Get a preset", tags=["presets"], errors=[NotFoundError])
async def get_preset(name: str) -> dict[str, Any]:
    """The store record + the active ``fixed_kwargs`` + the ``uses`` / ``used_by``
    cross-references; 404 for an absent name."""
    try:
        record = await instance.app.presets.store.get_preset(name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc
    # Read the active-body population so this row's references compute identically to
    # the list route. A name gone between the record read and here was deleted
    # concurrently — a genuine 404, never a silent KeyError.
    bodies = await instance.app.presets.list_active_bodies()
    if name not in bodies:
        raise NotFoundError(f"preset {name!r} not found")
    body = bodies[name]
    uses_map, used_by_map = _reference_maps(bodies)
    view = _store_record_view(name, record.active_version, body, uses=uses_map[name], used_by=used_by_map[name])
    view["fixed_kwargs"] = body.fixed_kwargs
    return view


# -- versions ----------------------------------------------------------------


@operation(summary="List a preset's versions", tags=["presets"], errors=[NotFoundError])
async def list_versions(name: str) -> list[dict[str, Any]]:
    """The full version history for a preset; 404 for an absent name."""
    try:
        versions = await instance.app.presets.store.list_versions(name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc
    return [v.model_dump() for v in versions]


@operation(summary="Get a specific preset version", tags=["presets"], errors=[BadRequestError, NotFoundError])
async def get_version(name: str, version: str) -> dict[str, Any]:
    """One version of a preset by its integer version number; a non-integer segment
    is a 400 and an unknown version a 404."""
    try:
        version_num = int(version)
    except ValueError as exc:
        raise BadRequestError("version must be an integer") from exc
    try:
        row = await instance.app.presets.store.get_version(name, version_num)
    except PresetVersionNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} has no version {version_num}") from exc
    return row.model_dump()


async def _save_version_core(
    name: str,
    *,
    fixed_kwargs: dict[str, Any] | None,
    extensions: list[list[ExtensionElement]] | None,
    output_schema: dict[str, Any] | None,
    output_schema_provided: bool,
    description: str | None,
    input_schema: dict[str, Any] | CarryForward | None = CARRY_FORWARD,
    tags: list[str] | None = None,
    enforce_tier: bool = True,
) -> tuple[Any, FleetResult]:
    """The reusable save-a-new-version path shared by the save door and the seed applier:
    read the active body, resolve the carry-forward sentinels, run create's validation
    over the effective new body, (optionally) fence on the base tool's authoring tier,
    save THEN reload (re-pointing the active version back on a residual register failure),
    guard the ``list_changed`` emit on a real wire/extension change, and fan the rebind
    out. Returns the new version row + the per-worker fleet report.

    ``input_schema`` carries forward by default (the save door's behavior); the seed
    applier passes an explicit schema so an upgraded shipped default sets it. ``tags`` labels
    the new version in the SAME save commit (``None`` is the door's untagged save); the seed
    applier passes the shipped-default tag so the save-then-tag has no untagged window.
    ``enforce_tier`` runs the caller-authorization fence; the seed applier passes ``False`` —
    no caller."""
    store = instance.app.presets.store
    if instance.app.preset_manager.is_quarantined(name):
        raise ConflictError(f"preset {name!r} is conflicted and is delete-only")

    # Read the active record + body BEFORE any write: the carried-forward base tool
    # + description come from it, the prior active version is captured for the
    # residual-failure re-point, and it gives the 404 for an absent name.
    try:
        prior_record = await store.get_preset(name)
        active = await store.get_active_body(name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc

    # The EFFECTIVE new body under the carry-forward sentinels (omitted → carry the
    # active value; an explicit value — including a clearing ``[]`` — wins). The
    # store applies the same rule on write; these mirror it for pre-write validation.
    base_tool = active.base_tool
    new_fixed_kwargs = active.fixed_kwargs if fixed_kwargs is None else fixed_kwargs
    new_extensions = active.extensions if extensions is None else extensions
    new_output_schema = active.output_schema if not output_schema_provided else output_schema
    # ``input_schema`` carries forward in the STORE (sentinel passed straight through); it
    # enters validation only when EXPLICITLY provided (the seed path), so the save door's
    # carry-forward validation yields ``None``.
    validation_input_schema = None if isinstance(input_schema, CarryForward) else input_schema
    # ``description`` is editable per version under the None-carry sentinel. The
    # resulting value is validated non-empty in the store view (explicit "" or a
    # carry-from-empty both raise); mirror the resolution here to feed the dry-run.
    new_description = active.description if description is None else description

    # Validate-before-commit — run the SAME checks create runs so a bad edit is a
    # 400 that commits nothing (never a version that can never bind, which would
    # brick the preset into delete-only).
    if fixed_kwargs is not None:
        authoring_error = await _agent_authoring_error(base_tool, new_fixed_kwargs)
        if authoring_error is not None:
            raise BadRequestError(authoring_error)
    combo_error = _combo_registry_error(new_extensions)
    if combo_error is not None:
        raise BadRequestError(combo_error)
    schema_error = await _output_schema_error(base_tool, new_output_schema, new_extensions)
    if schema_error is not None:
        raise BadRequestError(schema_error)
    bind_error = await _dry_run_bind_error(
        base_tool,
        new_fixed_kwargs,
        name=name,
        description=new_description,
        output_schema=new_output_schema,
        input_schema=validation_input_schema,
    )
    if bind_error is not None:
        raise BadRequestError(bind_error)
    new_body = PresetBody(
        base_tool=base_tool,
        description=new_description,
        fixed_kwargs=new_fixed_kwargs,
        extensions=new_extensions,
        output_schema=new_output_schema,
        input_schema=validation_input_schema,
    )
    # An EXPLICITLY provided input_schema over a base tool with no support is a loud
    # error that persists nothing (never a silently-ignored schema); a carried-forward
    # schema was already vetted at its own authoring.
    if not isinstance(input_schema, CarryForward):
        input_schema_error = _input_schema_authoring_error(new_body)
        if input_schema_error is not None:
            raise BadRequestError(input_schema_error)
    write_validator_error = await _write_validator_error(new_body)
    if write_validator_error is not None:
        raise BadRequestError(write_validator_error)
    # Registration-tier fence (ruling 14): the tier is the CURRENT preset's base tool,
    # so editing a fenced base tool's preset is admin-fenced too. Skipped for a platform
    # seed (``enforce_tier=False``) — no caller to fence.
    if enforce_tier:
        await _enforce_registration_tier(base_tool)

    # Snapshot the OLD wire tool + its extension combos BEFORE the store write —
    # reload tears the old tool down, and after the write the active body already
    # holds the new value.
    old_extensions = active.extensions
    old_wire = await _wire_snapshot(name)
    prior_active = prior_record.active_version
    # Same pre-write point, for the same reason: the save+reload below is the local
    # apply the fan-out's own census would be taken after.
    census = await _census_at_start(_RELOAD_OP)

    try:
        row = await store.save_version(
            name,
            fixed_kwargs=fixed_kwargs,
            extensions=extensions,
            output_schema=output_schema if output_schema_provided else CARRY_FORWARD,
            description=description,
            input_schema=input_schema,
            tags=tags,
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc

    # A residual re-register failure (the environment changed after the pre-write
    # validation) re-points the store's active version back to the prior one so the
    # committed row stays as inert history and store + live never diverge, then
    # re-raises loudly; the emit below is never reached, so a failed save fires
    # nothing.
    try:
        await instance.app.preset_manager.reload(name)
    except Exception:
        await store.rollback(name, prior_active)
        raise

    new_actual_extensions = (await store.get_active_body(name)).extensions
    new_wire = await _wire_snapshot(name)
    if old_wire != new_wire or old_extensions != new_actual_extensions:
        await instance.app.emit_list_changed("tool")
    # The rebind fans out regardless of the emit guard: siblings must re-read the
    # active body even when the wire tool is byte-identical (a baked VALUE changed).
    report = await _fanout_reload(name, census)
    return row, report


@operation(
    summary="Save a new preset version",
    tags=["presets"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, ConflictError, NotFoundError],
    request_model=PresetVersionSave,
)
async def save_version(
    name: str,
    fixed_kwargs: dict[str, Any] | None,
    extensions: list[list[ExtensionElement]] | None,
    output_schema: dict[str, Any] | None,
    output_schema_provided: bool,
    description: str | None,
    input_schema: dict[str, Any] | None = None,
    input_schema_provided: bool = False,
) -> dict[str, Any]:
    """Save a new version (carry-forward sentinels on omitted fields) then reload and
    fan out; 409 if the record is conflicted, 404 for an absent name. The
    ``list_changed`` emit is GUARDED on a real change to the serialized wire tool OR
    its extension combos. The response embeds the per-worker fleet report under
    ``fanout``."""
    # ``input_schema`` mirrors ``output_schema``'s presence flag: an ABSENT field carries
    # the active value forward (the ``CARRY_FORWARD`` sentinel the core accepts), a PRESENT
    # one — including an explicit ``null`` that clears — is the deliberate value the store
    # persists.
    row, report = await _save_version_core(
        name,
        fixed_kwargs=fixed_kwargs,
        extensions=extensions,
        output_schema=output_schema,
        output_schema_provided=output_schema_provided,
        description=description,
        input_schema=input_schema if input_schema_provided else CARRY_FORWARD,
    )
    # Embed the per-worker rebind fan-out report (mirrors the template writers): the
    # read-your-writes barrier proving the new version reached every serving worker.
    return {**row.model_dump(), "fanout": fleet_fanout(report)}


# -- rollback ----------------------------------------------------------------


@operation(
    summary="Roll a preset back to a version",
    tags=["presets"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, ConflictError, NotFoundError],
    request_model=PresetRollback,
)
async def rollback_preset(name: str, version: int) -> dict[str, Any]:
    """Re-point the active version then reload and fan out; 409 if the record is
    conflicted, 404 for an absent name or version, 400 if the target version cannot
    bind against the current live registry. The response embeds the per-worker fleet
    report under ``fanout``."""
    store = instance.app.presets.store
    if instance.app.preset_manager.is_quarantined(name):
        raise ConflictError(f"preset {name!r} is conflicted and is delete-only")

    try:
        prior_record = await store.get_preset(name)
        old_extensions = (await store.get_active_body(name)).extensions
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc
    old_wire = await _wire_snapshot(name)

    # Rollback has NO carry-forward: read the TARGET version body and validate THAT
    # against the CURRENT live registry (a base tool or extension it named may have
    # been removed since the version was authored), so a rollback to an unbindable
    # version is a 400 that commits nothing rather than a bricking re-point.
    try:
        target = await store.get_version(name, version)
    except PresetVersionNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} has no version {version}") from exc
    target_body = PresetBody.model_validate(target.body)
    combo_error = _combo_registry_error(target_body.extensions)
    if combo_error is not None:
        raise BadRequestError(combo_error)
    schema_error = await _output_schema_error(target_body.base_tool, target_body.output_schema, target_body.extensions)
    if schema_error is not None:
        raise BadRequestError(schema_error)
    bind_error = await _dry_run_bind_error(
        target_body.base_tool,
        target_body.fixed_kwargs,
        name=name,
        description=target_body.description,
        output_schema=target_body.output_schema,
        input_schema=target_body.input_schema,
    )
    if bind_error is not None:
        raise BadRequestError(bind_error)
    write_validator_error = await _write_validator_error(target_body)
    if write_validator_error is not None:
        raise BadRequestError(write_validator_error)
    # Registration-tier fence (ruling 14): the tier is the target body's base tool.
    await _enforce_registration_tier(target_body.base_tool)

    prior_active = prior_record.active_version
    # Pinned before the rollback+reload local apply — see :func:`_census_at_start`.
    census = await _census_at_start(_RELOAD_OP)
    record = await store.rollback(name, version)

    # Residual re-register failure: re-point the active version back to the prior
    # one so store + live never diverge, then re-raise loudly.
    try:
        await instance.app.preset_manager.reload(name)
    except Exception:
        await store.rollback(name, prior_active)
        raise

    new_extensions = (await store.get_active_body(name)).extensions
    new_wire = await _wire_snapshot(name)
    if old_wire != new_wire or old_extensions != new_extensions:
        await instance.app.emit_list_changed("tool")
    # The rebind fans out regardless of the emit guard: siblings must re-read the
    # active body even when the wire tool is byte-identical (a baked VALUE changed).
    report = await _fanout_reload(name, census)
    # Embed the per-worker rebind fan-out report (mirrors the template writers): the
    # read-your-writes barrier proving the rolled-back version reached every worker.
    return {"name": name, "active_version": record.active_version, "fanout": fleet_fanout(report)}


# -- rename ------------------------------------------------------------------


@operation(
    summary="Rename a preset",
    tags=["presets"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, ConflictError, NotFoundError],
    request_model=PresetRename,
)
async def rename_preset(name: str, new_name: str) -> dict[str, Any]:
    """Rename a preset, ATOMIC (a preset's name IS its live tool name). Runs create's
    ordered name pre-checks on the NEW name, BLOCKS with a 409 listing every referee
    if any live reference composes the current name, binds the new tool BEFORE tearing
    the old one down, fires one ``list_changed``, and fans the rebind out NEW-first
    then the old removal. The response embeds the primary rebind (new-name) fan-out
    report under ``fanout``; the old-name removal stays log-only.

    A preset's NAME is its identity everywhere — it IS the live tool binding, and every
    reference (preset bodies, platform wiring, plugin holders) keys on it deliberately;
    rename integrity is enforced at THIS gate, and there are no surrogate ids."""
    # The new name is a live tool name + a ``{name}`` route segment, so it must be
    # tool-name-safe — the same rule create enforces.
    if not is_valid_preset_name(new_name):
        raise BadRequestError(f"invalid preset name {new_name!r}: must match ^[A-Za-z0-9_-]{{1,64}}$")
    # A no-op rename is a caller error, surfaced loudly — never a silent 200.
    if new_name == name:
        raise BadRequestError("new name must differ from the current name")

    mgr = instance.app.preset_manager
    # A conflicted record was never registered and its name may be owned by a foreign
    # tool — rename must not touch it, nor launder a quarantined record into a clean
    # name (the delete-only stance save/rollback take).
    if mgr.is_quarantined(name):
        raise ConflictError(f"preset {name!r} is conflicted and is delete-only")

    # A store-less deploy holds no preset, so an unquarantined name is a genuine 404
    # without a Postgres open (delete's reasoning).
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotFoundError(f"preset {name!r} not found")

    store = instance.app.presets.store
    try:
        await store.get_preset(name)
        active_body = await store.get_active_body(name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc
    # Registration-tier fence (ruling 14): the tier is the CURRENT preset's base tool, so
    # renaming a fenced base tool's preset is admin-fenced too. Runs BEFORE the store move.
    await _enforce_registration_tier(active_body.base_tool)

    # NEW-name pre-checks — create's exact order and codes: quarantine 409 → live-tool
    # collision 409 → agent tool-name collision 400 → duplicate-preset 409.
    if mgr.is_quarantined(new_name):
        raise ConflictError(f"a quarantined preset {new_name!r} exists — delete the quarantined record first")
    if await mgr.name_conflicts(new_name):
        raise ConflictError(f"preset name {new_name!r} collides with an existing tool")
    if new_name in _agent_tool_names():
        raise BadRequestError(f"preset name {new_name!r} collides with an agent tool name")
    if mgr.is_registered(new_name):
        raise ConflictError(f"preset {new_name!r} already exists")

    # Referential integrity: BLOCK (never silently cascade-rewrite) a rename that would
    # strand any live reference — the FULL union of preset-body referees + every
    # registered referee (platform wiring: schedules/hooks/routes/extensions/parks, and
    # plugin holders), listing every holder so the operator updates them first. A referee
    # raising fails the rename loudly (no silent bypass).
    referees = await _rename_referees(name)
    if referees:
        raise ConflictError(
            f"preset {name!r} cannot be renamed — it is still referenced by: {referees}; update those references first"
        )

    # Move the store key. The pre-checks make the typed conflicts race-window catches,
    # mapped exactly as create maps its post-write errors.
    # The pre-rename snapshot judges the RELOAD, and is the floor for the remove:
    # both halves are one rename, so a worker owed the rename from the start stays
    # owed it at the life it had then. The remove's set is WIDER — see the union
    # assembled after the reload below.
    census = await _census_at_start(_RELOAD_OP)

    try:
        record = await store.rename_preset(name, new_name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc
    except PresetExistsError as exc:
        raise ConflictError(f"preset {new_name!r} already exists") from exc
    except PresetNameConflictError as exc:
        raise ConflictError(f"preset name {new_name!r} collides with an existing tool") from exc

    # Local apply, NEW FIRST: bind ``new_name`` from the moved store row's active body.
    # Reload-before-remove keeps every call resolvable — during the window BOTH names
    # are bound and the old binding still works (its baked spec is in-memory, its base
    # tool untouched). On a re-register failure, compensate by re-pointing the store
    # back so store + live never diverge, then surface loudly; the OLD binding was
    # never touched, so the preset stays fully live under its old name.
    try:
        await mgr.reload(new_name)
    except Exception as reload_exc:
        try:
            await store.rename_preset(new_name, name)
        except Exception as compensate_exc:
            logger.exception("failed to re-point store row for preset %r after a rename re-register failure", name)
            raise compensate_exc from reload_exc
        if isinstance(reload_exc, PresetExistsError):
            raise ConflictError(f"preset {new_name!r} already exists") from reload_exc
        if isinstance(reload_exc, PresetNameConflictError):
            raise ConflictError(f"preset name {new_name!r} collides with an existing tool") from reload_exc
        raise reload_exc

    # Then tear the OLD binding down. A failure here leaves BOTH names bound (old is
    # stale-but-functional: its baked spec is in-memory and its base tool is untouched);
    # it re-raises loudly and ``reload_config`` is the documented recovery (rehydration
    # rebuilds from the store, which now knows only ``new_name``). The store move is
    # never unwound here — ``new_name`` is live and correct.
    await mgr.remove(name)

    # Re-key the tool_meta overlay AFTER the versioned rename's rollback window has
    # closed (after ``mgr.remove`` — the point past which the rename is never
    # unwound). An overlay re-keyed earlier would be stranded under the new name if a
    # reload failure rolled the versioned store back to the old name. The re-key
    # atomically drops any pre-existing ``new_name`` overlay row before moving the old
    # one (clean slate); a failure here raises loudly, leaving only a dangling old-name
    # row that the next claim reclaims. Guarded on the overlay store: with tool_meta
    # OFF the re-key is a no-op rather than a 500 opening an absent Postgres.
    if component_store_configured(SKELETON_COMPONENT):
        await instance.app.tool_meta.store.rename_tool(name, new_name)

    # A rename changes the tool listing by definition (old gone, new present), so the
    # emit is unconditional — no wire-diff guard.
    await instance.app.emit_list_changed("tool")
    # Fan out NEW FIRST — reload ``new_name`` on every worker BEFORE removing ``old``
    # (both briefly alive beats neither): the two are sequentially awaited confirmed
    # broadcasts, so every worker applies the reload before any is asked to remove.
    reload_report = await _fanout_reload(new_name, census)
    addressed = _addressed_siblings(reload_report)
    # Every worker the reload actually reached is now bound to ``new_name`` and must
    # also be told to drop the old one — including a worker that joined after the
    # snapshot (it rehydrated the OLD name before the commit and announced itself
    # after, so it holds a binding the store no longer knows).
    await _fanout_remove(name, _union_census(census, addressed, await _census_at_start(_REMOVE_OP)))
    # Embed the primary rebind fan-out report — the propagation of the NEW binding, the
    # read-your-writes barrier a deployer checks. The old-name removal is teardown and
    # stays log-only (a single ``fanout`` field mirrors the template writers exactly; a
    # rename does not invent a two-report shape).
    return {
        "name": new_name,
        "renamed_from": name,
        "active_version": record.active_version,
        "fanout": fleet_fanout(reload_report),
    }


# -- delete ------------------------------------------------------------------


@operation(
    summary="Delete a preset",
    tags=["presets"],
    reload_gated=True,
    errors=[NotFoundError],
)
async def delete_preset(name: str) -> dict[str, Any]:
    """Delete a preset. A non-conflicted record is soft-deleted and its base + branch
    tools torn down (one ``list_changed``); a conflicted record is removed store-side
    ONLY (HARD delete + drop the quarantine entry), touching no registration and
    firing no emit. Both branches fan the removal out on the bus and embed the
    per-worker fleet report under ``fanout``."""
    mgr = instance.app.preset_manager

    if mgr.is_quarantined(name):
        # A conflicted record was never registered — remove ONLY the stored
        # document (HARD delete, so no ghost/version history lingers), drop the
        # quarantine entry immediately, and touch NO registration (the name may be
        # owned by a foreign tool) and fire NO emit. The removal still fans out so a
        # sibling's in-memory quarantine entry is cleared too. A quarantine entry only
        # ever arises from a store-backed preset (rehydrate or reconcile of a created
        # preset — both require a configured store), so the hard-delete here always has
        # a store to talk to; the store-config guard below is only for the
        # non-quarantined path.
        census = await _census_at_start(_REMOVE_OP)
        try:
            await instance.app.versioning.store.delete("preset", name)
        except Exception:
            logger.exception("failed to hard-delete conflicted preset record %r", name)
            raise
        # Cascade the overlay row: the tool is gone, so its organizational
        # metadata goes with it. A no-op when the preset never got a row, and skipped
        # entirely when the overlay store is OFF (no absent-Postgres open).
        if component_store_configured(SKELETON_COMPONENT):
            await instance.app.tool_meta.store.delete_meta(name)
        mgr.drop_quarantine(name)
        report = await _fanout_remove(name, census)
        # Embed the per-worker removal fan-out report (mirrors the template writers): a
        # deployer reads it as proof the teardown reached every serving worker.
        return {"name": name, "deleted": True, "fanout": fleet_fanout(report)}

    # A store-less deploy (no versioned store configured) can hold no preset, so a
    # name that is not quarantined is a genuine 404 without a Postgres read.
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotFoundError(f"preset {name!r} not found")
    census = await _census_at_start(_REMOVE_OP)
    try:
        await instance.app.presets.store.soft_delete(name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc
    await mgr.remove(name)
    # Cascade the overlay row once the preset is soft-deleted and torn down.
    # Keyed by tool name, so the soft-delete ghost in ``versioned_documents`` is
    # irrelevant; a no-op when the preset never got a row, and skipped entirely when
    # the overlay store is OFF (no absent-Postgres open).
    if component_store_configured(SKELETON_COMPONENT):
        await instance.app.tool_meta.store.delete_meta(name)
    await instance.app.emit_list_changed("tool")
    report = await _fanout_remove(name, census)
    # Embed the per-worker removal fan-out report (mirrors the template writers): a
    # deployer reads it as proof the teardown reached every serving worker.
    return {"name": name, "deleted": True, "fanout": fleet_fanout(report)}


# -- referees ----------------------------------------------------------------


@operation(summary="List live references to this preset", tags=["presets"], errors=[NotFoundError])
async def preset_referees(name: str) -> dict[str, Any]:
    """Every live reference a rename of this preset would strand — the SAME full union
    the rename door blocks on: the OTHER presets whose active body composes it, plus every
    registered referee (platform wiring — schedules/hooks/routes/extensions/parks — and
    plugin holders). Exposed so the UI can preflight a rename. 404 for an unknown preset,
    the same existence check the rename door runs first; a referee raising propagates
    loudly, exactly as at the rename gate."""
    # A store-less deploy holds no preset, so an unknown name is a genuine 404
    # without a Postgres open (the rename/delete doors' reasoning).
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotFoundError(f"preset {name!r} not found")
    try:
        await instance.app.presets.store.get_preset(name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc
    referees = await _rename_referees(name)
    return {"name": name, "referees": referees}


# -- validate (dry-run) ------------------------------------------------------


def _verdict(error: str | None) -> dict[str, Any]:
    """A validation verdict — ``valid`` is the absence of an ``error``. The op
    returns 200 for BOTH outcomes: an invalid draft is a SUCCESSFUL validation, not
    a request failure."""
    return {"valid": error is None, "error": error}


async def _validate_create(
    name: str,
    base_tool: str,
    description: str | None,
    fixed_kwargs: dict[str, Any],
    extensions: list[list[ExtensionElement]],
    output_schema: dict[str, Any] | None,
    input_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The create route's full pre-store verdict for a brand-new preset — the exact
    ordered checks create runs before its store write (name safety → description
    non-empty → quarantine → tool collision → agent-name collision → duplicate →
    base rules → agent authoring), then combo → schema → dry-run bake — as a
    ``valid``/``error`` verdict rather than a write. A draft may omit ``description``
    (``None``): the emptiness gate applies only to an explicitly provided value, so an
    unfilled draft validates its structure and defers the required-description rule to
    the real create's edge."""
    if not is_valid_preset_name(name):
        return _verdict(f"invalid preset name {name!r}: must match ^[A-Za-z0-9_-]{{1,64}}$")
    if description is not None and not description.strip():
        return _verdict("a preset description must not be empty")
    mgr = instance.app.preset_manager
    if mgr.is_quarantined(name):
        return _verdict(f"a quarantined preset {name!r} exists — delete the quarantined record first")
    if await mgr.name_conflicts(name):
        return _verdict(f"preset name {name!r} collides with an existing tool")
    if name in _agent_tool_names():
        return _verdict(f"preset name {name!r} collides with an agent tool name")
    if mgr.is_registered(name):
        return _verdict(f"preset {name!r} already exists")
    if base_tool in mgr.registered_names():
        return _verdict(f"base tool {base_tool!r} is itself a preset")
    if base_tool not in await instance.app.tools.get_tools():
        return _verdict(f"base tool {base_tool!r} is not a registered tool")
    authoring_error = await _agent_authoring_error(base_tool, fixed_kwargs)
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
    )


async def _verdict_bind_chain(
    base_tool: str,
    fixed_kwargs: dict[str, Any],
    *,
    name: str,
    description: str,
    output_schema: dict[str, Any] | None,
    input_schema: dict[str, Any] | None = None,
    extensions: list[list[ExtensionElement]] | None = None,
) -> dict[str, Any]:
    """The shared tail both modes run: combo registry → output schema → dry-run
    bake → input-schema support → write validator, as a verdict — the SAME chain the
    real create/save doors run, so the dry run never reports valid on a draft the
    write door would 400. ``extensions`` defaults to no combos for the bind chain's
    combo/schema checks."""
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
    return _verdict(None)


@operation(
    summary="Validate a preset draft (dry-run)",
    tags=["presets"],
    errors=[BadRequestError, NotSupportedError],
    request_model=PresetValidate,
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
) -> dict[str, Any]:
    """Report whether a preset draft would be accepted, running the SAME pre-store
    verdict the corresponding write route would — CREATE mode when no preset named
    ``name`` exists, VERSION mode when one does (mode-resolved by a store lookup).
    Both verdicts return 200; only a malformed body is a 400."""
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
        return await _validate_create(
            name,
            base_tool,
            description,
            fixed_kwargs or {},
            extensions,
            output_schema,
            input_schema,
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

    new_fixed_kwargs = active.fixed_kwargs if fixed_kwargs is None else fixed_kwargs
    # An authored-agent (``fixed_kwargs``) edit runs the full authoring validation
    # over the carried-forward base tool, exactly as save-version does — only when
    # fixed_kwargs was provided.
    if fixed_kwargs is not None:
        authoring_error = await _agent_authoring_error(active.base_tool, new_fixed_kwargs)
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
    )


# -- version tags ------------------------------------------------------------


@operation(
    summary="Set a preset version's tags",
    tags=["presets"],
    destructive=True,
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    request_model=PresetVersionTags,
)
async def set_preset_version_tags(name: str, version: str, tags: list[str]) -> dict[str, Any]:
    """Replace one version's ``tags`` annotation. Tags are labels on an immutable
    version body, so this edits only the annotation and never rebinds the live tool
    (no reload / no fan-out). 404 for an unknown preset or version; a 501
    ``NotSupportedError`` (versioning-not-configured) on a store-less deploy, exactly as
    the create route refuses."""
    try:
        version_num = int(version)
    except ValueError as exc:
        raise BadRequestError("version must be an integer") from exc

    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})

    try:
        await instance.app.presets.set_version_tags(name, version_num, tags)
    except DocumentVersionNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} has no version {version_num}") from exc
    return {"name": name, "version": version_num, "tags": tags}


# -- declared preset seeds ---------------------------------------------------

# The version tag a shipped default carries. A seed OWNS a version only while it wears
# this tag; the moment an operator saves an untagged version the preset is user-edited
# and the applier never touches it again.
_SHIPPED_DEFAULT_TAG = "shipped-default"


def _operator_edited(active_tags: Sequence[str]) -> bool:
    """The applier's single untouched-vs-edited policy, over the ACTIVE version's tags.
    Every version the applier writes carries ``shipped-default`` in the SAME commit (no
    untagged window can exist) and the operator save door tags nothing, so an untagged
    active version is unambiguously operator-authored — the applier never touches the
    preset again (no upgrade, no retirement delete)."""
    return _SHIPPED_DEFAULT_TAG not in active_tags


def _canonical(value: Any) -> str:
    """A stable JSON rendering for the normalized seed-vs-body content compare — key order
    and container identity never register as a difference."""
    return json.dumps(value, sort_keys=True, default=str)


def _seed_matches(seed: PresetSeed, body: PresetBody) -> bool:
    """Whether the live active body already equals the seed's declared content — the
    normalized compare of base_tool + description + fixed_kwargs + input/output schemas.
    ``base_tool`` is core content: a shipped default that re-points its binding across
    releases must register as drift so the new binding ships. Extensions are not part of a
    seed, so they are not compared."""
    return (
        seed.base_tool == body.base_tool
        and seed.description == body.description
        and _canonical(seed.fixed_kwargs) == _canonical(body.fixed_kwargs)
        and _canonical(seed.input_schema) == _canonical(body.input_schema)
        and _canonical(seed.output_schema) == _canonical(body.output_schema)
    )


async def _apply_seed_tool_meta(seed: PresetSeed) -> None:
    """Apply the seed's tool_meta display fields ONLY where the preset's tool_meta leaves
    them absent — a seed never overwrites an operator-set display value. ``folder_path`` is
    resolved to a leaf ``folder_id`` (creating missing folders), never a raw path string. A
    no-op when the seed declares no tool_meta."""
    meta = seed.tool_meta
    if meta is None:
        return
    from tai42_skeleton.operations.tool_meta import _clean_label, resolve_folder_path

    store = instance.app.tool_meta.store
    current = await store.get_meta(seed.name)
    patch: dict[str, Any] = {}
    if meta.display_name is not None and (current is None or current.display_name is None):
        # Route through the operation-door guard: a blank/whitespace display_name is refused
        # LOUDLY (not persisted as an empty label), and an accepted value is stored stripped —
        # the same invariant ``upsert_tool_meta`` enforces so ``display_name ?? name`` never
        # renders empty.
        patch["display_name"] = _clean_label(meta.display_name, "display_name")
    if meta.tags is not None and (current is None or not current.tags):
        patch["tags"] = list(meta.tags)
    if meta.folder_path is not None and (current is None or current.folder_id is None):
        folder_id = await resolve_folder_path(meta.folder_path)
        if folder_id is not None:
            patch["folder_id"] = folder_id
    if patch:
        await store.merge_meta(seed.name, patch=patch)


async def _seed_create(seed: PresetSeed) -> None:
    """Create an absent seed through the shared create core (no caller fence), tagging
    version 1 ``shipped-default`` in the SAME commit, then apply its tool_meta where
    absent. The atomic tag leaves no untagged window, so an interrupted create can never
    strand an untagged version the applier must later repair."""
    await _create_preset_core(
        seed.name,
        seed.base_tool,
        seed.description,
        seed.fixed_kwargs,
        [],
        seed.output_schema,
        seed.input_schema,
        tags=[_SHIPPED_DEFAULT_TAG],
        enforce_tier=False,
    )
    await _apply_seed_tool_meta(seed)


async def _seed_upgrade(seed: PresetSeed, active_body: PresetBody) -> None:
    """Re-ship a shipped default whose content drifted from its live active version, tagging
    the result ``shipped-default``. tool_meta is left untouched — an upgrade re-ships content,
    not display.

    Content drift that leaves ``base_tool`` unchanged saves a new version through the shared
    save core (no caller fence). A drift that RE-POINTS ``base_tool`` cannot go through the
    save core — it carries ``base_tool`` forward from the active version, so a version save
    can never change the binding — so the full new body is written straight to the versioned
    store (the tag applied in the SAME commit), then the live tool is reloaded onto the new
    base and the rebind fans out. History and the tool_meta overlay are preserved (no
    recreate); a reload that cannot bind the new base rolls the store pointer back and raises
    loudly.

    Concurrent-boot dedup (symmetric with the create branch): a sibling worker running the
    same on_startup hook may have re-shipped this drift between the applier's drift check and
    here. Re-read the active body first and no-op if it now matches the seed, so a concurrent
    fleet boot yields ONE upgraded version, not a duplicate per worker — for both branches."""
    if _seed_matches(seed, await instance.app.presets.store.get_active_body(seed.name)):
        return
    if seed.base_tool == active_body.base_tool:
        # Tag the new version in the SAME save commit — no untagged window an
        # interrupt could strand as an "operator-edited" active version the applier
        # then freezes against future upgrades.
        await _save_version_core(
            seed.name,
            fixed_kwargs=seed.fixed_kwargs,
            extensions=[],
            output_schema=seed.output_schema,
            output_schema_provided=True,
            description=seed.description,
            input_schema=seed.input_schema,
            tags=[_SHIPPED_DEFAULT_TAG],
            enforce_tier=False,
        )
        return

    new_body = PresetBody(
        base_tool=seed.base_tool,
        description=seed.description,
        fixed_kwargs=seed.fixed_kwargs,
        extensions=[],
        output_schema=seed.output_schema,
        input_schema=seed.input_schema,
    )
    # The direct store write bypasses the create/save cores, so run their authoring
    # validators FIRST: a re-point onto a base that rejects the carried body (an unbindable
    # base, an unsupported input_schema, a rejecting write validator, an empty description)
    # raises loudly and persists nothing — never served unvalidated.
    await _validate_authoring(seed.name, new_body)
    generic = instance.app.versioning.store
    prior_active = (await generic.get("preset", seed.name)).active_version
    census = await _census_at_start(_RELOAD_OP)
    await generic.save_version("preset", seed.name, new_body.model_dump(), tags=[_SHIPPED_DEFAULT_TAG])
    try:
        await instance.app.preset_manager.reload(seed.name)
    except Exception:
        await generic.rollback("preset", seed.name, prior_active)
        raise
    await instance.app.emit_list_changed("tool")
    await _fanout_reload(seed.name, census)


async def _apply_one_seed(seed: PresetSeed) -> None:
    """The per-seed policy, idempotent across boot/reload/epoch-swap:

    * absent → create + tag + tool_meta;
    * present, active version tagged ``shipped-default``, content matches → no-op;
    * present, active tagged ``shipped-default``, content matches → no-op;
    * present, active tagged ``shipped-default``, content drifted → re-ship a new tagged
      version (a base_tool re-point re-binds the live tool, other drift saves a version);
    * present, active version UNTAGGED → operator-edited, never touched, a VISIBLE skip line
      names the seed and why. The create tags version 1 atomically (no untagged window can
      exist), so an untagged active version is unambiguously operator-authored;
    * any real failure raises loudly at the lifecycle hook.

    Every non-raising branch ends through the local-load guard below, so this worker leaves
    first boot with the seed's ACTIVE stored version bound in its own registry."""
    store = instance.app.presets.store
    try:
        await store.get_preset(seed.name)
    except PresetNotFoundError:
        # A sibling worker booting in parallel may create the same seed between this check
        # and the create. On a conflict, re-read the STORE (not the local registry, which
        # lags the sibling's fan-out): a preset row now present is that sibling's create —
        # benign, idempotent. A name colliding with something that is NOT a preset row is a
        # genuine foreign-name collision and re-raises loudly.
        try:
            await _seed_create(seed)
        except ConflictError:
            present = True
            try:
                await store.get_preset(seed.name)
            except PresetNotFoundError:
                present = False
            if not present:
                raise
            logger.info("preset seeds: %r created concurrently by a sibling — treating as present", seed.name)
            # Self-heal placement: the sibling committed the preset ROW then may have crashed
            # (or still be) before its non-atomic tool_meta step — re-apply here so this worker
            # restores the seed's folder/tags rather than leaving the concurrently-created
            # preset stranded without placement.
            await _apply_seed_tool_meta(seed)
    else:
        versions = await store.list_versions(seed.name)
        active = next(version for version in versions if version.is_current)
        active_body = PresetBody.model_validate(active.body)
        if _operator_edited(active.tags):
            # Operator-authored (see ``_operator_edited``) — never re-shipped, a VISIBLE
            # skip names the seed and why.
            logger.info(
                "preset seeds: leaving %r untouched — its active version %d is operator-edited (not %s-tagged)",
                seed.name,
                active.version,
                _SHIPPED_DEFAULT_TAG,
            )
        elif not _seed_matches(seed, active_body):
            await _seed_upgrade(seed, active_body)
        # Self-heal placement on the present branch. ``_seed_create`` commits the preset row +
        # tag FIRST and applies tool_meta AFTER (a non-atomic window): a worker that crashed in
        # between — or a race that stranded folder creation — leaves a preset present WITHOUT
        # its seeded folder/``palette-node`` placement, and no versioned branch above repairs
        # it. Re-apply the seed's tool_meta here so any boot/config-reload restores placement.
        # The guards are fill-only (write display_name/tags/folder_id ONLY where absent): an
        # operator-CHANGED value survives untouched. Fill-only cannot distinguish a
        # crash-stranded NULL from a field the operator deliberately CLEARED, so a cleared
        # tags/folder is re-filled by the seed on the next boot.
        await _apply_seed_tool_meta(seed)

    # Local-load guard on every non-raising branch. A sibling's boot create/upgrade lands
    # store-side only — it does not fan out to this worker at boot — so a seed present in
    # the store may be absent from THIS worker's registry (the sibling-dedup and up-to-date
    # / operator-edited branches both leave it so). Bind the ACTIVE stored version here so
    # every declared seed is callable on first boot, before any reload — never a per-worker
    # subset. A create/upgrade already registered locally, so the guard no-ops; reload only
    # loads the ACTIVE version (never writes one), so an operator edit is preserved and a
    # reboot stays a no-op; it tears down first, so a name it reaches binds exactly once. A
    # quarantined seed stays conflicted — never force-loaded onto a base it cannot bind.
    mgr = instance.app.preset_manager
    if not mgr.is_registered(seed.name) and not mgr.is_quarantined(seed.name):
        await mgr.reload(seed.name)


async def _retire_one_seed(name: str) -> None:
    """Retire a WITHDRAWN shipped default — delete the deployed record while the seed
    still owns it, so a plugin that stops shipping a seed does not leave the stale
    record visible forever. Ownership is the applier's single untouched-vs-edited
    policy (:func:`_operator_edited`): only a record whose active version still wears
    the ``shipped-default`` tag is deleted, and it goes through the ordinary delete
    door — the registration teardown, tool_meta-overlay cascade, and fleet fan-out are
    exactly a manual delete's (a quarantined record takes that door's hard-delete
    branch). An operator-edited record — or an operator-created preset that merely
    shares the retired name — is left in place with a VISIBLE skip line; an absent
    name is a no-op, so a re-run is idempotent.

    Concurrent-boot dedup (symmetric with the create path's conflict absorption): a
    sibling worker running the same startup hook may win the delete between this
    worker's ownership check and its own delete door, leaving the door a 404 — the
    record is already gone, which is the outcome this worker wanted, so it is absorbed
    as benign rather than crashing the boot lifecycle."""
    try:
        versions = await instance.app.presets.store.list_versions(name)
    except PresetNotFoundError:
        return
    active = next(version for version in versions if version.is_current)
    if _operator_edited(active.tags):
        logger.info(
            "preset seeds: leaving retired %r in place — its active version %d is operator-edited (not %s-tagged)",
            name,
            active.version,
            _SHIPPED_DEFAULT_TAG,
        )
        return
    try:
        await delete_preset(name)
    except NotFoundError:
        logger.info("preset seeds: retired %r deleted concurrently by a sibling — treating as retired", name)
        return
    logger.info("preset seeds: retired %r — its shipped-default record was deleted", name)


async def apply_preset_seeds() -> None:
    """Startup/reload/epoch-swap handler: apply every declared preset seed, then every
    declared seed retirement.

    Registered AFTER the preset-rehydrate handler so a just-created seed is LIVE in the
    same epoch (resolvable in the tool registry) — it creates through the operations-layer
    internal path, which registers the tool — and a retired record is rehydrated/registered
    before its delete tears it down. Feature-OFF is legal: with the versioned store
    unconfigured every seed and retirement logs a VISIBLE skip and nothing is touched.
    Any real failure raises loudly. Idempotent across re-runs."""
    seeds = instance.app.presets.seeds()
    retired = instance.app.presets.retired_seeds()
    if not seeds and not retired:
        return
    if not component_store_configured(SKELETON_COMPONENT):
        for seed in seeds:
            logger.info("preset seeds: skipping seed %r — the versioned-document store is not configured", seed.name)
        for name in retired:
            logger.info(
                "preset seeds: skipping retirement of %r — the versioned-document store is not configured", name
            )
        return
    for seed in seeds:
        await _apply_one_seed(seed)
    for name in retired:
        await _retire_one_seed(name)
