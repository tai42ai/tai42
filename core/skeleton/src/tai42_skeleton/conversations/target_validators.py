"""The process-wide conversation target-bind-validator registry.

The body behind ``app.conversations.register_target_validator``.
The skeleton registers its own validator for the ``agent`` kind (an asking agent needs a
reply/resume path — see :func:`register_platform_target_validators`); a plugin registers
another kind when its module loads (importing the module runs its
``tai42_app.conversations.register_target_validator(...)`` call). Route creation consults the
registered validator for a route's target kind after the target exists but before the row is
written, passing the FULL create model; a validator returning message lines refuses the create
with them (a 422), so a defect the target carries — a flow reading a state no binding supplies,
an asking agent with no reply/resume path — is caught at bind, not deferred to run time.

The registry is reset on every ``start()`` (like the preset write-validator registry) so
a reload re-imports the plugin modules and re-registers cleanly; a duplicate kind within
one load raises loudly (a silent overwrite could swap a target kind's bind gate out from
under it).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from tai42_contract.conversations import ConversationRouteCreate, ConversationTargetKind, TargetBindValidator

if TYPE_CHECKING:
    from tai42_contract.agent import Agent


class TargetBindValidatorRegistry:
    """The registered target-bind validators, keyed by target kind."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._validators: dict[str, TargetBindValidator] = {}

    def register(self, target_kind: ConversationTargetKind, validator: TargetBindValidator) -> None:
        """Register ``validator`` for ``target_kind``; a duplicate kind raises loudly."""
        if target_kind in self._validators:
            raise ValueError(f"conversation target validator for kind {target_kind!r} is already registered")
        self._validators[target_kind] = validator

    def get(self, target_kind: str) -> TargetBindValidator | None:
        """The validator registered for ``target_kind``, or ``None`` when none is."""
        return self._validators.get(target_kind)

    def reset(self) -> None:
        """Clear every registered validator (called on each ``start()``)."""
        self._validators.clear()


# The builtin tool an agent lists to ask its caller mid-run. A route bound to an agent that
# lists it needs a reply and resume path, or an async caller-ask has nowhere to land.
_ASK_TOOL_NAME = "ask"


def _agent_declares_ask(agent: Agent) -> bool:
    """Whether ``agent``'s statically-declared tool set names the caller-ask tool.

    Read from the agent's declared ``tool_names`` (the fixed tool set an agent binds); an
    agent that resolves its tools at run time declares none, so it is treated as not asking
    — the truthful answer, never a guess.
    """
    declared = getattr(agent, "tool_names", None)
    if declared is None:
        field = agent.ToolInput.model_fields.get("tool_names")
        declared = field.get_default(call_default_factory=True) if field is not None else None
    if not isinstance(declared, Iterable) or isinstance(declared, str | bytes):
        return False
    return _ASK_TOOL_NAME in declared


async def _agent_ask_target_validator(create: ConversationRouteCreate) -> list[str]:
    """The platform bind check for an ``agent`` target: an asking agent needs a reply/resume path.

    An agent whose ``tool_names`` include ``ask`` can pause a turn to ask its caller; the route
    must then carry both ``reply_expr`` (to map the agent's result back to a reply) and
    ``resume_expr`` (to resume the parked run), or a caller-ask on the turn has no path back.
    Any other agent binds freely. The target's existence is asserted before this runs, so a
    name with no live agent is nothing to validate here.
    """
    from tai42_skeleton.app import instance

    agent = instance.app.agents.all_agents().get(create.target_name)
    if agent is None or not _agent_declares_ask(agent):
        return []
    missing = [
        name
        for name, value in (("reply_expr", create.reply_expr), ("resume_expr", create.resume_expr))
        if value is None
    ]
    if not missing:
        return []
    return [
        f"agent {create.target_name!r} can ask its caller (its tool_names include {_ASK_TOOL_NAME!r}) "
        f"but the route declares no {' and no '.join(missing)}: a caller-ask has no reply/resume path"
    ]


def register_platform_target_validators(registry: TargetBindValidatorRegistry) -> None:
    """Register the skeleton's own target-bind validators, after the registry is reset each ``start()``.

    The platform owns the ``agent`` kind (an asking agent needs a reply/resume path); a consumer
    plugin registers the other kinds — a flow tool target's state bindings — through
    ``app.conversations.register_target_validator`` when its module loads.
    """
    registry.register("agent", _agent_ask_target_validator)
