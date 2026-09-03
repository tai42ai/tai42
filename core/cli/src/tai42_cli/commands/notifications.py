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

from typing import Annotated

import typer
from pydantic import TypeAdapter, ValidationError
from tai42_contract.channels import ChannelTemplate
from tai42_contract.interactions.models import MediaItem

from tai42_cli.commands._common import app_context, covers, emit_records, emit_result, parse_json_value

_MEDIA_ADAPTER = TypeAdapter(list[MediaItem])
_OPTIONS_ADAPTER = TypeAdapter(list[str])

app = typer.Typer(
    name="notifications",
    help="Read and send internal notifications.",
    no_args_is_help=True,
)


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
                'JSON object for an out-of-window template send, e.g. \'{"name":"status_update","language":"en_US"}\'.'
            ),
        ),
    ] = None,
    options: Annotated[
        str | None,
        typer.Option(
            "--options",
            help='JSON array of tappable options, e.g. \'["Item A","Item B"]\'.',
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

    ``--media``, ``--template``, ``--options`` and ``--schema`` are JSON strings (the nested
    shapes have
    no flat-flag form). The CLI checks only their shape before the request — a list of
    ``MediaItem`` for ``--media``, ``list[str]`` for ``--options``, a ``ChannelTemplate`` for
    ``--template``, a JSON object for ``--schema`` — so a mis-shaped value raises loudly
    here; the contract's richer rules
    (caps, non-blank, exclusivity, the channel-deliverable form subset) are enforced by the
    server.

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
        body["media"] = [item.model_dump(mode="json") for item in items]
    if template is not None:
        parsed_template = parse_json_value(template, param_hint="--template")
        try:
            model = ChannelTemplate.model_validate(parsed_template)
        except ValidationError as exc:
            raise typer.BadParameter(f"invalid template: {exc}", param_hint="--template") from exc
        body["template"] = model.model_dump(mode="json")
    if options is not None:
        parsed_options = parse_json_value(options, param_hint="--options")
        try:
            option_list = _OPTIONS_ADAPTER.validate_python(parsed_options)
        except ValidationError as exc:
            raise typer.BadParameter(f"invalid options: {exc}", param_hint="--options") from exc
        body["options"] = list(option_list)
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
