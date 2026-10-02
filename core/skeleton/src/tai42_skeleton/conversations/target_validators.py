"""The process-wide conversation target-bind-validator registry and the route bind check.

The body behind ``app.conversations.register_target_validator``: a plugin registers a bind
validator for the one tool or agent it OWNS, keyed ``(kind, name)``. The route bind check
(:func:`target_bind_refusal_lines`) resolves a route's target to its registered owner (an agent
is its own name; a tool walks its ``parent_tool`` chain, so a preset inherits its base tool's
owner), runs the platform's OWN rules plus that one owner validator, and returns the refusal
lines a route write joins into the 422. The validators are passed the route's full create model
and the target's candidate body, so an owner can judge an unsaved version of the target.

The registry is reset on every ``start()`` (like the preset write-validator registry) so a
reload re-imports the plugin modules and re-registers cleanly; a duplicate ``(kind, name)``
within one load raises loudly — a name has one owner, and a silent overwrite could swap a
target's bind gate out from under it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from fastmcp.tools.tool_transform import TransformedTool
from tai42_contract.conversations import ConversationRouteCreate, ConversationTargetKind, TargetBindValidator
from tai42_contract.presets import PresetBody
from tai42_contract.presets.errors import PresetNotFoundError
from tai42_kit.db import component_store_configured

from tai42_skeleton.db import SKELETON_COMPONENT

if TYPE_CHECKING:
    from tai42_contract.agent import Agent


class TargetBindValidatorRegistry:
    """The registered target-bind validators, keyed by ``(target_kind, target_name)``."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._validators: dict[tuple[str, str], TargetBindValidator] = {}

    def register(self, target_kind: ConversationTargetKind, target_name: str, validator: TargetBindValidator) -> None:
        """Register ``validator`` for the ``(target_kind, target_name)`` a plugin owns; a duplicate raises loudly."""
        key = (target_kind, target_name)
        if key in self._validators:
            raise ValueError(f"conversation target validator for {target_kind!r} {target_name!r} is already registered")
        self._validators[key] = validator

    def get(self, target_kind: str, target_name: str) -> TargetBindValidator | None:
        """The validator registered for ``(target_kind, target_name)``, or ``None`` when none is."""
        return self._validators.get((target_kind, target_name))

    def reset(self) -> None:
        """Clear every registered validator (called on each ``start()``)."""
        self._validators.clear()


# The builtin tool an agent lists to ask its caller mid-run. A route whose target resolves to an
# agent that lists it needs a reply and resume path, or an async caller-ask has nowhere to land.
_ASK_TOOL_NAME = "ask"


def _declares_ask(tool_names: object) -> bool:
    """Whether a statically-declared ``tool_names`` value names the caller-ask tool.

    A non-iterable (or an unresolved run-time tool set declared as ``None``) is treated as not
    asking — the truthful answer, never a guess.
    """
    if not isinstance(tool_names, Iterable) or isinstance(tool_names, str | bytes):
        return False
    return _ASK_TOOL_NAME in tool_names


def _agent_declared_tool_names(agent: Agent) -> object:
    """An agent's statically-declared ``tool_names`` (the fixed tool set an agent binds), or its ToolInput default.

    An agent that resolves its tools at run time declares none.
    """
    declared = getattr(agent, "tool_names", None)
    if declared is None:
        field = agent.ToolInput.model_fields.get("tool_names")
        declared = field.get_default(call_default_factory=True) if field is not None else None
    return declared


async def _resolve_base_tool_name(target_name: str) -> str:
    """Walk a tool target's ``parent_tool`` chain to its root base tool name.

    A preset is a transform of its base tool, so a preset resolves to the base tool (or agent run
    tool) it is built on. A name the live registry does not know resolves to itself — its existence
    is asserted before the bind check runs, so a miss here is nothing to walk.
    """
    from tai42_skeleton.app import instance
    from tai42_skeleton.tools.binding import UnknownToolError

    try:
        tool_obj = await instance.app.tools.get_tool(target_name)
    except UnknownToolError:
        return target_name
    while isinstance(tool_obj, TransformedTool):
        tool_obj = tool_obj.parent_tool
    return tool_obj.name


async def _resolve_target_owner(create: ConversationRouteCreate) -> tuple[str, str]:
    """The ``(kind, name)`` of the tool or agent a route's target RESOLVES to.

    An ``agent`` target is its own name. A ``tool`` target walks its ``parent_tool`` chain to its
    base tool; when that base is a registered agent's run tool the target resolves to that agent
    (every agent is also a tool, and a preset can sit over an agent base), otherwise to the base
    tool itself. This is the owner the registered validator — and the platform's asking-agent rule
    — key on, so a second name for the same thing cannot bind around a check.
    """
    from tai42_skeleton.app import instance

    if create.target_kind == "agent":
        return ("agent", create.target_name)
    base = await _resolve_base_tool_name(create.target_name)
    if base in instance.app.agents.all_agents():
        return ("agent", base)
    return ("tool", base)


async def _effective_tool_names(
    create: ConversationRouteCreate, candidate_body: PresetBody | None, agent_name: str
) -> object:
    """The ``tool_names`` the target would run the agent with.

    A direct ``agent`` target runs the agent's own statically-declared set. A ``tool`` target that
    resolves to an agent is a preset over the agent (or the agent's run tool itself): a preset BAKES
    the list as a hidden constant, so walk the preset chain from the target toward the agent and take
    the list baked CLOSEST to the agent (an inner bake an outer preset cannot override), falling back
    to the agent's own default when no preset in the chain bakes it.
    """
    from tai42_skeleton.app import instance

    agents = instance.app.agents.all_agents()
    if create.target_kind == "agent":
        return _agent_declared_tool_names(agents[agent_name])
    baked: object | None = None
    body = candidate_body
    while body is not None:
        if "tool_names" in body.fixed_kwargs:
            baked = body.fixed_kwargs["tool_names"]
        if body.base_tool in agents:
            break
        if not component_store_configured(SKELETON_COMPONENT):
            break
        try:
            body = await instance.app.presets.store.get_active_body(body.base_tool)
        except PresetNotFoundError:
            break
    if baked is not None:
        return baked
    return _agent_declared_tool_names(agents[agent_name])


async def _platform_target_lines(
    create: ConversationRouteCreate, candidate_body: PresetBody | None, owner: tuple[str, str]
) -> list[str]:
    """The platform's own bind rules for a route — the asking-agent reply/resume rule, by resolution.

    Whenever a route's target RESOLVES to an agent — in either kind, including a preset over an
    agent — whose effective ``tool_names`` include ``ask``, the route must carry both ``reply_expr``
    (to map the agent's result back to a reply) and ``resume_expr`` (to resume the parked run), or a
    caller-ask on the turn has no path back. Any other target carries no platform bind rule here.
    """
    from tai42_skeleton.app import instance

    kind, name = owner
    if kind != "agent" or name not in instance.app.agents.all_agents():
        return []
    tool_names = await _effective_tool_names(create, candidate_body, name)
    if not _declares_ask(tool_names):
        return []
    missing = [
        field
        for field, value in (("reply_expr", create.reply_expr), ("resume_expr", create.resume_expr))
        if value is None
    ]
    if not missing:
        return []
    return [
        f"agent {create.target_name!r} can ask its caller (its tool_names include {_ASK_TOOL_NAME!r}) "
        f"but the route declares no {' and no '.join(missing)}: a caller-ask has no reply/resume path"
    ]


async def target_bind_refusal_lines(create: ConversationRouteCreate, candidate_body: PresetBody | None) -> list[str]:
    """The blocking refusal lines forbidding a route to bind its target — platform rules plus the owner's.

    Resolves the route's target to its registered owner, runs the platform's own rules (the
    asking-agent reply/resume rule, by resolution) plus the one validator registered for that owner,
    and returns the joined refusal lines (empty = allow). ``candidate_body`` is the target's stored
    body (a preset's body, or ``None`` when the target is not a preset); it lets the owner — and the
    platform rule — judge an unsaved version of the target. The target's existence is asserted before
    this runs, so a name with no live target is nothing to validate here.
    """
    from tai42_skeleton.app import instance

    owner = await _resolve_target_owner(create)
    lines = await _platform_target_lines(create, candidate_body, owner)
    validator = instance.app._target_validator_registry.get(*owner)
    if validator is not None:
        lines = [*lines, *await validator(create, candidate_body)]
    return lines


async def active_target_candidate_body(target_kind: str, target_name: str) -> PresetBody | None:
    """The target's active stored body, or ``None`` when the target is not a preset.

    The candidate body the bind check judges on the create/import path (where the target's active
    version IS the candidate). A ``tool`` target that is a preset yields its active body; an agent
    target, a base tool, or a store-less deployment yields ``None``.
    """
    from tai42_skeleton.app import instance

    if target_kind != "tool" or not component_store_configured(SKELETON_COMPONENT):
        return None
    try:
        return await instance.app.presets.store.get_active_body(target_name)
    except PresetNotFoundError:
        return None
