"""``tai sandbox`` — inspect the sandbox provider's identity and resolved policy.

Thin wrapper over the authed ``/api/sandbox`` route. The identity command reports
whether a sandbox provider is installed along with the resolved security-as-config
policy (the empty state still carries the policy, so it reads as identity, not error).
The command is read-only — it inspects the resolved policy and never mutates
provider state.
"""

from __future__ import annotations

import typer

from tai42_cli.commands._common import (
    app_context,
    covers,
    emit_result,
)

app = typer.Typer(
    name="sandbox",
    help="Inspect the sandbox provider's identity and resolved policy.",
    no_args_is_help=True,
)


@app.command("info")
@covers(("GET", "/api/sandbox"))
def sandbox_info(ctx: typer.Context) -> None:
    """Show the registered sandbox provider's identity and resolved policy (or the empty state).

    Example: ``tai sandbox info``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/sandbox")
    emit_result(ctx_obj, data)
