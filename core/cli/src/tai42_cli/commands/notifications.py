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

from tai42_cli.commands._common import app_context, covers, emit_records, emit_result
from tai42_cli.commands._notification_body import build_notify_body

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
    emit_records(ctx_obj, data, route=("GET", "/api/notifications"))


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
    body = build_notify_body(
        message, channel, recipient, media, template, options, sections, location, header, footer, schema
    )
    with ctx_obj.client() as client:
        data = client.post("/api/notifications", json=body)
    emit_result(ctx_obj, data)
