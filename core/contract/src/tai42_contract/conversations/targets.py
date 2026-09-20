"""Per-target conversation config and the pairing errors.

``TargetConversationConfig`` carries the multichannel opt-in and the first-contact greeting;
the pairing errors name the distinct refusal reasons a pairing turn raises.
"""

from __future__ import annotations

import string

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tai42_contract.conversation_target import ConversationTargetKind
from tai42_contract.errors import ErrorKind
from tai42_contract.states.binding import StateBinding


class PairCodeInvalidError(Exception):
    """A submitted pair code did not resolve to a live single-use record.

    Deliberately UNIFORM across unknown / expired / already-redeemed: the three are indistinguishable to
    the caller (no oracle), so a redeem reply never reveals whether a code ever existed.
    """

    # The submitted code did not work; deliberately uniform across unknown/expired/redeemed (no oracle).
    __tai_error_kind__ = ErrorKind.BAD_INPUT


class NotLinkedError(Exception):
    """An unlink was asked of an address that is not part of a multi-address person.

    It is already its own provisional person, so there is nothing to detach.
    """

    # A state-dependent refusal: the address is not part of a multi-address person, so there is nothing to detach.
    __tai_error_kind__ = ErrorKind.CONFLICT


class MultichannelDisabledError(Exception):
    """A pairing operation was attempted against a target whose multichannel support is off.

    The pairing tool refuses with this; the ``/link`` and ``/unlink`` commands instead pass
    through as ordinary text on such a target.
    """

    # The target's multichannel capability is off — a capability refusal, mirroring NotSupported -> UNAVAILABLE.
    __tai_error_kind__ = ErrorKind.UNAVAILABLE


class CrossTargetMergeError(Exception):
    """A merge was attempted across two different targets.

    Persons are per-target and can never span targets; a NAMED type so a pairing turn scopes it
    distinctly from an infrastructure fault.
    """

    # Structurally impossible by construction (persons never span targets) — an
    # invalid request, not a current-state conflict.
    __tai_error_kind__ = ErrorKind.BAD_INPUT


# The one placeholder a greeting template may reference — the mint-at-greeting-time pair
# code. Any other ``{...}`` field is refused at write time so a typo cannot render literally.
GREETING_PLACEHOLDER = "pairing_code"


def _check_greeting_placeholders(template: str) -> None:
    """Refuse a greeting template that references anything but ``{pairing_code}``.

    Parsed exactly as :meth:`str.format` would render it, so a malformed template (an
    unbalanced brace), an auto-numbered ``{}``, a foreign field name, or a
    ``{pairing_code}`` carrying a conversion/format-spec/attribute access is refused here —
    at the write — rather than rendering wrong or raising when the greeting fires.
    """
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"greeting_template is not a valid template: {exc}") from exc
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            # Literal text, including the escaped ``{{``/``}}`` braces.
            continue
        if field_name != GREETING_PLACEHOLDER or conversion is not None or format_spec:
            raise ValueError(
                f"greeting_template may reference only the {{{GREETING_PLACEHOLDER}}} placeholder, "
                f"got an unsupported field {field_name!r}"
            )


class TargetConversationConfig(BaseModel):
    """Per-target configuration for the conversation bridge, keyed by ``(target_kind, target_name)``.

    The key names the agent or tool an inbound turn is routed to.
    ``multichannel`` opts the target into person linking; ``greeting_template`` is the
    first-contact greeting, which may reference at most the ``{pairing_code}`` placeholder
    (minted at greeting time). The row carries no server-derived fields, so it IS its own
    create payload — no Create/stored split. An unknown ``{...}`` placeholder is refused so
    a typo cannot render literally; a blank template string is refused, because ``None`` is
    the explicit spelling for "no greeting". Frozen.
    """

    model_config = ConfigDict(frozen=True)

    target_kind: ConversationTargetKind
    target_name: str = Field(min_length=1)
    multichannel: bool = False
    greeting_template: str | None = None
    #: The OPTIONAL door-layer state binding a tool-target route applies around each tool
    #: turn; deposited on the ambient dispatch context before the turn's ``run_tool``. The
    #: route read serves it back so an editor round-trips it.
    state_binding: StateBinding | None = None

    @field_validator("target_name")
    @classmethod
    def _non_blank_target_name(cls, value: str) -> str:
        # It keys the config row; a blank segment would collide distinct targets onto one key.
        if not value.strip():
            raise ValueError("target_name must be non-blank")
        return value

    @field_validator("greeting_template")
    @classmethod
    def _check_greeting_template(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("greeting_template must be non-blank (use null for no greeting)")
        _check_greeting_placeholders(value)
        return value

    @model_validator(mode="after")
    def _state_binding_is_tool_only(self) -> TargetConversationConfig:
        # A door binding applies around a tool DISPATCH; an agent turn deposits none, so a
        # binding on an agent config would never apply. Refuse it here so no door or store
        # read ever carries one on an agent target.
        if self.target_kind != "tool" and self.state_binding is not None:
            raise ValueError("state_binding is valid only for a tool target (target_kind='tool')")
        return self
