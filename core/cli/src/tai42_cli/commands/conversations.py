"""``tai conversations`` — manage the conversation routing table.

Thin wrappers over the authed ``/api/conversations*`` management routes. The channel
door and the authed message door are not operator routes and are not exposed here.
"""

from __future__ import annotations

from typing import Annotated

import typer

from tai42_cli.commands._common import (
    app_context,
    covers,
    emit_records,
    emit_result,
)

app = typer.Typer(
    name="conversations",
    help="Manage conversation routes.",
    no_args_is_help=True,
)


@app.command("list")
@covers(("GET", "/api/conversations"))
def list_routes(ctx: typer.Context) -> None:
    """List conversation routes (each row's callback_secret is withheld).

    Example: ``tai conversations list``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/conversations")
    emit_records(
        ctx_obj,
        data,
        ["route_name", "door", "target_kind", "target_name", "execution_key", "channel", "our_identity"],
        items_key="items",
    )


@app.command("get")
@covers(("GET", "/api/conversations/{route_name}"))
def get_route(ctx: typer.Context, route_name: Annotated[str, typer.Argument(help="Route name (slug).")]) -> None:
    """Show one conversation route by name (its callback_secret is withheld).

    Example: ``tai conversations get chat-line``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get(f"/api/conversations/{route_name}")
    emit_result(ctx_obj, data)


@app.command("create")
@covers(("POST", "/api/conversations/{route_name}"))
def create_route(
    ctx: typer.Context,
    route_name: Annotated[str, typer.Argument(help="Route name — a slug [a-z0-9-]+ (the thread-key handle).")],
    door: Annotated[str, typer.Option("--door", help="Inbound door: 'api' or 'channel'.")],
    target_name: Annotated[str, typer.Option("--target-name", help="The agent or tool the turn runs (must exist).")],
    execution_key: Annotated[
        str,
        typer.Option(
            "--execution-key",
            help="The api-key user id the turn runs AS; you must own it (or be admin), tokenless-evaluable.",
        ),
    ],
    target_kind: Annotated[str, typer.Option("--target-kind", help="What the turn runs: 'agent' or 'tool'.")] = "agent",
    payload_expr: Annotated[
        str | None,
        typer.Option("--payload-expr", help="target-kind=tool: jq mapping the inbound message to the tool kwargs."),
    ] = None,
    reply_expr: Annotated[
        str | None,
        typer.Option("--reply-expr", help="target-kind=tool: jq mapping the tool result to the reply."),
    ] = None,
    initial_mode: Annotated[
        str,
        typer.Option("--initial-mode", help="Default control mode when a thread has no override: 'agent' or 'manual'."),
    ] = "agent",
    channel: Annotated[
        str | None, typer.Option("--channel", help="door=channel: the channel registry name (e.g. twilio).")
    ] = None,
    our_identity: Annotated[
        str | None, typer.Option("--identity", help="door=channel: the medium address we are texted at.")
    ] = None,
    callback_url: Annotated[
        str | None, typer.Option("--callback-url", help="door=api: the https answer-delivery URL.")
    ] = None,
) -> None:
    """Create or replace a conversation route.

    An UPSERT — a name that already exists is REPLACED, rebinding its ``execution_key``
    along with everything else (``created`` is ``false`` for a replace). A ``door=api``
    route's ``callback_secret`` is minted server-side and shown ONCE in the result; it
    signs the delivery callback and is never re-readable. There is no check that you can
    run the target — the execution key's live grants bound the turn. A ``tool`` target may
    map the message to the tool kwargs (``--payload-expr``) and the result to the reply
    (``--reply-expr``); a tool reply of null/blank sends nothing.

    Example: ``tai conversations create chat-line --door channel --target-name relay \\
    --execution-key svc --channel twilio --identity +15550001111``
    """
    ctx_obj = app_context(ctx)
    body: dict = {
        "door": door,
        "target_kind": target_kind,
        "target_name": target_name,
        "execution_key": execution_key,
        "initial_mode": initial_mode,
    }
    if payload_expr is not None:
        body["payload_expr"] = payload_expr
    if reply_expr is not None:
        body["reply_expr"] = reply_expr
    if channel is not None:
        body["channel"] = channel
    if our_identity is not None:
        body["our_identity"] = our_identity
    if callback_url is not None:
        body["callback_url"] = callback_url
    with ctx_obj.client() as client:
        data = client.post(f"/api/conversations/{route_name}", json=body)
    emit_result(ctx_obj, data)


@app.command("delete")
@covers(("DELETE", "/api/conversations/{route_name}"))
def delete_route(ctx: typer.Context, route_name: Annotated[str, typer.Argument(help="Route name (slug).")]) -> None:
    """Delete a conversation route by name.

    Example: ``tai conversations delete chat-line``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/conversations/{route_name}")
    emit_result(ctx_obj, data)


@app.command("delete-thread")
@covers(("DELETE", "/api/conversations/{route_name}/thread"))
def delete_thread(
    ctx: typer.Context,
    route_name: Annotated[str, typer.Argument(help="Route name (slug).")],
    thread_id: Annotated[str, typer.Argument(help="Thread id (e.g. bridge:chat:+15550001111).")],
) -> None:
    """Forget one conversation thread — its agent checkpoint, its answer records and its
    thread indexes — so a later message on the same address starts a fresh memory.

    Forgetting is absolute: a valid id on its own route always succeeds, reporting
    ``removed: 0`` when nothing was left to clear (an aged-out or never-seen thread), never a
    404 — the agent memory is forgotten regardless. A route-keyed id must carry the route's
    ``bridge:<route_name>:`` prefix, else it is refused (400); a person thread not on the named
    route is a 404. A turn in flight on the thread is refused (409, retry once it drains). The
    same write grant that creates a route lets you delete routes and forget threads.

    The thread id rides the query string, so an api-door id — which carries a percent-encoded
    principal — reaches the door spelled exactly as the listing showed it.

    Example: ``tai conversations delete-thread chat-line bridge:chat-line:+15550001111``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/conversations/{route_name}/thread", params={"thread_id": thread_id})
    emit_result(ctx_obj, data)


@app.command("send")
@covers(("POST", "/api/conversations/{route_name}/thread/messages"))
def send_message(
    ctx: typer.Context,
    route_name: Annotated[str, typer.Argument(help="Route name (slug).")],
    thread_id: Annotated[str, typer.Argument(help="Thread id (e.g. bridge:chat-line:+15550001111).")],
    text: Annotated[str, typer.Option("--text", help="The message to send into the thread.")],
    address: Annotated[
        str | None,
        typer.Option("--address", help="Send target on a linked person's thread (one of the person's addresses)."),
    ] = None,
) -> None:
    """Send a message BY HAND into one thread, delivered as the route identity — no turn runs.

    Allowed in either mode and it never flips the mode. A route-keyed id must carry the
    route's ``bridge:<route_name>:`` prefix (else 400); a person thread not on the named route
    is a 404. ``--address`` picks the send target on a linked person's aggregated thread and
    must be one of the person's addresses; without it the target is the thread's newest
    record, and an empty person thread with no ``--address`` is a 400. A target that keeps
    thread memory gets the message appended to that memory as an assistant reply; a target
    with no thread memory skips it.

    The thread id rides the body, so an api-door id — which carries a percent-encoded
    principal — reaches the door spelled exactly as the listing showed it.

    Example: ``tai conversations send chat-line bridge:chat-line:+15550001111 --text 'On it.'``
    """
    ctx_obj = app_context(ctx)
    body: dict = {"thread_id": thread_id, "text": text}
    if address is not None:
        body["address"] = address
    with ctx_obj.client() as client:
        data = client.post(f"/api/conversations/{route_name}/thread/messages", json=body)
    emit_result(ctx_obj, data)


@app.command("mode-get")
@covers(("GET", "/api/conversations/{route_name}/thread/mode"))
def get_mode(
    ctx: typer.Context,
    route_name: Annotated[str, typer.Argument(help="Route name (slug).")],
    thread_id: Annotated[str, typer.Argument(help="Thread id (e.g. bridge:chat-line:+15550001111).")],
) -> None:
    """Show a thread's control mode and where it comes from (a per-thread override, or the
    route's default).

    Example: ``tai conversations mode-get chat-line bridge:chat-line:+15550001111``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get(f"/api/conversations/{route_name}/thread/mode", params={"thread_id": thread_id})
    emit_result(ctx_obj, data)


@app.command("mode-set")
@covers(("PUT", "/api/conversations/{route_name}/thread/mode"))
def set_mode(
    ctx: typer.Context,
    route_name: Annotated[str, typer.Argument(help="Route name (slug).")],
    thread_id: Annotated[str, typer.Argument(help="Thread id (e.g. bridge:chat-line:+15550001111).")],
    mode: Annotated[str, typer.Argument(help="Control mode: 'agent' or 'manual'.")],
) -> None:
    """Set a thread's control mode override.

    ``agent`` runs the target turn on the next inbound message; ``manual`` suppresses it so an
    operator answers by hand, while platform control turns (pairing, greeting) still run. The
    same write grant that forgets threads sets a thread's mode.

    Example: ``tai conversations mode-set chat-line bridge:chat-line:+15550001111 manual``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.put(f"/api/conversations/{route_name}/thread/mode", json={"thread_id": thread_id, "mode": mode})
    emit_result(ctx_obj, data)


@app.command("get-message")
@covers(("GET", "/api/conversations/{route_name}/messages/{message_id}"))
def get_message(
    ctx: typer.Context,
    route_name: Annotated[str, typer.Argument(help="Route name (slug).")],
    message_id: Annotated[str, typer.Argument(help="Answer record message id (uuid4).")],
) -> None:
    """Read one conversation answer record. Any holder of the conversations read grant reads
    any record on the route, whichever door it arrived through; a non-admin caller gets the
    caller-safe projection, with the internal error detail withheld.

    Example: ``tai conversations get-message chat-line 4f1c...``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get(f"/api/conversations/{route_name}/messages/{message_id}")
    emit_result(ctx_obj, data)


@app.command("threads")
@covers(("GET", "/api/conversations/{route_name}/threads"))
def list_threads(
    ctx: typer.Context,
    route_name: Annotated[str, typer.Argument(help="Route name (slug).")],
    page: Annotated[int, typer.Option("--page", help="Page number, from 1.")] = 1,
    page_size: Annotated[int, typer.Option("--page-size", help="Threads per page (capped server-side).")] = 50,
) -> None:
    """List a route's conversation threads, newest activity first (admin only).

    Example: ``tai conversations threads chat-line --page 1``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get(f"/api/conversations/{route_name}/threads", params={"page": page, "pageSize": page_size})
    emit_records(
        ctx_obj,
        data,
        ["thread_id", "client_address", "last_activity_at", "message_count", "last_delivery_status"],
        items_key="items",
    )


@app.command("transcript")
@covers(("GET", "/api/conversations/{route_name}/transcript"))
def get_transcript(
    ctx: typer.Context,
    route_name: Annotated[str, typer.Argument(help="Route name (slug).")],
    thread_id: Annotated[str, typer.Argument(help="Thread id (e.g. bridge:chat-line:+15550001111).")],
    page: Annotated[int, typer.Option("--page", help="Page number, from 1.")] = 1,
    page_size: Annotated[int, typer.Option("--page-size", help="Records per page (capped server-side).")] = 50,
    order: Annotated[str, typer.Option("--order", help="Record order: 'asc' (oldest first) or 'desc'.")] = "asc",
) -> None:
    """Read one thread's transcript. Any holder of the conversations read grant reads any
    thread's transcript on the route; an unknown thread or route is a plain 404.

    ``--order asc`` (the default) reads oldest first; ``--order desc`` reads newest first,
    so page 1 always holds the latest messages — the order a live tail wants. The window
    pages that order from its own end.

    The thread id rides the query string, so an api-door id — which carries a
    percent-encoded principal — reaches the door spelled exactly as the listing showed it.

    Example: ``tai conversations transcript chat-line bridge:chat-line:+15550001111``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get(
            f"/api/conversations/{route_name}/transcript",
            params={"thread_id": thread_id, "page": page, "pageSize": page_size, "order": order},
        )
    emit_records(
        ctx_obj,
        data,
        ["created_at", "message_id", "inbound_text", "answer", "answer_status", "delivery_status"],
        items_key="items",
    )


@app.command("failed")
@covers(("GET", "/api/conversations/messages/failed"))
def list_failed(ctx: typer.Context) -> None:
    """List answer records whose delivery ended failed (admin only).

    Example: ``tai conversations failed``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/conversations/messages/failed")
    emit_records(
        ctx_obj,
        data,
        ["message_id", "route_name", "door", "client_address", "answer_status", "attempts"],
        items_key="items",
    )


@app.command("config-list")
@covers(("GET", "/api/conversation-configs"))
def list_configs(ctx: typer.Context) -> None:
    """List the per-target conversation configs (multichannel opt-in + first-contact greeting).

    Example: ``tai conversations config-list``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/conversation-configs")
    emit_records(
        ctx_obj,
        data,
        ["target_kind", "target_name", "multichannel", "greeting_template"],
        items_key="items",
    )


@app.command("config-get")
@covers(("GET", "/api/conversation-configs/{target_kind}/{target_name}"))
def get_config(
    ctx: typer.Context,
    target_kind: Annotated[str, typer.Argument(help="Target kind: 'agent' or 'tool'.")],
    target_name: Annotated[str, typer.Argument(help="The agent or tool name.")],
) -> None:
    """Show one per-target conversation config by (target_kind, target_name).

    Example: ``tai conversations config-get agent assistant``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get(f"/api/conversation-configs/{target_kind}/{target_name}")
    emit_result(ctx_obj, data)


@app.command("config-set")
@covers(("PUT", "/api/conversation-configs/{target_kind}/{target_name}"))
def set_config(
    ctx: typer.Context,
    target_kind: Annotated[str, typer.Argument(help="Target kind: 'agent' or 'tool' (must exist).")],
    target_name: Annotated[str, typer.Argument(help="The agent or tool name (must exist).")],
    multichannel: Annotated[
        bool, typer.Option("--multichannel/--no-multichannel", help="Opt the target into person linking.")
    ] = False,
    greeting_template: Annotated[
        str | None,
        typer.Option(
            "--greeting-template",
            help="First-contact greeting; may reference the {pairing_code} placeholder. Omit for no greeting.",
        ),
    ] = None,
) -> None:
    """Create or replace a per-target conversation config.

    An UPSERT — a config for that (target_kind, target_name) is REPLACED if it exists
    (``created`` is ``false`` for a replace). The target must EXIST. ``--greeting-template``
    may reference at most the ``{pairing_code}`` placeholder; omit it (or pass an empty
    value is refused) for no greeting.

    Example: ``tai conversations config-set agent assistant --multichannel \\
    --greeting-template 'Hi! Pair another channel with {pairing_code}'``
    """
    ctx_obj = app_context(ctx)
    body: dict = {"multichannel": multichannel}
    if greeting_template is not None:
        body["greeting_template"] = greeting_template
    with ctx_obj.client() as client:
        data = client.put(f"/api/conversation-configs/{target_kind}/{target_name}", json=body)
    emit_result(ctx_obj, data)


@app.command("config-delete")
@covers(("DELETE", "/api/conversation-configs/{target_kind}/{target_name}"))
def delete_config(
    ctx: typer.Context,
    target_kind: Annotated[str, typer.Argument(help="Target kind: 'agent' or 'tool'.")],
    target_name: Annotated[str, typer.Argument(help="The agent or tool name.")],
) -> None:
    """Delete a per-target conversation config by (target_kind, target_name).

    Example: ``tai conversations config-delete agent assistant``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/conversation-configs/{target_kind}/{target_name}")
    emit_result(ctx_obj, data)
