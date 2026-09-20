"""Render an agent message from its :class:`~tai42_contract.template.TemplatedText`.

An agent message field is ``TemplatedText | None`` — inline ``content`` or a stored
``id`` plus its render ``kwargs``, or ``None`` for an unset slot. :func:`render_message`
maps an unset optional slot to the empty string and renders a present one through the kit's
:func:`~tai42_kit.utils.render.render_templated_text` seam, never the skeleton internals
below it; a required slot (``allow_empty`` false) that is absent or renders to nothing but
whitespace is refused loudly rather than run on a blank prompt.
"""

from __future__ import annotations

from tai42_contract.template import TemplatedText
from tai42_kit.utils.render import render_templated_text


async def render_message(
    text: TemplatedText | None,
    *,
    allow_empty: bool = True,
    field: str = "message",
    locale: str | None = None,
) -> str:
    """Render ``text`` — its stored ``id`` or inline ``content`` — with its own ``kwargs``.

    An unset message (``None``) renders to the empty string when ``allow_empty`` is true.
    When ``allow_empty`` is false the message is required: an absent one, and one that
    renders to only whitespace, both raise :class:`ValueError` naming ``field`` — a
    whitespace-only render carries no instruction, so it is as effectively absent as an
    empty string and refusing it keeps a required face from running on a blank prompt.
    ``locale`` selects a stored resource's locale variant and reaches the render as its
    language.
    """
    if text is None:
        if allow_empty:
            return ""
        raise ValueError(f"{field}: required message was not provided")
    rendered = await render_templated_text(text, locale)
    if not allow_empty and not rendered.strip():
        raise ValueError(f"{field}: required message was not provided")
    return rendered
