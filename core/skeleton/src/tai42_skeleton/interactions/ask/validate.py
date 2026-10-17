"""Up-front argument/combo validation for ``ask``.

Reject every bad argument combination and resolve the derived values (``fmt``, ``channel_obj``,
clamped ``audience``, normalized ``schema``) before any state is written.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ValidationError
from tai42_contract.app import tai42_app
from tai42_contract.channels import Channel
from tai42_contract.hooks import TopicVerifierBinding
from tai42_contract.interactions import AnswerFormat, check_addressing, check_ask_timing

from tai42_skeleton.access_control.user import clamp_write_audience
from tai42_skeleton.channels.registry import resolve_channel
from tai42_skeleton.interactions.form_schema import validate_channel_form_schema

from .format_args import check_format_arguments, parse_answer_format
from .payload import normalize_schema


@dataclass(frozen=True)
class AskValidation:
    """The resolved outcome of the up-front validation.

    The parsed answer format, whether it is the external format, the resolved channel object
    (reused by delivery), the write-clamped audience, the normalized schema, and the validated
    verifier binding.
    """

    fmt: AnswerFormat
    is_external: bool
    channel_obj: Channel | None
    audience: str | None
    schema: type[BaseModel] | dict[str, Any] | None
    verifier: TopicVerifierBinding | None


def validate_ask_arguments(
    question: str,
    *,
    answer_format: str,
    options: list[str] | None,
    schema: type[BaseModel] | dict[str, Any] | None,
    data: Any,
    pages: Any,
    timeout: float | None,
    link: str | Callable[[str], Awaitable[str]] | None,
    verifier: TopicVerifierBinding | Mapping[str, Any] | None,
    channel: str | None,
    recipient: str | None,
    audience: str | None,
    mode: Literal["sync", "async"],
    expiry_at: datetime | None,
    to: Literal["user", "caller"],
    payload: dict[str, Any] | None,
    reaction_tool: str | None = None,
    reactions: Any = None,
) -> AskValidation:
    """Reject every bad argument/combo before any state; resolve the derived values.

    Resolves ``fmt``, ``channel_obj``, the write-clamped ``audience`` and the normalized
    ``schema``.
    """
    check_addressing(to=to, mode=mode, question=question, payload=payload, answer_format=answer_format)
    fmt = _validate_timing_and_format(
        mode,
        timeout,
        expiry_at,
        answer_format,
        options,
        data,
        pages,
        reaction_tool,
        reactions,
    )
    is_external = fmt is AnswerFormat.EXTERNAL
    # ``audience`` (the addressed identity) is validated loud and up front — a
    # blank/whitespace value can never address a real identity — mirroring the
    # ``notify_user`` guard so both surfaces reject it identically.
    if audience is not None and (not isinstance(audience, str) or not audience.strip()):
        raise ValueError("audience must be a non-empty identity")
    # Write-side isolation clamp: a restricted caller may address only its own slice.
    audience = clamp_write_audience(audience)
    channel_obj, schema = _validate_channel_args(
        channel, link, verifier, fmt, schema, question, recipient, reaction_tool
    )
    binding = _validate_external_args(is_external, link, channel, schema, verifier)
    if fmt is AnswerFormat.FREE and schema is not None:
        # A FREE answer is checked against its schema; normalize it here so a bad schema fails
        # before any state is written.
        schema = normalize_schema(schema)
    return AskValidation(
        fmt=fmt, is_external=is_external, channel_obj=channel_obj, audience=audience, schema=schema, verifier=binding
    )


def _validate_timing_and_format(
    mode: str,
    timeout: float | None,
    expiry_at: datetime | None,
    answer_format: str,
    options: Any,
    data: Any,
    pages: Any,
    reaction_tool: str | None,
    reactions: Any,
) -> AnswerFormat:
    """Validate the timing (sync ``timeout`` vs async ``expiry_at``) and the format's argument shape.

    ``options`` only on select/text, ``data``/``pages``/``reactions``/``reaction_tool`` only on
    form. The per-send ``wording`` members' per-format admission is the one admission table checked
    when the ``InteractionRequest`` is built. Returns the parsed :class:`AnswerFormat`.
    """
    check_ask_timing(timeout=timeout, expiry_at=expiry_at)
    if mode != "async" and expiry_at is not None:
        raise ValueError("expiry_at is only valid with mode='async'")
    if mode == "async" and expiry_at is None:
        # An async park with no deadline is never expiry-indexed, so the reaper could
        # never fire its continuation and the idle TTL would drop it silently — refuse
        # it up front rather than persist an unresumable park.
        raise ValueError("async mode requires expiry_at")
    fmt = parse_answer_format(answer_format)
    check_format_arguments(
        fmt, options=options, data=data, pages=pages, reactions=reactions, reaction_tool=reaction_tool
    )
    return fmt


def _validate_channel_args(
    channel: str | None,
    link: Any,
    verifier: Any,
    fmt: AnswerFormat,
    schema: type[BaseModel] | dict[str, Any] | None,
    question: str,
    recipient: str | None,
    reaction_tool: str | None,
) -> tuple[Channel | None, type[BaseModel] | dict[str, Any] | None]:
    """Resolve and validate a set ``channel`` (loud, up front).

    The channel owns delivery so ``link``/``verifier`` are forbidden, a form is deliverable only
    over a form-capable channel with a renderable schema, a reacting form only over a channel
    that advertises ``supports_form_reaction``, and a ``recipient`` needs a channel. Returns the
    resolved channel object (or ``None``) and the possibly-normalized schema.
    """
    if channel is None:
        if recipient is not None:
            # An address is meaningless without a channel to send on; the named
            # channel is what carries (and allowlist-validates) the recipient.
            raise ValueError("recipient requires a channel (an address is meaningless without one)")
        # A reacting form with no channel is served by the in-app reaction door (the
        # authenticated sibling of the answer door), so it is allowed here — no transport
        # gate, the inbox IS the transport.
        return None, schema
    channel_obj = validate_channel(channel)
    if link is not None:
        # The channel owns the delivery surface for every format.
        raise ValueError("link is forbidden when a channel is set (the channel owns delivery)")
    if verifier is not None:
        # A channel's forward to the callback door is unsigned, so a bound verifier
        # would 401 every reply — the question could never be answered.
        raise ValueError("verifier is forbidden when a channel is set (the channel forward is unsigned)")
    if fmt is AnswerFormat.FORM:
        schema = _validate_channel_form(channel, channel_obj, schema, question, reaction_tool)
    if recipient is not None and (not isinstance(recipient, str) or not recipient.strip()):
        # Rejected up-front as a clean ValueError — never a post-persist pydantic
        # error from the delivery frame's own recipient validator.
        raise ValueError("recipient must be a non-empty address")
    return channel_obj, schema


def _validate_channel_form(
    channel: str,
    channel_obj: Channel,
    schema: type[BaseModel] | dict[str, Any] | None,
    question: str,
    reaction_tool: str | None,
) -> type[BaseModel] | dict[str, Any] | None:
    """Validate a channel-delivered form.

    The channel must advertise ``supports_form_delivery`` and (when a schema is given) the
    schema must fall in the channel-renderable subset plus the channel's own optional
    ``validate_form_schema`` limits. A REACTING form (one naming a ``reaction_tool``) must ALSO
    advertise ``supports_form_reaction`` — a medium that cannot route a reaction's triggers back
    is refused loudly here, never left to render the form inert. Returns the normalized schema.
    """
    if not getattr(channel_obj, "supports_form_delivery", False):
        # A form is delivered only over a channel that advertises the capability; a
        # channel without it can never surface a multi-field form.
        raise ValueError(f"channel {channel!r} does not deliver form questions")
    if reaction_tool is not None and not getattr(channel_obj, "supports_form_reaction", False):
        # A reacting form needs a medium that can route its mid-form triggers back to the
        # reaction door; a channel without the flag is refused up front (never inert).
        raise ValueError(f"channel {channel!r} does not support reacting forms (supports_form_reaction)")
    if schema is not None:
        # A channel form is answered on the server-rendered callback page, so its
        # schema must fall in the renderable subset. Normalize once (pydantic model ->
        # JSON schema) and carry the dict forward so nothing re-normalizes it.
        schema = normalize_schema(schema)
        validate_channel_form_schema(schema)
        # Channel-specific form limits (reserved names, per-medium caps, question-text
        # caps) the generic subset does not know: the channel's OPTIONAL
        # ``validate_form_schema`` hook enforces them at the chokepoint, raising
        # ``ValueError`` before any state is written.
        validate_form_schema = getattr(channel_obj, "validate_form_schema", None)
        if validate_form_schema is not None:
            validate_form_schema(schema, question)
    return schema


def _validate_external_args(
    is_external: bool, link: Any, channel: str | None, schema: type[BaseModel] | dict[str, Any] | None, verifier: Any
) -> TopicVerifierBinding | None:
    """Validate the external/non-external combos; return the validated verifier binding.

    External needs a ``link`` (or a channel) and normalizes its optional schema + validates its
    optional verifier; a non-external format forbids both ``link`` and ``verifier``.
    """
    if is_external:
        if link is None and channel is None:
            raise ValueError("answer_format 'external' requires a link (or a channel)")
        # For external, the schema is normalized here too so a bad schema fails BEFORE
        # the link builder does external work.
        if schema is not None:
            normalize_schema(schema)
        return validate_verifier(verifier) if verifier is not None else None
    if link is not None:
        raise ValueError("link is only valid with answer_format 'external'")
    # A verifier authenticates the external server-to-server callback; on a
    # human-answerable format it would emit ``server_verified`` and make the UI
    # render a non-actionable card no human can ever answer.
    if verifier is not None:
        raise ValueError("verifier is only valid with answer_format 'external'")
    return None


def validate_verifier(verifier: Any) -> TopicVerifierBinding:
    """Validate ``verifier`` into a :class:`TopicVerifierBinding` naming a registered verifier, before any state.

    A binding of another shape (or a typo'd/unregistered name) would otherwise reach the callback
    door as a malformed binding — so this is a hard guard (``ValueError``), never a soft ignore.
    """
    try:
        binding = TopicVerifierBinding.model_validate(verifier)
    except ValidationError as exc:
        raise ValueError(
            f"verifier must be a {{'verifier': <name>, 'config': {{...}}}} binding: {exc.errors()[0]['msg']}"
        ) from None
    try:
        tai42_app.webhook_verifiers.get(binding.verifier)
    except Exception as exc:
        raise ValueError(f"unknown webhook verifier: {binding.verifier!r}") from exc
    return binding


def validate_channel(channel: Any) -> Channel:
    """Reject a malformed or unknown ``channel`` at ask-time, before any state is written.

    It must be a non-empty string naming a registered channel — an unknown name would otherwise
    persist a question no deliverer can ever push to a human, leaving the caller blocked until
    timeout. A hard guard (raise), never a soft ignore. Returns the resolved channel object;
    delivery reuses this exact validated instance, so a registry change between validation and
    delivery can never surface as a post-persist lookup failure.
    """
    return resolve_channel(channel)
