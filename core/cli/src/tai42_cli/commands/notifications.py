"""``tai notifications`` — read the internal notifications feed and send a
notification.

Thin wrappers over the authed ``/api/notifications`` routes: ``list`` reads the
deployment's internal notifications feed — channel-less sends plus any
audience-addressed notification, recorded even when a channel also delivers it —
newest-first (the feed is a bounded ring buffer written by the sink); ``notify``
sends a human a one-way, fire-and-forget message on a named channel or (channel
omitted) into the sink.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
from pydantic import BaseModel, TypeAdapter, ValidationError
from tai42_contract.channels import ChannelTemplate, Option, OptionSection
from tai42_contract.interactions.models import LocationElement, MediaItem

from tai42_cli.commands._common import app_context, covers, emit_records, emit_result, parse_json_value

_MEDIA_ADAPTER = TypeAdapter(list[MediaItem])
_OPTIONS_ADAPTER = TypeAdapter(list[Option])
_SECTIONS_ADAPTER = TypeAdapter(list[OptionSection])

app = typer.Typer(
    name="notifications",
    help="Read and send internal notifications.",
    no_args_is_help=True,
)


def _reject_unknown_keys(raw: object, model: type[BaseModel], *, param_hint: str) -> dict[str, Any]:
    """Refuse a JSON object carrying keys the contract model does not declare.

    The contract's channel models do not set ``extra="forbid"``, so an unknown key
    (e.g. the pre-7 template ``parameters``) would be silently DROPPED by
    ``model_validate`` and the caller would never learn their input was ignored. The
    CLI guards its own seam: it validates the raw object's keys against the model's
    fields FIRST and rejects any stray key loudly, naming the accepted keys.
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


@app.command("list")
@covers(("GET", "/api/notifications"))
def list_notifications(ctx: typer.Context) -> None:
    """List the internal notifications, newest-first.

    Example: ``tai notifications list``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/notifications")
    emit_records(ctx_obj, data, ["message", "recipient", "created_at"], items_key="notifications")


@app.command("notify")
@covers(("POST", "/api/notifications"))
def notify(
    ctx: typer.Context,
    message: Annotated[str, typer.Argument(help="The notification text shown to the human.")],
    channel: Annotated[
        str | None,
        typer.Option("--channel", help="Named channel to send on; omit to record to the internal sink."),
    ] = None,
    recipient: Annotated[
        str | None,
        typer.Option("--recipient", help="Optional per-call address (chat id, phone number, ...)."),
    ] = None,
    media: Annotated[
        str | None,
        typer.Option(
            "--media",
            help='JSON array of display-media items sent WITH the message, e.g. \'[{"kind":"image","url":"https://…"}]\'.',
        ),
    ] = None,
    template: Annotated[
        str | None,
        typer.Option(
            "--template",
            help=(
                "JSON object for an out-of-window template send with contract-7 components — "
                "``name``, ``language`` and optional ``header_media`` / ``body_parameters`` / "
                '``buttons``, e.g. \'{"name":"status_update","language":"en_US",'
                '"body_parameters":["A-42"]}\'.'
            ),
        ),
    ] = None,
    options: Annotated[
        str | None,
        typer.Option(
            "--options",
            help=(
                "JSON array of tappable options — each a reply "
                '(``{"kind":"reply","text":"Yes","description":"…","id":"…"}``, description/id '
                'optional) or a link (``{"kind":"link","label":"Docs","url":"https://…"}``), e.g. '
                '\'[{"kind":"reply","text":"Yes"},{"kind":"link","label":"Docs","url":"https://x/d"}]\'.'
            ),
        ),
    ] = None,
    sections: Annotated[
        str | None,
        typer.Option(
            "--sections",
            help=(
                "JSON array of titled option sections (the sectioned alternative to --options), each "
                '``{"title":"…","rows":[{"kind":"reply","text":"…"}]}``, e.g. '
                '\'[{"title":"Fruit","rows":[{"kind":"reply","text":"Apple"}]}]\'.'
            ),
        ),
    ] = None,
    location: Annotated[
        str | None,
        typer.Option(
            "--location",
            help=(
                "JSON object for a shared map pin, "
                '``{"latitude":51.5,"longitude":-0.12,"name":"…","address":"…"}`` (name/address '
                "optional)."
            ),
        ),
    ] = None,
    header: Annotated[
        str | None,
        typer.Option(
            "--header",
            help=(
                "JSON object for a single display-media header above an interactive message "
                '(requires --options or --sections), e.g. \'{"kind":"image","url":"https://…"}\'.'
            ),
        ),
    ] = None,
    footer: Annotated[
        str | None,
        typer.Option(
            "--footer",
            help="Short trailing line under an interactive message (requires --options or --sections).",
        ),
    ] = None,
    schema: Annotated[
        str | None,
        typer.Option(
            "--schema",
            help=(
                "JSON object holding an ask-less form's answer schema (channel-only; the message is the "
                'form\'s prompt), e.g. \'{"type":"object","properties":{"name":{"type":"string"}}}\'.'
            ),
        ),
    ] = None,
) -> None:
    """Send a human a one-way, fire-and-forget notification.

    The rich-send forms are JSON strings validated into their contract models before the
    request — a list of ``MediaItem`` for ``--media``, a discriminated ``Option`` list
    (reply/link) for ``--options``, an ``OptionSection`` list for ``--sections``, a
    ``ChannelTemplate`` for ``--template``, a ``LocationElement`` for ``--location``, a
    ``MediaItem`` for ``--header``, a JSON object for ``--schema`` — so a mis-shaped value
    (including an unknown template/location/header key, which the contract would otherwise
    silently drop) raises loudly here; the contract's cross-field rules (caps, non-blank,
    the options-XOR-sections choice surface, header/footer requiring a choice surface,
    template exclusivity, the channel-deliverable form subset) are enforced by the server.

    Example: ``tai notifications notify "Deploy finished" --channel telegram``
    """
    ctx_obj = app_context(ctx)
    body: dict[str, object] = {"message": message}
    if channel is not None:
        body["channel"] = channel
    if recipient is not None:
        body["recipient"] = recipient
    if media is not None:
        parsed_media = parse_json_value(media, param_hint="--media")
        try:
            items = _MEDIA_ADAPTER.validate_python(parsed_media)
        except ValidationError as exc:
            raise typer.BadParameter(f"invalid media item(s): {exc}", param_hint="--media") from exc
        body["media"] = _MEDIA_ADAPTER.dump_python(items, mode="json")
    if template is not None:
        parsed_template = parse_json_value(template, param_hint="--template")
        raw_template = _reject_unknown_keys(parsed_template, ChannelTemplate, param_hint="--template")
        try:
            model = ChannelTemplate.model_validate(raw_template)
        except ValidationError as exc:
            raise typer.BadParameter(f"invalid template: {exc}", param_hint="--template") from exc
        body["template"] = model.model_dump(mode="json")
    if options is not None:
        parsed_options = parse_json_value(options, param_hint="--options")
        try:
            option_list = _OPTIONS_ADAPTER.validate_python(parsed_options)
        except ValidationError as exc:
            raise typer.BadParameter(f"invalid options: {exc}", param_hint="--options") from exc
        body["options"] = _OPTIONS_ADAPTER.dump_python(option_list, mode="json")
    if sections is not None:
        parsed_sections = parse_json_value(sections, param_hint="--sections")
        try:
            section_list = _SECTIONS_ADAPTER.validate_python(parsed_sections)
        except ValidationError as exc:
            raise typer.BadParameter(f"invalid sections: {exc}", param_hint="--sections") from exc
        body["sections"] = _SECTIONS_ADAPTER.dump_python(section_list, mode="json")
    if location is not None:
        parsed_location = parse_json_value(location, param_hint="--location")
        raw_location = _reject_unknown_keys(parsed_location, LocationElement, param_hint="--location")
        try:
            location_model = LocationElement.model_validate(raw_location)
        except ValidationError as exc:
            raise typer.BadParameter(f"invalid location: {exc}", param_hint="--location") from exc
        body["location"] = location_model.model_dump(mode="json")
    if header is not None:
        parsed_header = parse_json_value(header, param_hint="--header")
        raw_header = _reject_unknown_keys(parsed_header, MediaItem, param_hint="--header")
        try:
            header_model = MediaItem.model_validate(raw_header)
        except ValidationError as exc:
            raise typer.BadParameter(f"invalid header: {exc}", param_hint="--header") from exc
        body["header"] = header_model.model_dump(mode="json")
    if footer is not None:
        body["footer"] = footer
    if schema is not None:
        parsed_schema = parse_json_value(schema, param_hint="--schema")
        if not isinstance(parsed_schema, dict):
            # The form's answer schema is a JSON object by contract; the server owns the
            # deeper subset walk, this shape check is the flag's whole local validation.
            raise typer.BadParameter("invalid schema: must be a JSON object", param_hint="--schema")
        body["schema"] = parsed_schema
    with ctx_obj.client() as client:
        data = client.post("/api/notifications", json=body)
    emit_result(ctx_obj, data)
