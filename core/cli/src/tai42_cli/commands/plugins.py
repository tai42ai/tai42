"""``tai plugins`` — browse and install marketplace plugins.

Thin wrappers over the ten ``/api/marketplace/*`` routes: the six reads
(``search``, ``info``, ``categories``, ``kinds``, ``installed``, ``advisories``)
and the four environment-mutating flows (``install``, ``uninstall``, ``update``,
``upgrade --all``). Each command declares the exact registered route it invokes
via ``@covers`` so the CLI↔route parity gate proves every marketplace route is
reachable from the terminal.

The mutating commands run arbitrary third-party code in the serving environment
by design; typed :class:`ApiError` results (a 409 collision, a 502 registry
failure, a 503 in-progress) surface through the root group's handler, so these
commands add no error handling of their own.
"""

from __future__ import annotations

from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Annotated, Any

import typer
from tai42_contract.plugins import LISTING_SLUG_RE, PluginSpec

from tai42_cli.commands._common import (
    app_context,
    covers,
    echo_stderr,
    emit_records,
    emit_result,
    parse_assignment_arg,
)
from tai42_cli.render import print_json

app = typer.Typer(
    name="plugins",
    help="Browse and install marketplace plugins.",
    no_args_is_help=True,
)


def _split_ref(ref: str) -> tuple[str, str]:
    """Split a ``namespace/name`` ref into its two halves, raising a usage error
    on anything that is not exactly one non-empty namespace and one non-empty
    name separated by a single ``/``."""
    namespace, sep, name = ref.partition("/")
    if not sep or not namespace or not name or "/" in name:
        raise typer.BadParameter("REF must be 'namespace/name'", param_hint="REF")
    return namespace, name


@app.command("search")
@covers(("GET", "/api/marketplace/search"))
def search(
    ctx: typer.Context,
    query: Annotated[str | None, typer.Argument(help="Free-text search query; omit to browse all.")] = None,
    kind: Annotated[str | None, typer.Option("--kind", help="Filter by provides kind (tool, agent, ...).")] = None,
    category: Annotated[str | None, typer.Option("--category", help="Filter by category.")] = None,
    tag: Annotated[list[str] | None, typer.Option("--tag", help="Filter by tag (repeatable).")] = None,
    namespace: Annotated[str | None, typer.Option("--namespace", help="Filter by publisher namespace.")] = None,
    tier: Annotated[str | None, typer.Option("--tier", help="Filter by trust tier.")] = None,
    contract: Annotated[str | None, typer.Option("--contract", help="Filter by compatible contract version.")] = None,
    sort: Annotated[str | None, typer.Option("--sort", help="Sort order (downloads, updated, name).")] = None,
    page: Annotated[int | None, typer.Option("--page", help="Result page (1-based).")] = None,
    page_size: Annotated[int | None, typer.Option("--page-size", help="Results per page.")] = None,
) -> None:
    """Search the marketplace registry, optionally filtered by facets.

    Example: ``tai plugins search uuid --kind tool``
    """
    ctx_obj = app_context(ctx)
    params: dict[str, Any] = {}
    if query is not None:
        params["q"] = query
    if kind is not None:
        params["kind"] = kind
    if category is not None:
        params["category"] = category
    if tag:
        # Each ``--tag`` becomes its own repeated ``tags`` query param — the client
        # encodes a list value as repeated params, never comma-joined.
        params["tags"] = tag
    if namespace is not None:
        params["namespace"] = namespace
    if tier is not None:
        params["tier"] = tier
    if contract is not None:
        params["contract"] = contract
    if sort is not None:
        params["sort"] = sort
    if page is not None:
        params["page"] = page
    if page_size is not None:
        params["page_size"] = page_size
    with ctx_obj.client() as client:
        data = client.get("/api/marketplace/search", params=params)
    emit_records(
        ctx_obj,
        data,
        ["ref", "display_name", "latest_version", "trust_tier", "downloads"],
        items_key="listings",
    )


@app.command("info")
@covers(("GET", "/api/marketplace/plugins/{ns}/{name}"))
def info(ctx: typer.Context, ref: Annotated[str, typer.Argument(help="Plugin ref 'namespace/name'.")]) -> None:
    """Show one listing's detail and its published versions.

    Example: ``tai plugins info tai42/toolbox``
    """
    ctx_obj = app_context(ctx)
    namespace, name = _split_ref(ref)
    with ctx_obj.client() as client:
        data = client.get(f"/api/marketplace/plugins/{namespace}/{name}")
    emit_result(ctx_obj, data)


@app.command("categories")
@covers(("GET", "/api/marketplace/categories"))
def categories(ctx: typer.Context) -> None:
    """List the marketplace's controlled category vocabulary.

    Example: ``tai plugins categories``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/marketplace/categories")
    emit_records(ctx_obj, data, ["category"])


@app.command("kinds")
@covers(("GET", "/api/marketplace/kinds"))
def kinds(ctx: typer.Context) -> None:
    """List the marketplace's controlled item-kind vocabulary.

    Example: ``tai plugins kinds``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/marketplace/kinds")
    emit_records(ctx_obj, data, ["kind"])


@app.command("installed")
@covers(("GET", "/api/marketplace/installed"))
def installed(ctx: typer.Context) -> None:
    """List the installed marketplace plugins, their compat verdicts and update
    availability, and any boot-quarantined plugins.

    Example: ``tai plugins installed``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/marketplace/installed")
    if not ctx_obj.json_output and isinstance(data, dict):
        # The table shows the compat STATUS in its own column (the reason is in
        # the JSON form), and quarantined plugins are printed after the table so
        # a skipped plugin is never invisible in the default view.
        rows = [{**row, "compat": row.get("compat", {}).get("status")} for row in data.get("installed", [])]
        emit_records(
            ctx_obj,
            {"installed": rows},
            ["ref", "version", "latest", "update_available", "incompatible_newer", "compat", "installed_at"],
            items_key="installed",
        )
        for entry in data.get("quarantined", []):
            typer.echo(f"quarantined: {entry.get('name')} — {entry.get('reason')}")
        return
    emit_records(
        ctx_obj,
        data,
        ["ref", "version", "latest", "update_available", "incompatible_newer", "compat", "installed_at"],
        items_key="installed",
    )


def _mount_overrides(mount: list[str] | None) -> dict[str, str]:
    """Parse repeated ``--mount item=base`` pairs into a ``{item: base}`` map,
    rejecting a duplicated item so a base is never silently last-wins."""
    overrides: dict[str, str] = {}
    for pair in mount or []:
        item, base = parse_assignment_arg(pair, param_hint="--mount")
        if item in overrides:
            raise typer.BadParameter(f"item {item!r} was given more than once", param_hint="--mount")
        overrides[item] = base
    return overrides


def _env_map(env: list[str] | None) -> dict[str, str]:
    """Parse repeated ``--env KEY=VALUE`` pairs into a ``{KEY: VALUE}`` map,
    rejecting a duplicated key so a value is never silently last-wins. These are
    the install-time env values the plugin's spec declares (an mcp entry's ``!ENV``
    markers or an oauth connector's client credentials)."""
    values: dict[str, str] = {}
    for pair in env or []:
        key, value = parse_assignment_arg(pair, param_hint="--env")
        if key in values:
            raise typer.BadParameter(f"key {key!r} was given more than once", param_hint="--env")
        values[key] = value
    return values


def _refuse_missing_env(preview: Any, supplied: set[str]) -> None:
    """Exit non-zero, naming the vars, when the server's ``missing_env`` (required
    env not already in the store or process env) still holds names the caller did
    not supply via ``--env``. The CLI is not the env authority — it consumes the
    server's ``missing_env`` and never re-derives from ``GET /api/config/env``."""
    if not isinstance(preview, dict):
        return
    missing = [name for name in preview.get("missing_env") or [] if name not in supplied]
    if missing:
        echo_stderr(f"missing required env (supply with --env): {', '.join(missing)}")
        raise typer.Exit(code=1)


def _render_preview_table(preview: dict[str, Any]) -> None:
    """Print the resolved routes, collisions, public routes, delivery, and required
    env of an install preview as human lines (stdout stays data; every route is
    visible)."""
    delivery = preview.get("delivery")
    if delivery is not None:
        typer.echo(f"delivery: {delivery}")
    required_env = preview.get("required_env") or []
    if required_env:
        typer.echo("required env:")
        for entry in required_env:
            mark = "secret" if entry.get("secret") else "plain"
            typer.echo(f"  {entry.get('name')} [{mark}]")
    missing_env = preview.get("missing_env") or []
    if missing_env:
        typer.echo(f"missing env (supply with --env): {', '.join(missing_env)}")
    for item in preview.get("items", []):
        typer.echo(f"item {item.get('item')} (base {item.get('base')}):")
        for route in item.get("routes", []):
            flag = "public" if route.get("public") else "authed"
            methods = ",".join(route.get("methods", []))
            typer.echo(f"  {methods} {route.get('full_path')} [{flag}]")
    for collision in preview.get("collisions", []):
        methods = ",".join(collision.get("methods", []))
        typer.echo(
            f"collision: {methods} {collision.get('full_path')} clashes with "
            f"{collision.get('conflict_owner')} {collision.get('conflict_path')} — remap the item base"
        )
    if preview.get("requires_public_acceptance"):
        typer.echo("public routes answer WITHOUT authentication; pass --accept-public-routes to install:")
        for row in preview.get("new_public_routes", []):
            methods = ",".join(row.get("methods", []))
            typer.echo(f"  {methods} {row.get('full_path')}")


@app.command("install")
@covers(("POST", "/api/marketplace/install"), ("POST", "/api/marketplace/install/preview"))
def install(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Plugin ref 'namespace/name'.")],
    version: Annotated[str | None, typer.Option("--version", help="Pin a specific version; omit for latest.")] = None,
    mount: Annotated[
        list[str] | None,
        typer.Option("--mount", help="Remap a route-carrying item's mount base as item=base (repeatable)."),
    ] = None,
    accept_public_routes: Annotated[
        bool,
        typer.Option("--accept-public-routes", help="Acknowledge routes that answer without authentication."),
    ] = False,
    env: Annotated[
        list[str] | None,
        typer.Option("--env", help="Supply an install-time env value as KEY=VALUE (repeatable)."),
    ] = None,
    secret: Annotated[
        list[str] | None,
        typer.Option("--secret", help="Mark an env KEY secret (repeatable); the server auto-marks schema-known ones."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show the resolved routes, delivery, and required env without installing."),
    ] = False,
) -> None:
    """Install a marketplace plugin by ref, optionally pinning a version.

    ``--mount`` remaps a route-carrying item's base prefix; ``--env KEY=VALUE`` and
    ``--secret KEY`` supply the install-time env the spec declares (an mcp entry's
    ``!ENV`` markers or an oauth connector's client credentials, the secret
    auto-marked); ``--dry-run`` previews the resolved routes, delivery, and required
    env (exiting non-zero on any collision) without installing; and
    ``--accept-public-routes`` is required when the plugin declares routes that
    answer without authentication.

    Before installing, the server's preview reports which required env is still
    missing; an unresolved var not passed via ``--env`` fails the command before any
    install is posted.

    Example: ``tai plugins install tai42/toolbox``
    """
    ctx_obj = app_context(ctx)
    overrides = _mount_overrides(mount)
    env_values = _env_map(env)
    preview_body: dict[str, Any] = {"ref": ref}
    if version is not None:
        preview_body["version"] = version
    if overrides:
        preview_body["route_mounts"] = overrides
    if dry_run:
        with ctx_obj.client() as client:
            preview = client.post("/api/marketplace/install/preview", json=preview_body)
        if ctx_obj.json_output:
            print_json(preview)
        else:
            _render_preview_table(preview)
        # A collision is a non-zero exit so a scripted dry-run gates on it.
        if isinstance(preview, dict) and preview.get("collisions"):
            raise typer.Exit(code=1)
        return
    body: dict[str, Any] = {"ref": ref}
    if version is not None:
        body["version"] = version
    if overrides:
        body["route_mounts"] = overrides
    if accept_public_routes:
        body["accept_public_routes"] = True
    if env_values:
        body["env"] = env_values
    if secret:
        body["secret_keys"] = list(secret)
    with ctx_obj.client() as client:
        # The preview reports the server-computed ``missing_env`` (required minus the
        # store and process env); refuse before posting when a name is still unsupplied.
        preview = client.post("/api/marketplace/install/preview", json=preview_body)
        _refuse_missing_env(preview, set(env_values))
        data = client.post("/api/marketplace/install", json=body)
    emit_result(ctx_obj, data)


@app.command("uninstall")
@covers(("POST", "/api/marketplace/uninstall"))
def uninstall(ctx: typer.Context, ref: Annotated[str, typer.Argument(help="Plugin ref 'namespace/name'.")]) -> None:
    """Uninstall a marketplace-installed plugin by ref.

    Example: ``tai plugins uninstall tai42/toolbox``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post("/api/marketplace/uninstall", json={"ref": ref})
    emit_result(ctx_obj, data)


@app.command("update")
@covers(("POST", "/api/marketplace/update"), ("POST", "/api/marketplace/install/preview"))
def update(
    ctx: typer.Context,
    ref: Annotated[str, typer.Argument(help="Plugin ref 'namespace/name'.")],
    version: Annotated[str | None, typer.Option("--version", help="Target version; omit for latest.")] = None,
    env: Annotated[
        list[str] | None,
        typer.Option("--env", help="Supply an install-time env value as KEY=VALUE (repeatable)."),
    ] = None,
    secret: Annotated[
        list[str] | None,
        typer.Option("--secret", help="Mark an env KEY secret (repeatable); the server auto-marks schema-known ones."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show the target's delivery and required env without updating."),
    ] = False,
) -> None:
    """Update an installed plugin to a newer (or named) version.

    ``--env KEY=VALUE`` and ``--secret KEY`` supply env a new version newly requires
    (a fresh ``!ENV`` marker or connector credential); ``--dry-run`` previews the
    target's delivery and required env. A required var still missing and not passed
    via ``--env`` fails the command before any update is posted.

    Example: ``tai plugins update tai42/toolbox``
    """
    ctx_obj = app_context(ctx)
    env_values = _env_map(env)
    preview_body: dict[str, Any] = {"ref": ref}
    if version is not None:
        preview_body["version"] = version
    if dry_run:
        with ctx_obj.client() as client:
            preview = client.post("/api/marketplace/install/preview", json=preview_body)
        if ctx_obj.json_output:
            print_json(preview)
        else:
            _render_preview_table(preview)
        if isinstance(preview, dict) and preview.get("collisions"):
            raise typer.Exit(code=1)
        return
    body: dict[str, Any] = {"ref": ref}
    if version is not None:
        body["version"] = version
    if env_values:
        body["env"] = env_values
    if secret:
        body["secret_keys"] = list(secret)
    with ctx_obj.client() as client:
        preview = client.post("/api/marketplace/install/preview", json=preview_body)
        _refuse_missing_env(preview, set(env_values))
        data = client.post("/api/marketplace/update", json=body)
    emit_result(ctx_obj, data)


@app.command("upgrade")
@covers(("POST", "/api/marketplace/upgrade-all"))
def upgrade(
    ctx: typer.Context,
    all_plugins: Annotated[
        bool,
        typer.Option("--all", help="Upgrade every installed plugin to its latest compatible version."),
    ] = False,
) -> None:
    """Upgrade every installed plugin to its latest compatible version.

    ``--all`` is required: the batch is deliberately explicit, and a single
    plugin is upgraded with ``tai plugins update REF`` instead.

    Example: ``tai plugins upgrade --all``
    """
    if not all_plugins:
        raise typer.BadParameter(
            "pass --all to upgrade every installed plugin (a single plugin is 'tai plugins update REF')",
            param_hint="--all",
        )
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post("/api/marketplace/upgrade-all", json={})
    emit_records(ctx_obj, data, ["ref", "outcome", "detail"], items_key="results")


@app.command("advisories")
@covers(("GET", "/api/marketplace/advisories"))
def advisories(ctx: typer.Context) -> None:
    """Show the cached advisory snapshot for the installed plugins.

    Example: ``tai plugins advisories``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/marketplace/advisories")
    emit_records(
        ctx_obj,
        data,
        ["listing", "severity", "summary", "affected_versions", "withdrawn_at"],
        items_key="advisories",
    )


# -- local scaffolding (no server) -------------------------------------------

# The placeholder token every template file carries where the listing name goes;
# ``init`` substitutes the given NAME for it.
_TEMPLATE_NAME_TOKEN = "__PLUGIN_NAME__"
# The package-data subdirectory shipping the descriptor-only plugin skeletons,
# navigated from the ``tai42_cli`` package (never a filesystem path — read from
# the wheel via importlib.resources).
_TEMPLATES_DIRNAME = "templates"


def _template_name(kind: str, auth: str | None) -> str:
    """The template directory name for a ``--kind``/``--auth`` combination.

    ``--auth`` is REQUIRED for a connector (its provider is oauth or no-auth) and
    FORBIDDEN for an mcp-server (a descriptor-only server carries no auth block)."""
    if kind == "connector":
        if auth is None:
            raise typer.BadParameter("connector requires --auth {oauth,none}", param_hint="--auth")
        return f"connector-{auth}"
    if auth is not None:
        raise typer.BadParameter("--auth is only valid for --kind connector", param_hint="--auth")
    return kind


def _write_template(source: Traversable, dest: Path, name: str) -> list[Path]:
    """Copy the ``source`` template tree to ``dest``, substituting the name token.

    Every template file is UTF-8 text; the name token is replaced in each. Both the
    target dir and every subdir are created with ``exist_ok=False`` so a pre-existing
    path raises loudly rather than merging into an author's tree."""
    written: list[Path] = []

    def recurse(node: Traversable, target: Path) -> None:
        for child in node.iterdir():
            child_target = target / child.name
            if child.is_dir():
                child_target.mkdir(exist_ok=False)
                recurse(child, child_target)
            else:
                text = child.read_text(encoding="utf-8").replace(_TEMPLATE_NAME_TOKEN, name)
                child_target.write_text(text, encoding="utf-8")
                written.append(child_target)

    dest.mkdir(parents=True, exist_ok=False)
    recurse(source, dest)
    return written


@app.command("init")
def init(
    name: Annotated[str, typer.Argument(help="Plugin listing name (the 'name' half of namespace/name).")],
    kind: Annotated[str, typer.Option("--kind", help="Item kind to scaffold: connector or mcp-server.")],
    auth: Annotated[
        str | None,
        typer.Option("--auth", help="Connector auth mode (required for connector, forbidden for mcp-server)."),
    ] = None,
    directory: Annotated[
        Path | None,
        typer.Option("--dir", help="Target directory; defaults to ./NAME."),
    ] = None,
) -> None:
    """Scaffold a descriptor-only plugin directory (yml, docs, licence, changelog).

    ``--kind connector`` needs ``--auth {oauth,none}``; ``--kind mcp-server`` takes no
    ``--auth``. The written tree parses as a plugin spec and its docs validate as-is;
    edit the placeholders (``acme``, ``example.invalid``, the icon) before publishing.

    Example: ``tai plugins init connector-acme --kind connector --auth oauth``
    """
    if kind not in ("connector", "mcp-server"):
        raise typer.BadParameter("--kind must be one of: connector, mcp-server", param_hint="--kind")
    if auth is not None and auth not in ("oauth", "none"):
        raise typer.BadParameter("--auth must be one of: oauth, none", param_hint="--auth")
    if not LISTING_SLUG_RE.fullmatch(name):
        raise typer.BadParameter(f"NAME must match {LISTING_SLUG_RE.pattern}", param_hint="NAME")
    template = _template_name(kind, auth)
    dest = (directory if directory is not None else Path(name)).resolve()
    if dest.exists():
        raise typer.BadParameter(f"target {dest} already exists", param_hint="--dir")
    source = resources.files("tai42_cli") / _TEMPLATES_DIRNAME / template
    written = _write_template(source, dest, name)
    echo_stderr(f"scaffolded {kind} plugin at {dest} ({len(written)} files)")
    typer.echo(str(dest))


@app.command("schema")
def schema() -> None:
    """Print the JSON Schema of a ``tai-plugin.yml`` (the ``PluginSpec`` model).

    The canonical shape the marketplace validates against, including the ``provides``
    index; feed it to an editor or a validator.

    Example: ``tai plugins schema``
    """
    print_json(PluginSpec.model_json_schema())
