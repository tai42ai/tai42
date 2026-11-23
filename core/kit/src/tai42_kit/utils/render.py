"""Render a :class:`~tai42_contract.template.TemplatedText` through the host's resource manager.

Homed in kit so anything below the skeleton — a plugin, an execution backend — renders a
templated text without importing the skeleton that implements the manager. The bound
``tai42_app`` handle is the seam, the way :func:`~tai42_kit.utils.data.run_jq_first` is
the seam for evaluating a jq expression.
"""

from __future__ import annotations

import json
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText, TemplateRenderError


async def render_templated_text(text: TemplatedText, locale: str | None = None) -> str:
    """Return ``text`` rendered with its own ``kwargs``.

    The source is the stored resource it names by ``id``, or its inline ``content``.
    ``locale`` selects the stored resource's locale variant and reaches the render as its
    language. A text that cannot be rendered as authored raises the manager's
    :class:`~tai42_contract.template.TemplateRenderError`; every other failure (the store,
    the network, the manager's own code) propagates as itself.
    """
    return await tai42_app.storage.resource_manager.render_templated_text(text, locale)


class SchemaBodyError(ValueError):
    """An authored schema body could not be resolved to a JSON-object schema.

    Raised for a :class:`~tai42_contract.template.TemplatedText` body whose stored resource could
    not be rendered, whose rendered text was not valid JSON, or whose JSON was not an object. The
    message names the field so the failure is never a silent empty schema or an accepted raw text.
    """


async def resolve_schema_body(
    field: str, value: TemplatedText | dict[str, Any] | None, *, locale: str | None = None
) -> dict[str, Any] | None:
    """Resolve an authored JSON-Schema body — the ``TemplatedText | dict`` union — to a plain schema ``dict``.

    An inline ``dict`` is the schema document itself, returned unchanged (no stringification,
    no behaviour change for an inline author). A :class:`~tai42_contract.template.TemplatedText`
    names the schema by stored ``id`` (or carries it inline as ``content``): it is RENDERED
    through the resource manager, its rendered text is PARSED as JSON, and the result must be a
    JSON object — the same object precondition an inline schema meets. A text the manager cannot
    render as authored, a parse failure, or a non-object result each raises :class:`SchemaBodyError`
    LOUDLY naming ``field`` (and the stored ``id`` when it is by-id); none falls back to an empty
    schema, accepts the raw text, or skips the caller's own JSON-Schema validation that follows on
    the returned dict. Any other failure of the render propagates as itself. ``None`` resolves to
    ``None`` (the unset body). ``locale`` selects a stored resource's locale variant.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    where = f"stored id {value.id!r}" if value.id is not None else "inline templated content"
    try:
        rendered = await render_templated_text(value, locale)
    except TemplateRenderError as exc:
        raise SchemaBodyError(f"{field} {exc}") from exc
    try:
        parsed = json.loads(rendered)
    except json.JSONDecodeError as exc:
        raise SchemaBodyError(f"{field}: its {where} did not render to valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SchemaBodyError(
            f"{field}: its {where} rendered to a {type(parsed).__name__}, not a JSON object (a JSON Schema)"
        )
    return parsed


class TextBodyError(ValueError):
    """An authored text body could not be resolved to a program text.

    Raised for a :class:`~tai42_contract.template.TemplatedText` whose stored resource could not be
    rendered, and for a text (either form) that is empty or only whitespace. The message names the
    field.
    """


async def resolve_text_body(field: str, value: TemplatedText | str | None, *, locale: str | None = None) -> str | None:
    """Resolve an authored text body — the ``TemplatedText | str`` union — to its text.

    A ``str`` is the text itself; a :class:`~tai42_contract.template.TemplatedText` is RENDERED
    through the resource manager (its stored ``id`` or inline ``content``). A text the manager cannot
    render as authored raises :class:`TextBodyError` naming ``field`` (and the stored ``id`` when it
    is by-id), and so does an empty or whitespace-only text: the unset state is ``None``. Any other
    failure of the render propagates as itself. ``None`` resolves to ``None``. ``locale`` selects a
    stored resource's locale variant.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = await render_templated_text(value, locale)
        except TemplateRenderError as exc:
            raise TextBodyError(f"{field} {exc}") from exc
    if not text.strip():
        raise TextBodyError(f"{field} is empty; send null to clear it")
    return text
