"""Per-target conversation-config doors keyed ``(target_kind, target_name)``.

The multichannel opt-in + first-contact greeting, with their key validator and config-store accessor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, get_args

from tai42_contract.conversations import ConversationTargetKind, TargetConversationConfig
from tai42_contract.states.binding import StateBinding

from tai42_skeleton.operations import BadRequestError, NotFoundError, operation
from tai42_skeleton.operations.errors import NotSupportedError
from tai42_skeleton.operations.response_models_group_a import (
    ConversationConfigDeleteResult,
    ConversationConfigListEnvelope,
    ConversationConfigSetResult,
)

from .backend import _require_backend
from .routes import _assert_target_exists

if TYPE_CHECKING:
    from tai42_skeleton.conversations.target_config import ConversationTargetConfigStore

_TARGET_KINDS = get_args(ConversationTargetKind)


def _validate_target_key(target_kind: str, target_name: str) -> None:
    """Validate a config key: a known ``target_kind`` and a non-blank ``target_name``.

    A malformed key is the caller's own 400, told apart from a well-formed key that names no stored config
    (a 404).
    """
    if target_kind not in _TARGET_KINDS:
        raise BadRequestError(f"target_kind must be one of {list(_TARGET_KINDS)}: {target_kind!r}")
    if not target_name.strip():
        raise BadRequestError("target_name must be a non-blank target identifier")


def _config_store() -> ConversationTargetConfigStore:
    """The config store over the live conversations settings.

    Called only after :func:`_require_backend`, so its own backend guard never fires here.
    """
    from tai42_skeleton.conversations.settings import ConversationsSettings
    from tai42_skeleton.conversations.target_config import ConversationTargetConfigStore

    return ConversationTargetConfigStore(ConversationsSettings())


@operation(
    summary="List conversation target configs",
    tags=["conversations"],
    errors=[NotSupportedError],
    response_model=ConversationConfigListEnvelope,
)
async def list_conversation_configs() -> dict[str, Any]:
    """Every stored per-target conversation config. Returns ``{"items", "total"}``."""
    _require_backend()
    configs = await _config_store().list()
    items = [config.model_dump(mode="json") for config in configs.values()]
    return {"items": items, "total": len(items)}


@operation(
    summary="Get a conversation target config",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    response_model=TargetConversationConfig,
)
async def get_conversation_config(target_kind: str, target_name: str) -> dict[str, Any]:
    """One per-target config by ``(target_kind, target_name)``.

    An unknown key is a loud 404; a key whose ``target_kind`` is not a known kind, or whose ``target_name``
    is blank, is a 400.
    """
    _validate_target_key(target_kind, target_name)
    _require_backend()
    config = await _config_store().get(target_kind, target_name)
    if config is None:
        raise NotFoundError(f"conversation config not found: {target_kind}/{target_name}")
    return config.model_dump(mode="json")


@operation(
    summary="Create or replace a conversation target config",
    tags=["conversations"],
    destructive=True,
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    request_model=TargetConversationConfig,
    response_model=ConversationConfigSetResult,
)
async def set_conversation_config(
    target_kind: str,
    target_name: str,
    multichannel: bool = False,
    greeting_template: str | None = None,
    state_binding: StateBinding | None = None,
) -> dict[str, Any]:
    """Create or replace the per-target config for ``(target_kind, target_name)`` — an UPSERT.

    This is the create path AND the edit path for a config of that key.

    The target must merely EXIST — the agent (``target_kind=agent``) or tool
    (``target_kind=tool``) — exactly as the route create checks it. ``greeting_template``
    may reference at most the ``{pairing_code}`` placeholder and a blank template is refused
    (``null`` = no greeting), both enforced by the model. No key is bound and no authority
    delegated: this is inert operator config. Returns
    ``{"created", "target_kind", "target_name", "config"}``.
    """
    # Validate the whole body at the operation, not the edge: the MCP tool and a direct
    # call take these flat parameters and bypass the HTTP extractor.
    try:
        config = TargetConversationConfig(
            target_kind=target_kind,  # pyright: ignore[reportArgumentType]
            target_name=target_name,
            multichannel=multichannel,
            greeting_template=greeting_template,
            state_binding=state_binding,
        )
    except ValueError as exc:
        raise BadRequestError(f"invalid conversation config: {exc}") from exc
    _require_backend()
    await _assert_target_exists(config.target_kind, config.target_name)
    if state_binding is not None:
        from tai42_skeleton.app import instance
        from tai42_skeleton.tools.state_binding import validate_and_attach_binding

        # Attach-on-use + validate the binding at SAVE (the config upsert) — a bad binding is
        # a 400 that persists no config.
        await validate_and_attach_binding(instance.app, state_binding)
    created = await _config_store().upsert(config)
    return {
        "created": created,
        "target_kind": config.target_kind,
        "target_name": config.target_name,
        "config": config.model_dump(mode="json"),
    }


@operation(
    summary="Delete a conversation target config",
    tags=["conversations"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    response_model=ConversationConfigDeleteResult,
)
async def delete_conversation_config(target_kind: str, target_name: str) -> dict[str, Any]:
    """Delete the per-target config for ``(target_kind, target_name)``.

    An unknown key is a loud 404; a malformed key is a 400. Returns ``{"removed", "target_kind", "target_name"}``.
    """
    _validate_target_key(target_kind, target_name)
    _require_backend()
    removed = await _config_store().delete(target_kind, target_name)
    if not removed:
        raise NotFoundError(f"conversation config not found: {target_kind}/{target_name}")
    return {"removed": True, "target_kind": target_kind, "target_name": target_name}
