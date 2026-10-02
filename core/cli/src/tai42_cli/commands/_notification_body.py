"""Assemble the ``POST /api/notifications`` request body, validating each rich-send field into its contract model."""

from __future__ import annotations

from typing import Any

import typer
from pydantic import BaseModel, TypeAdapter, ValidationError
from tai42_contract.channels import ChannelTemplate, Option, OptionSection
from tai42_contract.interactions.models import FormData, FormPage, LocationElement, MediaItem

from tai42_cli.commands._common import compact, parse_json_value

_MEDIA_ADAPTER = TypeAdapter(list[MediaItem])
_OPTIONS_ADAPTER = TypeAdapter(list[Option])
_SECTIONS_ADAPTER = TypeAdapter(list[OptionSection])
_PAGES_ADAPTER = TypeAdapter(list[FormPage])


def _list_field(raw: str, adapter: TypeAdapter[Any], label: str, *, param_hint: str) -> list[Any]:
    """Parse one JSON-array option, validate it through ``adapter``, and return the json-dumped list.

    A validation error raises ``invalid {label}`` loudly.
    """
    parsed = parse_json_value(raw, param_hint=param_hint)
    try:
        items = adapter.validate_python(parsed)
    except ValidationError as exc:
        raise typer.BadParameter(f"invalid {label}: {exc}", param_hint=param_hint) from exc
    return adapter.dump_python(items, mode="json")


def _model_field(raw: str, model: type[BaseModel], label: str, *, param_hint: str) -> dict[str, Any]:
    """Parse one JSON-object option, validate it into ``model``, and return the json-dumped dict.

    A validation error — including an undeclared key, which every notification-body model
    forbids (``extra="forbid"``) — raises ``invalid {label}`` loudly.
    """
    parsed = parse_json_value(raw, param_hint=param_hint)
    try:
        validated = model.model_validate(parsed)
    except ValidationError as exc:
        raise typer.BadParameter(f"invalid {label}: {exc}", param_hint=param_hint) from exc
    return validated.model_dump(mode="json")


def _schema_field(raw: str) -> dict[str, Any]:
    """Parse ``--schema`` as a JSON object (the ask-less form's answer schema).

    The server owns the deeper subset walk, so this shape check is its whole local validation.
    """
    parsed = parse_json_value(raw, param_hint="--schema")
    if not isinstance(parsed, dict):
        raise typer.BadParameter("invalid schema: must be a JSON object", param_hint="--schema")
    return parsed


def build_notify_body(
    message: str,
    channel: str | None,
    recipient: str | None,
    media: str | None,
    template: str | None,
    options: str | None,
    sections: str | None,
    location: str | None,
    header: str | None,
    footer: str | None,
    schema: str | None,
    data: str | None,
    pages: str | None,
) -> dict[str, object]:
    """Build the ``POST /api/notifications`` body from the ``notify`` command's flags.

    Plain scalars ride through unchanged (an omitted one is dropped); each rich field
    is parsed and validated into its contract model, so a mis-shaped value raises
    before the request leaves the process.
    """
    body: dict[str, object] = compact(
        {"message": message, "channel": channel, "recipient": recipient, "footer": footer}
    )
    for key, raw, adapter, label, param_hint in (
        ("media", media, _MEDIA_ADAPTER, "media item(s)", "--media"),
        ("options", options, _OPTIONS_ADAPTER, "options", "--options"),
        ("sections", sections, _SECTIONS_ADAPTER, "sections", "--sections"),
        ("pages", pages, _PAGES_ADAPTER, "form pages", "--pages"),
    ):
        if raw is not None:
            body[key] = _list_field(raw, adapter, label, param_hint=param_hint)
    for key, raw, model, label, param_hint in (
        ("template", template, ChannelTemplate, "template", "--template"),
        ("location", location, LocationElement, "location", "--location"),
        ("header", header, MediaItem, "header", "--header"),
        ("data", data, FormData, "form data", "--data"),
    ):
        if raw is not None:
            body[key] = _model_field(raw, model, label, param_hint=param_hint)
    if schema is not None:
        body["schema"] = _schema_field(schema)
    return body
