"""The one table of a channel's rich-send capabilities and the fields each one gates.

A channel advertises each capability as an OPTIONAL class flag (absent = ``False``), so a
text-only channel is a valid target that declares none. ``notify_user`` refuses a gated field a
channel does not advertise, and the conversation delivery executor refuses an answer part that
needs one; both read this table. ``header``/``footer`` ride an already-gated choice surface and
``data``/``pages`` ride the form, so none of them carries a gate of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from tai42_contract.channels import Channel

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
