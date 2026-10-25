"""The preset reference graph and the agent-name space.

The one walk over a kwargs blob (every name it composes, as registry references), the
uses / used_by maps every stored walk builds from it, and the routes bound to a preset.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from pydantic import TypeAdapter, ValidationError
from tai42_contract.agent.base import PresetSpec
from tai42_contract.presets import PresetBody
from tai42_contract.template import TemplatedText
from tai42_contract.tools import ComposedToolRef, TemplateRef

from tai42_skeleton.app import instance
from tai42_skeleton.operations.errors import BadRequestError, OperationFailedError
from tai42_skeleton.operations.reference_check import RegistryReference, as_template_ref, assert_references_resolve
from tai42_skeleton.tools.template_slots import annotated_template_ids

logger = logging.getLogger(__name__)

# A walk that descends deeper than this refuses the blob: an in-process blob can hold a cycle.
MAX_REFERENCE_DEPTH = 32

# The ``code`` a stored walk's 500 carries when an active preset body cannot be walked.
STORED_PRESET_UNREADABLE = "stored-preset-unreadable"

_INLINE_BASE_KIND = re.compile(r"^(?P<place>.+\.presets\[\d+\]) base_tool$")


class ReferenceShapeError(ValueError):
    """A kwargs blob is off the shape its target reads names from; the text is value-free."""


class ExtractorFaultError(RuntimeError):
    """A base tool's ``tool_refs`` or ``template_refs`` extractor broke its contract: a plugin fault."""


def _agent_tool_names() -> set[str]:
    """Every registered agent's declared ``tool_name``.

    A ``tool_name`` can differ from the decorator-registration name: the run tool
    binds under the REGISTRATION name (so that name is already a live tool the
    ``name_conflicts`` guard catches), but the ``tool_name`` may not be a bound tool.
    Keeping the authored-agent name off BOTH sets keeps one unambiguous agent-name
    space.
    """
    return {agent.tool_name for agent in instance.app.agents.all_agents().values()}


def base_reference(base_tool: str) -> RegistryReference:
    """A preset's base tool as a reference the check resolves."""
    return RegistryReference("base tool", base_tool)


_SCHEMA_SLOT: TypeAdapter[Any] = TypeAdapter(PresetBody.model_fields["output_schema"].annotation)


def schema_references(output_schema: Any, input_schema: Any) -> list[RegistryReference]:
    """A preset's ``output_schema`` and ``input_schema`` stored ids, each at its own field.

    Each value is read as the preset body reads it, so ``{"id": ...}`` names a stored template.
    """
    refs: list[RegistryReference] = []
    for site, value in (("output_schema", output_schema), ("input_schema", input_schema)):
        schema = _SCHEMA_SLOT.validate_python(value)
        if isinstance(schema, TemplatedText) and schema.id is not None:
            refs.append(RegistryReference("stored id", schema.id, "template", site))
    return refs


def body_head_references(body: PresetBody) -> list[RegistryReference]:
    """A preset body's base tool and its schema ids: the references checked before any render."""
    return [base_reference(body.base_tool), *schema_references(body.output_schema, body.input_schema)]


def settled_target(name: str) -> str | None:
    """The tool a blob handed to ``name`` is read over, or ``None`` when no leg reads it.

    A registered preset settles through its base, as the run does, until a name that is
    not a preset (an agent, a base tool, an extension branch tool, or an unserved name the
    caller's own reference refuses). A preset with an authored input settles on ``None``:
    the run validates the blob against the authored schema and the preset routes it, never
    handing it to its base as given.
    """
    presets = instance.app.preset_manager
    seen: list[str] = []
    current = name
    while presets.is_registered(current):
        if current in seen:
            raise ReferenceShapeError(f"preset {name!r} settles through a cycle: {' > '.join([*seen, current])}")
        seen.append(current)
        spec = presets.get_spec(current)
        if spec.input_schema is not None:
            return None
        current = spec.base_tool
    return current


def _at(site: str | None, text: str) -> str:
    return f"at {site}: {text}" if site is not None else text


def _joined(*parts: str | None) -> str | None:
    present = [part for part in parts if part]
    return " > ".join(present) if present else None


def _too_deep(where: str, site: str | None) -> ReferenceShapeError:
    return ReferenceShapeError(_at(site, f"{where} nests more than {MAX_REFERENCE_DEPTH} levels deep"))


def _spec_error_text(exc: ValidationError) -> str:
    """Pydantic's errors without the input values, ``<loc>: <msg>`` joined by ``; ``."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors(include_url=False, include_input=False)
    )


def _tool_names_references(node: dict[str, Any], where: str, site: str | None) -> list[RegistryReference]:
    tool_names = node.get("tool_names", [])
    if not isinstance(tool_names, list):
        raise ReferenceShapeError(_at(site, f"{where}.tool_names must be a list of tool names"))
    refs: list[RegistryReference] = []
    for tool_name in tool_names:
        if not isinstance(tool_name, str):
            raise ReferenceShapeError(_at(site, f"{where}.tool_names entries must be strings"))
        refs.append(RegistryReference(f"{where}.tool_names entry", tool_name, "tool", site))
    return refs


def _inline_preset_references(
    node: dict[str, Any], where: str, site: str | None, inline: list[tuple[str, PresetSpec]]
) -> list[RegistryReference]:
    presets = node.get("presets", [])
    if not isinstance(presets, list):
        raise ReferenceShapeError(_at(site, f"{where}.presets must be a list of preset specs"))
    refs: list[RegistryReference] = []
    for i, entry in enumerate(presets):
        place = f"{where}.presets[{i}]"
        try:
            preset = PresetSpec.model_validate(entry)
        except ValidationError as exc:
            raise ReferenceShapeError(
                _at(site, f"{place} is not a valid preset spec: {_spec_error_text(exc)}")
            ) from exc
        # An inline preset's description is the docstring of the tool it binds, read by the model.
        if not preset.description.strip():
            raise ReferenceShapeError(_at(site, f"{place} description must not be empty"))
        refs.append(RegistryReference(f"{place} base_tool", preset.base_tool, "tool", site))
        inline.append((place, preset))
    return refs


def _agent_spec_references(
    node: dict[str, Any],
    where: str,
    site: str | None,
    depth: int,
    inline: list[tuple[str, PresetSpec]],
) -> list[RegistryReference]:
    """Leg (i): the ``tool_names`` entries and inline ``presets[i].base_tool`` of one agent spec node, at any depth.

    Recurses ``subagents``; each inline preset that passes its shape legs is appended to
    ``inline`` with its place, for the walk of its own ``fixed_kwargs``.
    """
    if depth > MAX_REFERENCE_DEPTH:
        raise _too_deep(where, site)
    refs = _tool_names_references(node, where, site)
    refs.extend(_inline_preset_references(node, where, site, inline))
    subagents = node.get("subagents", [])
    if not isinstance(subagents, list):
        raise ReferenceShapeError(_at(site, f"{where}.subagents must be a list of sub-agent specs"))
    for i, entry in enumerate(subagents):
        if not isinstance(entry, dict):
            raise ReferenceShapeError(_at(site, f"{where}.subagents[{i}] must be an object"))
        refs.extend(_agent_spec_references(entry, f"{where}.subagents[{i}]", site, depth + 1, inline))
    return refs


def _extracted_entries[Entry](
    base: str,
    label: str,
    entry_type: type[Entry],
    extractor: Callable[[dict[str, Any]], Iterable[Any]],
    kwargs: dict[str, Any],
    prefix: str | None,
) -> list[Entry]:
    """A declared extractor's call: its entries, a ``ValueError`` as a shape refusal, anything else a plugin fault."""
    try:
        entries = list(extractor(kwargs))
    except ValueError as exc:
        raise ReferenceShapeError(_at(prefix, str(exc))) from exc
    except Exception as exc:
        fault = ExtractorFaultError(f"{label} extractor of base tool {base!r} raised {type(exc).__name__}")
        logger.exception("%s", fault)
        raise fault from exc
    for entry in entries:
        if not isinstance(entry, entry_type):
            fault = ExtractorFaultError(
                f"{label} extractor of base tool {base!r} returned a {type(entry).__name__} entry, "
                f"not a {entry_type.__name__}"
            )
            logger.error("%s", fault)
            raise fault
    return entries


async def _template_references(
    base: str, kwargs: dict[str, Any], site: str | None, place: str | None
) -> list[RegistryReference]:
    """Leg (iv): the stored template ids the blob names, from the base's declared extractor, else its annotations."""
    extractor = instance.app.tools.template_refs_extractor(base)
    if extractor is not None:
        entries = _extracted_entries(base, "template_refs", TemplateRef, extractor, kwargs, _joined(site, place))
        return [
            RegistryReference("stored id", entry.id, "template", _joined(site, place, entry.site)) for entry in entries
        ]
    try:
        slots = await annotated_template_ids(base, kwargs)
    except ValueError as exc:
        raise ReferenceShapeError(_at(_joined(site, place), str(exc))) from exc
    return [
        RegistryReference("stored id", template_id, "template", _joined(site, place, path))
        for path, template_id in slots
    ]


async def kwargs_references(
    target: str,
    kwargs: dict[str, Any],
    where: str,
    site: str | None = None,
    *,
    body_root: bool = False,
    depth: int = 0,
) -> list[RegistryReference]:
    """Every name a kwargs blob handed to ``target`` composes, as references, in walk order.

    ``where`` is the blob's place (``fixed_kwargs``, ``tool_kwargs``), ``site`` the chained
    extractor site above it, ``body_root`` set for a preset's own ``fixed_kwargs`` (the
    holder itself, so its place is left out of an extractor entry's site). The legs over
    ``settled_target(target)``, in order:

    - (i) an agent: its spec's ``tool_names`` and inline ``presets[i].base_tool`` at any depth;
    - (ii) a base declaring a ``tool_refs`` extractor: each entry it yields, then the walk of
      the ``tool_kwargs`` that entry passes, over the entry's tool;
    - (iv) the stored template ids the blob names: the base's declared ``template_refs``
      extractor when it has one, else the ``TemplatedText`` fields of its input annotations;
    - (iii) each inline preset's own ``fixed_kwargs``, over its own base.

    Raises ``ReferenceShapeError`` on a blob off a leg's shape (value-free text, prefixed
    ``at <site>: `` when a site is set) and ``ExtractorFaultError`` on a broken extractor.
    """
    if depth > MAX_REFERENCE_DEPTH:
        raise _too_deep(where, site)
    base = settled_target(target)
    if base is None:
        return []
    refs: list[RegistryReference] = []

    inline: list[tuple[str, PresetSpec]] = []
    if instance.app.agents.all_agents().get(base) is not None:
        refs.extend(_agent_spec_references(kwargs, where, site, depth, inline))

    place = where if not body_root else None
    extractor = instance.app.tools.tool_refs_extractor(base)
    if extractor is not None:
        for entry in _extracted_entries(base, "tool_refs", ComposedToolRef, extractor, kwargs, _joined(site, place)):
            chained = _joined(site, place, entry.site)
            refs.append(RegistryReference("composed tool", entry.name, "tool", chained))
            refs.extend(
                await kwargs_references(entry.name, entry.tool_kwargs or {}, "tool_kwargs", chained, depth=depth + 1)
            )

    refs.extend(await _template_references(base, kwargs, site, place))

    for inline_place, preset in inline:
        refs.extend(
            await kwargs_references(
                preset.base_tool, preset.fixed_kwargs, f"{inline_place}.fixed_kwargs", site, depth=depth + 1
            )
        )
    return refs


async def kwargs_template_refs(tool: str, kwargs: Mapping[str, Any]) -> list[TemplateRef]:
    """The stored template ids a call of ``tool`` with ``kwargs`` names, in walk order, each at its site."""
    refs = await kwargs_references(tool, dict(kwargs), "kwargs", body_root=True)
    return [as_template_ref(ref) for ref in refs if ref.registry == "template"]


async def body_references(body: PresetBody) -> list[RegistryReference]:
    """Every name a preset body's ``fixed_kwargs`` compose, by :func:`kwargs_references` over its base."""
    return await kwargs_references(body.base_tool, body.fixed_kwargs, "fixed_kwargs", body_root=True)


def inline_base_refusal(refs: Iterable[RegistryReference]) -> str | None:
    """The first inline preset base among ``refs`` that a registered preset stands behind, as a refusal, else ``None``.

    An inline preset's base follows the create door's base rule; a stored walk only collects names.
    """
    presets = instance.app.preset_manager
    for ref in refs:
        match = _INLINE_BASE_KIND.match(ref.kind)
        if match is None:
            continue
        refusal = presets.base_refusal(ref.name)
        if refusal is not None:
            return _at(ref.site, f"{match['place']} {refusal}")
    return None


async def assert_blob_references_resolve(
    head: RegistryReference, target: str, kwargs: dict[str, Any], where: str, *, body_root: bool = False
) -> list[RegistryReference]:
    """A write door's check of the blob it stores for ``target``: answers the blob's references when all resolve.

    ``head`` is the door's own reference (the base, the hook tool, the scheduled tool),
    resolved with the blob's. A shape refusal, an inline preset base a preset stands behind,
    or any unresolved name is a 400; an extractor fault is a 500.
    """
    try:
        refs = await kwargs_references(target, kwargs, where, body_root=body_root)
    except ReferenceShapeError as exc:
        raise BadRequestError(str(exc)) from exc
    except ExtractorFaultError as exc:
        raise OperationFailedError(str(exc)) from exc
    refusal = inline_base_refusal(refs)
    if refusal is not None:
        raise BadRequestError(refusal)
    await assert_references_resolve([head, *refs])
    return refs


async def stored_body_references(name: str, body: PresetBody) -> list[RegistryReference]:
    """Every name stored preset ``name``'s body composes, by :func:`body_references`.

    The one place a stored body's walk fault is answered: a 500 naming the row and the
    fault, ``code`` :data:`STORED_PRESET_UNREADABLE`, so deleting the row named clears it.
    """
    try:
        return await body_references(body)
    except (ReferenceShapeError, ExtractorFaultError) as exc:
        raise OperationFailedError(
            f"stored preset {name!r}: {exc}", extra={"code": STORED_PRESET_UNREADABLE, "preset": name}
        ) from exc


async def _preset_references(name: str, body: PresetBody) -> set[str]:
    """The tool names stored preset ``name``'s active body composes."""
    return {ref.name for ref in await stored_body_references(name, body) if ref.registry == "tool"}


async def _composed_origins(name: str, body: PresetBody) -> set[str]:
    """The tools a preset body composes, each extension branch tool replaced by the base it was produced from.

    A composed extension branch tool of a preset is bound, renamed and removed with that preset, so it
    counts as a reference to the preset itself. The origin is read from the tool registry's record of
    every bound branch (a name it does not know as a branch stays as it is).
    """
    return {instance.app.tools.base_of(composed) for composed in await _preset_references(name, body)}


async def _reference_maps(bodies: dict[str, PresetBody]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """The ``uses`` and ``used_by`` maps over the active preset population, built in one pass.

    ``uses[X]`` is the sorted OTHER preset names X's active body composes as tools, directly
    or through one of their extension branch tools (:func:`_composed_origins`, intersected
    with the population so base and foreign tools never appear); ``used_by[X]`` is the
    sorted OTHER presets whose active bodies compose X. Self is never listed on either side.
    Every population name is a key on both maps, empty when it has no references.
    """
    population = set(bodies)
    uses: dict[str, set[str]] = {name: set() for name in bodies}
    used_by: dict[str, set[str]] = {name: set() for name in bodies}
    for name, body in bodies.items():
        for referenced in (await _composed_origins(name, body)) & population:
            if referenced == name:
                continue
            uses[name].add(referenced)
            used_by[referenced].add(name)
    return (
        {name: sorted(names) for name, names in uses.items()},
        {name: sorted(names) for name, names in used_by.items()},
    )


class StoredPresets:
    """The stored presets' active bodies, read once, each body's composed origins walked at most once.

    One holder check builds one and asks it about every name it judges. Only active bodies are
    walked: a historical version may still name a tool, judged when it is rolled back. A body
    whose walk raises keeps no origins, so every later ask that walks it raises again.
    """

    def __init__(self, bodies: Mapping[str, PresetBody]) -> None:
        """Over ``bodies``, the stored active bodies keyed by preset name."""
        self.bodies = bodies
        self._origins: dict[str, set[str]] = {}

    @classmethod
    async def read(cls) -> StoredPresets:
        """Over the stored presets' active bodies, read now."""
        return cls(await instance.app.presets.list_active_bodies())

    async def _origins_of(self, name: str) -> set[str]:
        origins = self._origins.get(name)
        if origins is None:
            origins = self._origins[name] = await _composed_origins(name, self.bodies[name])
        return origins

    async def composers_of(self, tool: str) -> list[str]:
        """Every OTHER stored preset whose active body composes ``tool`` as a tool, sorted.

        A reference to one of ``tool``'s extension branch tools counts (:func:`_composed_origins`:
        a rename renames them with it). The preset named ``tool`` is never walked for it.
        """
        return sorted([name for name in self.bodies if name != tool and tool in await self._origins_of(name)])


async def _routes_bound_to(names: set[str]) -> dict[str, Any]:
    """Every conversation route whose ``tool`` target is one of ``names``, keyed by route name.

    A ``tool``-target route binds a preset as its target; a write that changes or removes the
    preset — or a preset that composes it — can strand it. A deployment without the routes
    capability holds none.
    """
    from tai42_skeleton.conversations import get_conversations_manager
    from tai42_skeleton.operations import NotSupportedError

    try:
        routes, _ = await get_conversations_manager().list_routes()
    except NotSupportedError:
        return {}
    return {
        route.route_name: route
        for route in routes.values()
        if route.target_kind == "tool" and route.target_name in names
    }


async def _composing_closure(name: str) -> set[str]:
    """Every preset that transitively COMPOSES ``name`` as a tool, via the ``used_by`` graph (``name`` excluded).

    A new version of ``name`` can break a preset that composes it as a tool, however deeply nested, so a
    route bound to any such composer is re-checked too. Built over the active preset population's
    reference maps; an empty set on a store-less deployment (no active bodies).
    """
    bodies = await instance.app.presets.list_active_bodies()
    _uses, used_by = await _reference_maps(bodies)
    closure: set[str] = set()
    frontier = [name]
    while frontier:
        current = frontier.pop()
        for composer in used_by.get(current, []):
            if composer != name and composer not in closure:
                closure.add(composer)
                frontier.append(composer)
    return closure


async def _assert_bound_routes_still_bind(name: str, candidate_body: Any) -> None:
    """Refuse a preset write whose new version would break a conversation route bound to it or a composer.

    Re-runs the route bind check of every route bound to ``name`` AND every route bound to a preset that
    transitively composes ``name`` (:func:`_composing_closure`), against the UNSAVED new version — the
    same referential-integrity stance a rename takes: a save/rollback that would leave a bound route
    unable to bind is a 409 that commits nothing, naming the routes so the operator fixes them first.

    For a route bound directly to ``name`` the candidate IS ``candidate_body``; for a route bound to a
    composer the candidate is the composer's own (unchanged) active body, while the changed ``name`` is
    made visible to the composer's composed-preset resolution through the candidate scope — so a composer
    the new version breaks (its target can now ask with no reply/resume path, or reads a state nothing
    binds) is refused too. The scope is cleared on exit so the candidate never leaks into the commit.
    """
    from tai42_skeleton.conversations.target_validators import active_target_candidate_body, target_bind_refusal_lines
    from tai42_skeleton.operations import ConflictError
    from tai42_skeleton.presets.candidate_scope import candidate_bodies

    targets = {name} | await _composing_closure(name)
    routes = await _routes_bound_to(targets)
    if not routes:
        return

    broken: list[str] = []
    with candidate_bodies({name: candidate_body}):
        for route_name, route in sorted(routes.items()):
            if route.target_name == name:
                candidate = candidate_body
            else:
                candidate = await active_target_candidate_body(route.target_kind, route.target_name)
            lines = await target_bind_refusal_lines(route, candidate)
            if lines:
                broken.append(f"{route_name!r} ({'; '.join(lines)})")
    if broken:
        raise ConflictError(
            f"preset {name!r} cannot be saved — the new version would break conversation route(s): "
            f"{'; '.join(broken)}; update those routes first"
        )
