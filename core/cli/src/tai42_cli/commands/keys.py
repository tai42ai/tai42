"""``tai keys`` — provision API keys and manage their access-control policy.

Thin wrappers over the authed ``/api/auth/api-keys*``, ``/api/auth/tokens-payload``
and ``/api/auth/validate-condition`` routes. The raw ``sk-…`` key is returned ONCE
by ``create`` — capture it then.
"""

from __future__ import annotations

import sys
from typing import Annotated

import typer

from tai42_cli.commands._common import (
    app_context,
    covers,
    emit_records,
    emit_result,
    load_json_object_arg,
    parse_json_object,
    seg,
)

app = typer.Typer(
    name="keys",
    help="Manage API keys and their access-control conditions.",
    no_args_is_help=True,
)

_CONDITION_HELP = (
    'The authorization condition as a templated-text JSON object: inline jq under "content" OR a stored resource '
    '"id" (exactly one), plus optional render "kwargs". Example: \'{"content": ".method == \\"GET\\""}\'.'
)
_CONDITION_FILE_HELP = (
    "Read the condition templated-text JSON object from a file, or from stdin when the path is '-', instead of "
    "putting a secret on the command line (a value on argv leaks via ps and shell history). Mutually exclusive "
    "with --condition."
)
_POLICY_DATA_FILE_HELP = (
    "Read the policy data JSON object from a file, or from stdin when the path is '-', instead of putting a secret "
    "on the command line (a value on argv leaks via ps and shell history). Mutually exclusive with --policy-data."
)


def _reject_double_stdin(condition_file: str | None, policy_data_file: str | None) -> None:
    """Stdin drains on the first read, so only one file option may be ``-`` per call."""
    if condition_file == "-" and policy_data_file == "-":
        raise typer.BadParameter(
            "only one option may read from stdin ('-')",
            param_hint="--condition-file/--policy-data-file",
        )


@app.command("list")
@covers(("GET", "/api/auth/tokens-payload"))
def list_keys(ctx: typer.Context) -> None:
    """List every provisioned key's identity and policy (never key material).

    Example: ``tai keys list``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/auth/tokens-payload")
    emit_records(ctx_obj, data, route=("GET", "/api/auth/tokens-payload"))


@app.command("create")
@covers(("POST", "/api/auth/api-keys"))
def create_key(
    ctx: typer.Context,
    user: Annotated[str, typer.Option("--user", help="The key's user id.")],
    description: Annotated[str, typer.Option("--description", help="Human description (required identity field).")],
    scope: Annotated[list[str] | None, typer.Option("--scope", help="A scope to grant (repeatable).")] = None,
    condition: Annotated[str | None, typer.Option("--condition", help=_CONDITION_HELP)] = None,
    condition_file: Annotated[str | None, typer.Option("--condition-file", help=_CONDITION_FILE_HELP)] = None,
    policy_data: Annotated[
        str | None, typer.Option("--policy-data", help="Extra policy data as a JSON object.")
    ] = None,
    policy_data_file: Annotated[str | None, typer.Option("--policy-data-file", help=_POLICY_DATA_FILE_HELP)] = None,
) -> None:
    """Provision an API key; the raw ``sk-…`` value is printed ONCE.

    Example: ``tai keys create --user alice --description 'CI key' --scope read``
    """
    ctx_obj = app_context(ctx)
    body: dict = {"user_id": user, "description": description, "scopes": list(scope or [])}
    _reject_double_stdin(condition_file, policy_data_file)
    condition_obj = load_json_object_arg(
        condition, condition_file, param_hint="--condition", file_param_hint="--condition-file"
    )
    if condition_obj is not None:
        body["condition"] = condition_obj
    policy_data_obj = load_json_object_arg(
        policy_data, policy_data_file, param_hint="--policy-data", file_param_hint="--policy-data-file"
    )
    if policy_data_obj is not None:
        body["policy_data"] = policy_data_obj
    with ctx_obj.client() as client:
        data = client.post("/api/auth/api-keys", json=body)
    emit_result(ctx_obj, data)


@app.command("bootstrap")
@covers(("POST", "/api/keys/bootstrap"))
def bootstrap_key(
    ctx: typer.Context,
    user: Annotated[str, typer.Option("--user", help="The first admin key's user id.")],
    description: Annotated[str, typer.Option("--description", help="Human description (required identity field).")],
    token: Annotated[
        str,
        typer.Option(
            "--token",
            help="The boot-time bootstrap token (printed in the server log at startup), or '-' to read it from "
            "stdin instead of putting the secret on the command line (a value on argv leaks via ps and shell "
            "history).",
        ),
    ],
) -> None:
    """Mint the FIRST admin API key on a fresh deployment — runs WITHOUT a credential.

    Access control ON with no key yet has no authenticated door to mint the first key;
    this is the one-shot public door. Gated by the boot-time bootstrap token; refused
    once any key exists. The raw ``sk-…`` value is printed ONCE — capture it now.

    Example: ``tai keys bootstrap --user alice --description 'root key' --token -``
    """
    ctx_obj = app_context(ctx)
    if token == "-":
        token = sys.stdin.readline().strip()
    body = {"user_id": user, "description": description, "bootstrap_token": token}
    # The caller has no key yet — the whole point — so the mint runs over the
    # no-credential client path; a stale/wrong credential is never sent to the public door.
    with ctx_obj.client(anonymous=True) as client:
        data = client.post("/api/keys/bootstrap", json=body)
    emit_result(ctx_obj, data)


@app.command("edit")
@covers(("PUT", "/api/auth/api-keys/{user_id}"))
def edit_key(
    ctx: typer.Context,
    user: Annotated[str, typer.Argument(help="The key's user id.")],
    description: Annotated[str | None, typer.Option("--description", help="New description.")] = None,
    scope: Annotated[
        list[str] | None, typer.Option("--scope", help="Replacement scope (repeatable); replaces the set.")
    ] = None,
    condition: Annotated[str | None, typer.Option("--condition", help=_CONDITION_HELP)] = None,
    condition_file: Annotated[str | None, typer.Option("--condition-file", help=_CONDITION_FILE_HELP)] = None,
    clear_condition: Annotated[
        bool, typer.Option("--clear-condition", help="Remove the key's condition gate (leave it unconditional).")
    ] = False,
    policy_data: Annotated[str | None, typer.Option("--policy-data", help="Policy data JSON; '{}' clears.")] = None,
    policy_data_file: Annotated[str | None, typer.Option("--policy-data-file", help=_POLICY_DATA_FILE_HELP)] = None,
) -> None:
    """Partially edit a key's description/scopes/policy in place (no rotation).

    Only the flags you pass are written; omitted fields are preserved. ``--clear-condition``
    sends the explicit reset (``condition: null``) that drops the key's gate, distinct from
    omitting the flag entirely.

    De-scoping this key (or its owner) also NARROWS what every hook and trigger link
    bound to it as its ``execution_key`` may call at its next fire — see ``tai hooks
    list`` for which records bind it.

    Example: ``tai keys edit alice --scope read --scope write``
    """
    ctx_obj = app_context(ctx)
    updates: dict = {}
    if description is not None:
        updates["description"] = description
    if scope:
        updates["scopes"] = list(scope)
    if clear_condition and (condition is not None or condition_file is not None):
        raise typer.BadParameter("pass either --condition/--condition-file or --clear-condition, not both")
    _reject_double_stdin(condition_file, policy_data_file)
    condition_obj = load_json_object_arg(
        condition, condition_file, param_hint="--condition", file_param_hint="--condition-file"
    )
    if condition_obj is not None:
        updates["condition"] = condition_obj
    elif clear_condition:
        updates["condition"] = None
    policy_data_obj = load_json_object_arg(
        policy_data, policy_data_file, param_hint="--policy-data", file_param_hint="--policy-data-file"
    )
    if policy_data_obj is not None:
        updates["policy_data"] = policy_data_obj
    if not updates:
        raise typer.BadParameter("provide at least one field to edit")
    with ctx_obj.client() as client:
        data = client.put(f"/api/auth/api-keys/{seg(user)}", json=updates)
    emit_result(ctx_obj, data)


@app.command("scopes")
@covers(("POST", "/api/auth/api-keys/{user_id}/scopes"))
def modify_scopes(
    ctx: typer.Context,
    user: Annotated[str, typer.Argument(help="The key's user id.")],
    add: Annotated[list[str] | None, typer.Option("--add", help="A scope to add (repeatable).")] = None,
    remove: Annotated[list[str] | None, typer.Option("--remove", help="A scope to remove (repeatable).")] = None,
) -> None:
    """Add and/or remove individual scopes on a key WITHOUT replacing the whole set.

    At least one --add or --remove is required. This is the granular complement of
    ``tai keys edit --scope``, which replaces the entire scope set at once.

    Example: ``tai keys scopes alice --add write --remove read``
    """
    if not add and not remove:
        raise typer.BadParameter("provide at least one --add or --remove scope")
    ctx_obj = app_context(ctx)
    body: dict = {"add": list(add or []), "remove": list(remove or [])}
    with ctx_obj.client() as client:
        data = client.post(f"/api/auth/api-keys/{seg(user)}/scopes", json=body)
    emit_result(ctx_obj, data)


@app.command("delete")
@covers(("DELETE", "/api/auth/api-keys/{user_id}"))
def delete_key(ctx: typer.Context, user: Annotated[str, typer.Argument(help="The key's user id.")]) -> None:
    """Revoke a key (immediate: the next request fails to auth).

    Revoking also STOPS every hook and trigger link bound to this key as its
    ``execution_key`` — their next fire is refused. Run ``tai hooks list`` and
    ``tai hooks trigger-links`` first to see which records bind it.

    Example: ``tai keys delete alice``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/auth/api-keys/{seg(user)}")
    emit_result(ctx_obj, data)


@app.command("claim-link")
@covers(("POST", "/api/auth/claim-links"))
def claim_link(
    ctx: typer.Context,
    raw_key: Annotated[
        str | None,
        typer.Argument(
            help="The raw API key to share (omit to enter it at a hidden prompt so it never lands in shell history)."
        ),
    ] = None,
    ttl: Annotated[
        int | None, typer.Option("--ttl", help="Link lifetime in seconds (capped by the server ceiling).")
    ] = None,
) -> None:
    """Mint a one-time claim link that carries a key you hold to another device.

    The link's token rides the URL FRAGMENT (``/login#claim=<token>``) and is
    SINGLE-USE — the first exchange burns it. Prints the claim path and expiry; compose
    the absolute URL (or a QR) from your own origin.

    Example: ``tai keys claim-link sk-abc123 --ttl 300``
    """
    ctx_obj = app_context(ctx)
    key = raw_key if raw_key is not None else typer.prompt("API key to share", hide_input=True)
    body: dict = {"api_key": key}
    if ttl is not None:
        body["ttl_seconds"] = ttl
    with ctx_obj.client() as client:
        data = client.post("/api/auth/claim-links", json=body)
    emit_result(ctx_obj, data)


@app.command("validate-condition")
@covers(("POST", "/api/auth/validate-condition"))
def validate_condition(
    ctx: typer.Context,
    condition: Annotated[str | None, typer.Option("--condition", help=_CONDITION_HELP)] = None,
    condition_file: Annotated[str | None, typer.Option("--condition-file", help=_CONDITION_FILE_HELP)] = None,
    sample_context: Annotated[
        str | None, typer.Option("--sample-context", help="A JqAuthContext-shaped sample to evaluate against, as JSON.")
    ] = None,
) -> None:
    """Compile (and optionally sample-evaluate) a jq policy condition without saving.

    Example: ``tai keys validate-condition --condition '{"content": ".method == \\"GET\\""}'``
    """
    ctx_obj = app_context(ctx)
    body: dict = {}
    condition_obj = load_json_object_arg(
        condition, condition_file, param_hint="--condition", file_param_hint="--condition-file"
    )
    if condition_obj is not None:
        body["condition"] = condition_obj
    if sample_context is not None:
        body["sample_context"] = parse_json_object(sample_context, param_hint="--sample-context")
    with ctx_obj.client() as client:
        data = client.post("/api/auth/validate-condition", json=body)
    emit_result(ctx_obj, data)


@app.command("policy-versions")
@covers(("GET", "/api/auth/api-keys/{user_id}/policy/versions"))
def policy_versions(ctx: typer.Context, user: Annotated[str, typer.Argument(help="The key's user id.")]) -> None:
    """List a user's append-only policy version history.

    Example: ``tai keys policy-versions alice``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get(f"/api/auth/api-keys/{seg(user)}/policy/versions")
    emit_records(ctx_obj, data, route=("GET", "/api/auth/api-keys/{user_id}/policy/versions"))


@app.command("policy-rollback")
@covers(("POST", "/api/auth/api-keys/{user_id}/policy/rollback"))
def policy_rollback(
    ctx: typer.Context,
    user: Annotated[str, typer.Argument(help="The key's user id.")],
    version: Annotated[int, typer.Argument(help="Target policy version to enforce.")],
) -> None:
    """Roll a user's enforced policy back to a prior version.

    Example: ``tai keys policy-rollback alice 2``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post(f"/api/auth/api-keys/{seg(user)}/policy/rollback", json={"version": version})
    emit_result(ctx_obj, data)
