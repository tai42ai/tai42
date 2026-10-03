"""``tai auth`` — identity and capability introspection, plus member actions.

Thin wrappers over the authed ``/api/auth/me`` route, the PUBLIC
``/api/login/claim`` claim-exchange door (``claim`` runs credential-free), and the
admin-only member-actions doors (``tai auth member-actions list`` /
``tai auth member-actions invoke``) that list and invoke the accounts providers'
declared member-admin actions by their opaque keys.
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
)

app = typer.Typer(
    name="auth",
    help="Identity and capability introspection.",
    no_args_is_help=True,
)

_CLAIM_FRAGMENT_MARKER = "#claim="


def _extract_claim_token(value: str) -> str:
    """The claim token from either a bare token or a full claim URL/fragment.

    The token rides the URL FRAGMENT (``…/login#claim=<token>``), so pasting the whole
    link just works: take the tail after ``#claim=``. A bare token (no marker) is
    returned as-is. The token is NEVER matched by its mint prefix — the server validates
    it, and a prefix check here would add nothing.
    """
    if _CLAIM_FRAGMENT_MARKER in value:
        value = value.rsplit(_CLAIM_FRAGMENT_MARKER, 1)[1]
    return value.strip()


@app.command("whoami")
@covers(("GET", "/api/auth/me"))
def whoami(ctx: typer.Context) -> None:
    """Print the caller's derived capability projection.

    Example: ``tai auth whoami``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/auth/me")
    emit_result(ctx_obj, data)


@app.command("claim")
@covers(("POST", "/api/login/claim"))
def claim(
    ctx: typer.Context,
    token: Annotated[
        str,
        typer.Argument(
            help="A claim token or a full claim URL/fragment (either works), or '-' to read it from stdin "
            "(keeping it out of shell history)."
        ),
    ],
) -> None:
    """Exchange a one-time claim link for its API key — runs WITHOUT a credential.

    Pass the bare token or the whole claim URL; the token is taken from the ``#claim=``
    fragment. Pass ``-`` to read the token (or claim URL) from stdin, stripped, keeping it
    off the command line. The exchanged API key is printed ONCE — capture it now (there is
    no second exchange; the link is single-use). A used/unknown/expired token answers the
    same ``unknown or already used claim token``.

    Example: ``tai auth claim 'https://host/login#claim=<token>'``
    """
    ctx_obj = app_context(ctx)
    if token == "-":  # noqa: S105 constant identifier, not a secret value
        token = sys.stdin.read()
    claim_token = _extract_claim_token(token)
    # The caller has no key yet — this is the whole point — so the exchange runs over the
    # no-credential client path; a stale/wrong credential is never sent to the public door.
    with ctx_obj.client(anonymous=True) as client:
        data = client.post("/api/login/claim", json={"token": claim_token})
    emit_result(ctx_obj, data)


member_actions_app = typer.Typer(
    name="member-actions",
    help="List and invoke the accounts providers' declared member-admin actions.",
    no_args_is_help=True,
)
app.add_typer(member_actions_app, name="member-actions")


@member_actions_app.command("list")
@covers(("GET", "/api/auth/member-actions"))
def list_member_actions(ctx: typer.Context) -> None:
    """List every declared member action by its opaque key, label, scope, and destructive flag.

    Each registered accounts provider declares its own member-admin actions; the catalog
    carries each by an opaque ``key`` (which `invoke` echoes back), the rendered label, the
    placement ``scope``, the ``destructive`` confirm hint, and the input/result JSON schemas
    a caller renders a form and a result view from.

    Example: ``tai auth member-actions list``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/auth/member-actions")
    emit_records(ctx_obj, data, route=("GET", "/api/auth/member-actions"))


@member_actions_app.command("invoke")
@covers(("POST", "/api/auth/member-actions/invoke"))
def invoke_member_action(
    ctx: typer.Context,
    action_key: Annotated[
        str,
        typer.Option(
            "--action-key",
            help="The opaque catalog key of the action to invoke (from `tai auth member-actions list`).",
        ),
    ],
    target_handle: Annotated[
        str | None,
        typer.Option(
            "--target-handle",
            help="The opaque row handle the action acts on (from the members listing); omit for a page-scoped action.",
        ),
    ] = None,
    input_json: Annotated[
        str | None,
        typer.Option("--input", help="The per-action input as a JSON object."),
    ] = None,
    input_file: Annotated[
        str | None,
        typer.Option(
            "--input-file",
            help="Read the per-action input JSON object from a file, or '-' for stdin, keeping a secret off the "
            "command line (a value on argv leaks via ps and shell history).",
        ),
    ] = None,
) -> None:
    """Invoke one declared member action by its opaque key.

    The per-action ``input`` is handed to the action's declared input model and validated
    server-side (a declared-field type error is rejected; an unknown key is ignored). The
    result is the provider's opaque result — an invite action, say, returns a one-time
    sign-in link shown only once, so capture it now.

    Example: ``tai auth member-actions invoke --action-key <key> --target-handle <handle> --input '{"role": "editor"}'``
    """
    ctx_obj = app_context(ctx)
    input_payload = load_json_object_arg(input_json, input_file, param_hint="--input", file_param_hint="--input-file")
    body = {"action_key": action_key, "target_handle": target_handle, "input": input_payload or {}}
    with ctx_obj.client() as client:
        data = client.post("/api/auth/member-actions/invoke", json=body)
    emit_result(ctx_obj, data)
