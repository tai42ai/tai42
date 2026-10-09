"""``tai connectors`` — manage connector providers and connections.

Thin wrappers over the ``/api/connectors/*`` routes. The browser OAuth callback
(``/api/connectors/oauth/complete``) is not an operator command and is intentionally
not exposed.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from tai42_cli.commands._common import (
    app_context,
    compact,
    covers,
    emit_records,
    emit_result,
    load_json_object_arg,
    read_file_or_stdin,
    seg,
)

app = typer.Typer(
    name="connectors",
    help="Manage connector providers and connections.",
    no_args_is_help=True,
)

_SUB_SERVICE_HELP = "An enabled sub-service (repeatable)."
_RETURN_URL_HELP = "Where to return after the flow completes."
_CONFIG_FILE_HELP = (
    "Read the provider config values from a file, or from stdin when the path is '-', which keeps provider secrets "
    "off the command line (a value on argv leaks via ps and shell history). Mutually exclusive with --config."
)


@app.command("providers")
@covers(("GET", "/api/connectors/providers"))
def providers(ctx: typer.Context) -> None:
    """List the available connector providers.

    Example: ``tai connectors providers``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/connectors/providers")
    # /api/connectors/providers returns two lists (providers + categories); this
    # renders the providers list, so its columns are not model-derivable.
    emit_records(
        ctx_obj,
        data,
        ["id", "display_name", "kind", "category", "removable", "client_source"],
        items_key="providers",
    )


@app.command("connections")
@covers(("GET", "/api/connectors/connections"))
def connections(ctx: typer.Context) -> None:
    """List the installed connections (no secrets).

    Example: ``tai connectors connections``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/connectors/connections")
    if not ctx_obj.json_output and isinstance(data, dict):
        data = {**data, "items": [_with_needs(item) for item in data.get("items") or []]}
    emit_records(ctx_obj, data, route=("GET", "/api/connectors/connections"))


def _with_needs(item: Any) -> Any:
    """A connection row whose ``scopes_wanted`` reads ``needs: a, b`` (empty when it needs nothing)."""
    if not isinstance(item, dict):
        return item
    wanted = item.get("scopes_wanted") or []
    return {**item, "scopes_wanted": f"needs: {', '.join(wanted)}" if wanted else ""}


@app.command("get")
@covers(("GET", "/api/connectors/connections/{connection_id}"))
def get_connection(ctx: typer.Context, connection_id: Annotated[str, typer.Argument(help="Connection id.")]) -> None:
    """Get one connection.

    Example: ``tai connectors get conn_123``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get(f"/api/connectors/connections/{seg(connection_id)}")
    emit_result(ctx_obj, data)


@app.command("connect")
@covers(("POST", "/api/connectors/connections/start"))
def connect(
    ctx: typer.Context,
    provider: Annotated[str, typer.Argument(help="Provider id.")],
    alias: Annotated[str, typer.Option("--alias", help="A unique alias for the new connection.")],
    sub_service: Annotated[list[str], typer.Option("--sub-service", help=_SUB_SERVICE_HELP)],
    config_values: Annotated[
        str | None, typer.Option("--config", help="Provider config values as a JSON object.")
    ] = None,
    config_file: Annotated[str | None, typer.Option("--config-file", help=_CONFIG_FILE_HELP)] = None,
    return_url: Annotated[str, typer.Option("--return-url", help=_RETURN_URL_HELP)] = "/connectors",
) -> None:
    """Start a connection flow; prints the authorize URL (or the created connection).

    Example: ``tai connectors connect google --alias work --sub-service gmail``
    """
    ctx_obj = app_context(ctx)
    config = load_json_object_arg(config_values, config_file, param_hint="--config", file_param_hint="--config-file")
    body: dict = {
        "provider_id": provider,
        "alias": alias,
        "enabled_sub_services": list(sub_service),
        "config_values": config if config is not None else {},
        "return_url": return_url,
    }
    with ctx_obj.client() as client:
        data = client.post("/api/connectors/connections/start", json=body)
    emit_result(ctx_obj, data)


@app.command("disconnect")
@covers(("DELETE", "/api/connectors/connections/{connection_id}"))
def disconnect(ctx: typer.Context, connection_id: Annotated[str, typer.Argument(help="Connection id.")]) -> None:
    """Disconnect (delete) a connection.

    Example: ``tai connectors disconnect conn_123``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/connectors/connections/{seg(connection_id)}")
    emit_result(ctx_obj, data)


@app.command("reconnect")
@covers(("POST", "/api/connectors/connections/{connection_id}/reconnect"))
def reconnect(
    ctx: typer.Context,
    connection_id: Annotated[str, typer.Argument(help="Connection id.")],
    sub_service: Annotated[list[str], typer.Option("--sub-service", help=_SUB_SERVICE_HELP)],
    return_url: Annotated[str, typer.Option("--return-url", help=_RETURN_URL_HELP)] = "/connectors",
) -> None:
    """Start a reconnect (re-authorize) flow for an existing connection.

    Example: ``tai connectors reconnect conn_123 --sub-service gmail``
    """
    ctx_obj = app_context(ctx)
    body = {"enabled_sub_services": list(sub_service), "return_url": return_url}
    with ctx_obj.client() as client:
        data = client.post(f"/api/connectors/connections/{seg(connection_id)}/reconnect", json=body)
    emit_result(ctx_obj, data)


@app.command("sub-services")
@covers(("PATCH", "/api/connectors/connections/{connection_id}/sub-services"))
def patch_sub_services(
    ctx: typer.Context,
    connection_id: Annotated[str, typer.Argument(help="Connection id.")],
    sub_service: Annotated[
        list[str], typer.Option("--sub-service", help="The desired enabled sub-service (repeatable).")
    ],
    return_url: Annotated[
        str, typer.Option("--return-url", help="Where to return if consent is required.")
    ] = "/connectors",
) -> None:
    """Change a connection's enabled sub-services.

    Example: ``tai connectors sub-services conn_123 --sub-service gmail --sub-service calendar``
    """
    ctx_obj = app_context(ctx)
    body = {"enabled_sub_services": list(sub_service), "return_url": return_url}
    with ctx_obj.client() as client:
        data = client.patch(f"/api/connectors/connections/{seg(connection_id)}/sub-services", json=body)
    emit_result(ctx_obj, data)


@app.command("reencrypt-tokens")
@covers(("POST", "/api/connectors/tokens/reencrypt"))
def reencrypt_tokens(ctx: typer.Context) -> None:
    """Re-encrypt every stored token and client credential under the current CONNECTORS_KEK.

    Run after rotating CONNECTORS_KEK (old key moved to CONNECTORS_KEK_PREVIOUS) to
    converge every connection token blob and every stored OAuth client blob onto the
    new key so the previous key can be retired.

    Example: ``tai connectors reencrypt-tokens``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post("/api/connectors/tokens/reencrypt")
    emit_result(ctx_obj, data)


# -- the provider door (administrators only) ---------------------------------


@app.command("discover")
@covers(("POST", "/api/connectors/providers/discover"))
def discover(
    ctx: typer.Context,
    url: Annotated[str | None, typer.Argument(help="The address of the MCP server to look up.")] = None,
    provider: Annotated[
        str | None, typer.Option("--provider", help="Look up the server of this discovered provider instead.")
    ] = None,
) -> None:
    """Look up an MCP server's sign-in: every sign-in service it lists and the redirect URI to register.

    Example: ``tai connectors discover https://mcp.example.com/mcp``
    """
    if (url is None) == (provider is None):
        raise typer.BadParameter("give exactly one of URL or --provider")
    ctx_obj = app_context(ctx)
    body = {"url": url} if url is not None else {"provider_id": provider}
    with ctx_obj.client() as client:
        data = client.post("/api/connectors/providers/discover", json=body)
    if not ctx_obj.json_output and isinstance(data, dict) and data.get("redirect_uri") is None:
        data = {
            **data,
            "redirect_uri": "(none: no sign-in can start from this address; its origin is not on the "
            "redirect allow-list)",
        }
    emit_result(ctx_obj, data)


@app.command("add-server")
@covers(("POST", "/api/connectors/providers"))
def add_server(
    ctx: typer.Context,
    url: Annotated[str, typer.Argument(help="The address of the MCP server to add.")],
    provider_id: Annotated[
        str | None, typer.Option("--id", help="The provider id (default: suggested from the host).")
    ] = None,
    name: Annotated[str | None, typer.Option("--name", help="The display name.")] = None,
    description: Annotated[str | None, typer.Option("--description", help="A description.")] = None,
    icon_url: Annotated[str | None, typer.Option("--icon-url", help="An https URL of an icon.")] = None,
    category: Annotated[str | None, typer.Option("--category", help="The catalog category (default: other).")] = None,
    scope: Annotated[
        list[str] | None,
        typer.Option("--scope", help="A scope to request (repeatable; default: what the server lists)."),
    ] = None,
) -> None:
    """Add an MCP server that signs in with OAuth as a connector provider.

    Example: ``tai connectors add-server https://mcp.example.com/mcp --name "Team tools"``
    """
    ctx_obj = app_context(ctx)
    body = compact(
        {
            "url": url,
            "id": provider_id,
            "display_name": name,
            "description": description,
            "icon_url": icon_url,
            "category": category,
            "scopes": list(scope) if scope else None,
        }
    )
    with ctx_obj.client() as client:
        data = client.post("/api/connectors/providers", json=body)
    emit_result(ctx_obj, data)
    if not ctx_obj.json_output:
        added = data.get("provider", {}).get("id") if isinstance(data, dict) else None
        typer.echo(f"Next: tai connectors connect {added} --alias <alias> --sub-service default")


@app.command("remove-server")
@covers(("DELETE", "/api/connectors/providers/{provider_id}"))
def remove_server(ctx: typer.Context, provider_id: Annotated[str, typer.Argument(help="Provider id.")]) -> None:
    """Remove a provider added by its address, and its stored client credentials.

    Example: ``tai connectors remove-server mcp_example_com``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/connectors/providers/{seg(provider_id)}")
    emit_result(ctx_obj, data)


@app.command("set-client")
@covers(("PUT", "/api/connectors/providers/{provider_id}/client"))
def set_client(
    ctx: typer.Context,
    provider_id: Annotated[str, typer.Argument(help="Provider id.")],
    client_id: Annotated[str, typer.Option("--client-id", help="The client id of the app registered by hand.")],
    client_secret_file: Annotated[
        str | None,
        typer.Option(
            "--client-secret-file",
            help="Read the client secret from a file, or from stdin when the path is '-' (never an argument: a "
            "value on argv leaks via ps and shell history). Leave out for an app without a secret.",
        ),
    ] = None,
    auth_method: Annotated[
        str | None,
        typer.Option(
            "--auth-method",
            help="client_secret_post, client_secret_basic or none (default: chosen from the sign-in service).",
        ),
    ] = None,
    issuer: Annotated[
        str | None,
        typer.Option("--issuer", help="The sign-in service the app belongs to; the secret is sent to no other."),
    ] = None,
) -> None:
    """Set the client credentials of an app registered by hand for a discovered provider.

    Example: ``tai connectors set-client mcp_example_com --client-id app --client-secret-file - --issuer URL``
    """
    secret: str | None = None
    if client_secret_file is not None:
        secret = read_file_or_stdin(client_secret_file, param_hint="--client-secret-file").strip()
        if not secret:
            raise typer.BadParameter("the client secret is empty", param_hint="--client-secret-file")
    ctx_obj = app_context(ctx)
    body = compact(
        {
            "client_id": client_id,
            "client_secret": secret,
            "token_endpoint_auth_method": auth_method,
            "issuer": issuer,
        }
    )
    with ctx_obj.client() as client:
        data = client.put(f"/api/connectors/providers/{seg(provider_id)}/client", json=body)
    emit_result(ctx_obj, data)
