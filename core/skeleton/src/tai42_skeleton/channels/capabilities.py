"""The one table of a channel's rich-send capabilities and the fields each one gates.

A channel advertises each capability as an OPTIONAL class flag (absent = ``False``), so a
text-only channel is a valid target that declares none. ``notify_user`` refuses a gated field a
channel does not advertise, and the conversation delivery executor refuses an answer part that
needs one; both read this table. ``header``/``footer`` ride an already-gated choice surface and
``data``/``pages`` ride the form, so none of them carries a gate of its own.

:func:`check_channel_part` is the per-part check that follows the capability gate: the channel's
own template and form checks, run by ``notify_user`` and by the conversation send admission.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from tai42_contract.channels import Channel, ChannelTemplate

from tai42_skeleton.interactions.form_schema import check_channel_form

# (field, Channel flag, part label, notification word). A sectioned list IS an interactive
# choice surface, so it rides the same flag as flat options.
RICH_CAPABILITIES: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("media", "supports_media_notifications", "media", "media"),
    ("location", "supports_location_notifications", "location", "location"),
    ("template", "supports_template_notifications", "template", "template"),
    ("options", "supports_interactive_notifications", "interactive options", "interactive"),
    ("sections", "supports_interactive_notifications", "interactive sections", "interactive"),
    ("schema", "supports_form_notifications", "form", "form"),
)


def unsupported_capability(channel: Channel, values: Mapping[str, Any]) -> tuple[str, str] | None:
    """The first ``(part label, notification word)`` whose field is set in ``values`` and whose flag ``channel`` lacks.

    ``None`` when every set field is advertised. The flags are read defensively (``getattr`` with a
    ``False`` default), so a channel that never declares one is refused, never broken.
    """
    for field, flag, label, word in RICH_CAPABILITIES:
        if values.get(field) is not None and not getattr(channel, flag, False):
            return label, word
    return None


def check_channel_part(
    channel_obj: Channel,
    *,
    message: str,
    template: ChannelTemplate | None,
    schema: dict[str, Any] | None,
) -> None:
    """Refuse a part the channel ``channel_obj`` cannot draw, before any of it is sent.

    For a ``template``, the channel's OPTIONAL ``validate_template(template)`` hook, which parses
    the opaque parameters against the channel's own template-parameter schema; a channel without
    the hook passes every template here (its ``notify`` still refuses an unmappable one). For a
    ``schema``, :func:`~tai42_skeleton.interactions.form_schema.check_channel_form` with
    ``message`` as the question: the exact message the form is drawn with. Whatever is set is
    checked. Raises the hook's or the walk's ``ValueError`` unchanged.
    """
    if template is not None:
        validate_template = getattr(channel_obj, "validate_template", None)
        if validate_template is not None:
            validate_template(template)
    if schema is not None:
        check_channel_form(channel_obj, schema, message)
