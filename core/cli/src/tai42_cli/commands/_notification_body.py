"""Assemble the ``POST /api/notifications`` request body, validating each
rich-send field into its contract model."""

from __future__ import annotations

from typing import Any

import typer
from pydantic import BaseModel, TypeAdapter, ValidationError
from tai42_contract.channels import ChannelTemplate, Option, OptionSection
from tai42_contract.interactions.models import LocationElement, MediaItem

from tai42_cli.commands._common import compact, parse_json_value

_MEDIA_ADAPTER = TypeAdapter(list[MediaItem])
_OPTIONS_ADAPTER = TypeAdapter(list[Option])
_SECTIONS_ADAPTER = TypeAdapter(list[OptionSection])


def _reject_unknown_keys(raw: object, model: type[BaseModel], *, param_hint: str) -> dict[str, Any]:
    """Refuse a JSON object carrying keys the contract model does not declare.

    The contract's channel models do not set ``extra="forbid"``, so an unknown key
    would be silently DROPPED by ``model_validate`` and the caller would never learn
    their input was ignored. The CLI guards its own seam: it validates the raw
    object's keys against the model's fields FIRST and rejects any stray key loudly,
    naming the accepted keys.
    """
    if not isinstance(raw, dict):
        raise typer.BadParameter("must be a JSON object", param_hint=param_hint)
    unknown = sorted(set(raw) - set(model.model_fields))
    if unknown:
        allowed = ", ".join(sorted(model.model_fields))
        raise typer.BadParameter(
            f"unknown key(s): {', '.join(unknown)}; accepted keys are: {allowed}",
            param_hint=param_hint,
        )
    return raw


def _list_field(raw: str, adapter: TypeAdapter[Any], label: str, *, param_hint: str) -> list[Any]:
    """Parse one JSON-array option, validate it through ``adapter``, and return the
    json-dumped list; a validation error raises ``invalid {label}`` loudly."""
    parsed = parse_json_value(raw, param_hint=param_hint)
    try:
        items = adapter.validate_python(parsed)
    except ValidationError as exc:
        raise typer.BadParameter(f"invalid {label}: {exc}", param_hint=param_hint) from exc
    return adapter.dump_python(items, mode="json")


def _model_field(raw: str, model: type[BaseModel], label: str, *, param_hint: str) -> dict[str, Any]:
    """Parse one JSON-object option, reject unknown keys, validate it into ``model``,
    and return the json-dumped dict; a validation error raises ``invalid {label}``."""
    parsed = parse_json_value(raw, param_hint=param_hint)
    raw_object = _reject_unknown_keys(parsed, model, param_hint=param_hint)
    try:
        validated = model.model_validate(raw_object)
    except ValidationError as exc:
        raise typer.BadParameter(f"invalid {label}: {exc}", param_hint=param_hint) from exc
    return validated.model_dump(mode="json")


def _schema_field(raw: str) -> dict[str, Any]:
    """Parse ``--schema`` as a JSON object (the ask-less form's answer schema);
    the server owns the deeper subset walk, so this shape check is its whole local
    validation."""
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
    ):
        if raw is not None:
            body[key] = _list_field(raw, adapter, label, param_hint=param_hint)
    for key, raw, model, label, param_hint in (
        ("template", template, ChannelTemplate, "template", "--template"),
        ("location", location, LocationElement, "location", "--location"),
        ("header", header, MediaItem, "header", "--header"),
    ):
        if raw is not None:
            body[key] = _model_field(raw, model, label, param_hint=param_hint)
    if schema is not None:
        body["schema"] = _schema_field(schema)
    return body
