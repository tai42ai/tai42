"""``tai setup`` — initialize a fresh deployment, or recover the owner's key on the host.

The default path is a single command over the public ``POST /api/setup`` door: it creates
the owner principal, mints the owner's first key (printed ONCE), and — when a login-attaching
accounts provider is configured — attaches the owner's login (a password set now or a one-time
invite link). It first reads ``GET /api/login/methods`` to learn whether the deployment still
needs setup and what login kinds it can attach; both calls run credential-free (the caller has
no key yet). Failures surface with their server message and a non-zero exit: ``403`` means the
setup token was wrong or the door is throttled; ``409`` means the deployment is already
initialized; ``501`` means the door is unavailable — access control is off, no key-minting
provider is configured, or the access-control Redis is unset.

``--recover`` is the host-side path for an initialized deployment whose owner has no key that
can authenticate (the identity store was flushed with no backup export): run on the deployment
host, it reads the manifest and stores directly and re-mints ONE surviving owner key. It needs
the server package installed with the CLI. Its outcomes: ``Forbidden`` — the setup token was
wrong or the ``local`` throttle backed the attempts off; "already holds a live key" — an owner
key still authenticates, so use or revoke it instead; "not initialized" — run plain ``tai setup``.
"""

from __future__ import annotations

import sys
from typing import Annotated, Any, Protocol

import typer

from tai42_cli.commands._common import (
    app_context,
    compact,
    covers,
    echo_stderr,
    emit_result,
    read_secret_arg,
    reject_double_stdin,
)
from tai42_cli.context import AppContext

app = typer.Typer(
    name="setup",
    help="Initialize a fresh deployment: create the owner principal and mint its first key.",
    invoke_without_command=True,
)


class RecoveryHandler(Protocol):
    """The host-side ``--recover`` handler the server package registers.

    It re-mints one surviving owner key on the deployment and returns the result payload.
    The cli stays skeleton-agnostic and reaches it only through :func:`register_recovery`.
    """

    def __call__(
        self,
        setup_token: str,
        *,
        key_user: str | None,
        key_description: str,
        manifest_path: str | None,
    ) -> dict[str, Any]:
        """Re-mint the owner's key on the deployment and return the result payload."""
        ...


_recovery: RecoveryHandler | None = None


def register_recovery(handler: RecoveryHandler) -> None:
    """Register the host-side ``--recover`` handler; the server package calls this at CLI load."""
    global _recovery
    _recovery = handler


def _stdin_is_interactive() -> bool:
    """Whether stdin is a terminal — the seam prompts and non-TTY errors branch on."""
    return sys.stdin.isatty()


def _resolve_token(token: str | None) -> str:
    """The setup token from ``--token`` (``-`` reads stdin), the env, or an interactive prompt.

    A ``-`` value reads the token from stdin, stripped. An absent token prompts on a
    TTY and otherwise raises — an unattended run must supply the token explicitly.
    """
    if token == "-":  # noqa: S105 constant identifier, not a secret value
        return sys.stdin.readline().strip()
    if token is not None:
        return token
    if _stdin_is_interactive():
        return typer.prompt("Setup token", hide_input=True)
    raise typer.BadParameter("pass --token, set TAI_SETUP_TOKEN, or pipe the token with --token -")


def _requested_login(password: str | None, invite: bool, email: str | None) -> bool:
    """Whether the caller asked to attach a login through any of the login options."""
    return password is not None or invite or email is not None


def _validate_login_flags(password: str | None, invite: bool, no_login: bool, email: str | None) -> None:
    """Reject contradictory login options before any network call.

    The three attachment choices — a password, an invite, and keys-only — are mutually
    exclusive, and ``--email`` is required with (and only with) a password or an invite.
    """
    if no_login and _requested_login(password, invite, email):
        raise typer.BadParameter("--no-login cannot be combined with --password/--invite/--email")
    if password is not None and invite:
        raise typer.BadParameter("choose only one of --password/--password-file or --invite")
    if (password is not None or invite) and email is None:
        raise typer.BadParameter("--email is required with --password/--invite")
    if email is not None and password is None and not invite:
        raise typer.BadParameter("--email requires --password/--password-file or --invite")


def _prompt_login(kinds: list[str]) -> dict[str, Any] | None:
    """Interactively decide the owner's login when no login option was given on a TTY.

    Declining attaches nothing (keys-only); accepting collects the email and a
    confirmed password. A non-interactive caller must state its intent with a flag.
    """
    if not _stdin_is_interactive():
        raise typer.BadParameter("choose --password/--password-file, --invite, or --no-login")
    if not typer.confirm("Attach the owner's login now? (No = keys only)"):
        return None
    email = typer.prompt("Email")
    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    return _password_login(kinds, email, password)


def _password_login(kinds: list[str], email: str, password: str) -> dict[str, Any]:
    """The password-credential body, rejected when the provider does not accept passwords."""
    if "password" not in kinds:
        raise typer.BadParameter("this deployment's accounts provider does not accept a password login")
    return {"kind": "password", "email": email, "password": password}


def _invite_login(kinds: list[str], email: str) -> dict[str, Any]:
    """The invite-credential body, rejected when the provider does not accept invites."""
    if "invite" not in kinds:
        raise typer.BadParameter("this deployment's accounts provider does not accept an invite login")
    return {"kind": "invite", "email": email}


def _resolve_login(
    setup_login: Any, password: str | None, invite: bool, no_login: bool, email: str | None
) -> dict[str, Any] | None:
    """The ``login`` body for ``POST /api/setup`` given the server's attachable kinds and the flags.

    ``setup_login`` is ``null`` when no login-attaching provider is configured — then any
    login option is a usage error and the deployment is keys-only. Otherwise the caller's
    flags (or an interactive choice) select a password, an invite, or keys-only, checked
    against the kinds the provider accepts.
    """
    if setup_login is None:
        if _requested_login(password, invite, email):
            raise typer.BadParameter(
                "this deployment has no accounts provider that can attach a login; drop --password/--invite/--email"
            )
        return None
    kinds = setup_login["kinds"]
    if password is not None:
        assert email is not None  # noqa: S101 — _validate_login_flags guarantees email with a password
        return _password_login(kinds, email, password)
    if invite:
        assert email is not None  # noqa: S101 — _validate_login_flags guarantees email with an invite
        return _invite_login(kinds, email)
    if no_login:
        return None
    return _prompt_login(kinds)


def _require_needs_setup(methods: Any) -> None:
    """Stop with a non-zero exit when the deployment already has a principal.

    The setup token is never sent once a principal exists — the door is one-shot and
    the operator signs in with an existing credential instead.
    """
    if not methods.get("needs_setup"):
        echo_stderr(
            "Error: this deployment is already initialized (a principal exists); sign in with an existing "
            "credential instead"
        )
        raise typer.Exit(1)


def _validate_recover_flags(
    recover: bool,
    manifest_path: str | None,
    display_name: str | None,
    user: str | None,
    password: str | None,
    password_file: str | None,
    invite: bool,
    no_login: bool,
    email: str | None,
) -> None:
    """Reject flag combinations that do not belong to the chosen path, before any token read.

    On the initialize path ``--display-name`` is required and ``--manifest-path`` is a usage
    error (it applies to ``--recover`` only). On the recover path only ``--token``,
    ``--key-user``, ``--key-description`` and ``--manifest-path`` apply — every initialize-only
    option is a usage error, since recovery re-mints the EXISTING owner's key.
    """
    if not recover:
        if manifest_path is not None:
            raise typer.BadParameter("--manifest-path applies to --recover only")
        if display_name is None:
            raise typer.BadParameter("--display-name is required", param_hint="--display-name")
        return
    initialize_only = (
        display_name is not None
        or user is not None
        or password is not None
        or password_file is not None
        or invite
        or no_login
        or email is not None
    )
    if initialize_only:
        raise typer.BadParameter(
            "--recover re-mints the existing owner's key; it takes only --token, --key-user, "
            "--key-description and --manifest-path"
        )


def _run_recovery(
    ctx_obj: AppContext, token: str, key_user: str | None, key_description: str, manifest_path: str | None
) -> None:
    """Re-mint the owner's key through the host-side handler and render the once-shown result.

    The handler is filled by the server package's ``tai.commands`` entry point at CLI load;
    its absence means the deployment's server package is not installed alongside the CLI, which
    is a usage error rather than a crash. No HTTP request is made on this path.
    """
    if _recovery is None:
        raise typer.BadParameter(
            "--recover runs on the deployment host and needs the server package (tai42-skeleton) installed with the CLI"
        )
    data = _recovery(token, key_user=key_user, key_description=key_description, manifest_path=manifest_path)
    emit_result(ctx_obj, data)
    _announce(ctx_obj, data)


@app.callback()
@covers(("POST", "/api/setup"))
def setup(
    ctx: typer.Context,
    display_name: Annotated[
        str | None, typer.Option("--display-name", help="The owner principal's display name (required to initialize).")
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            envvar="TAI_SETUP_TOKEN",
            help="The setup token, or '-' to read it from stdin. The server auto-generates one and prints it in its "
            "startup log unless you set TAI_SETUP_TOKEN yourself, or the door is unavailable and mints none — access "
            "control off, no key-minting identity provider, or the access-control Redis unset (those three are also "
            "the 501 the door answers). Falls back to TAI_SETUP_TOKEN, then an interactive prompt.",
        ),
    ] = None,
    user: Annotated[
        str | None, typer.Option("--user", help="The owner principal's id (omitted = server-minted).")
    ] = None,
    key_user: Annotated[
        str | None, typer.Option("--key-user", help="The owner key's id (omitted = server-minted).")
    ] = None,
    key_description: Annotated[
        str, typer.Option("--key-description", help="The owner key's description.")
    ] = "owner key",
    password: Annotated[
        str | None, typer.Option("--password", help="Set the owner's password now (needs --email).")
    ] = None,
    password_file: Annotated[
        str | None,
        typer.Option(
            "--password-file",
            help="Read the owner's password from a file, or from stdin when the path is '-', instead of putting a "
            "secret on the command line (a value on argv leaks via ps and shell history).",
        ),
    ] = None,
    invite: Annotated[
        bool,
        typer.Option("--invite", help="Attach the owner's login via a one-time invite link instead of a password."),
    ] = False,
    no_login: Annotated[
        bool, typer.Option("--no-login", help="Keys-only: create the owner and its key, attach no login.")
    ] = False,
    email: Annotated[
        str | None, typer.Option("--email", help="The owner's email (required with --password/--invite).")
    ] = None,
    recover: Annotated[
        bool,
        typer.Option(
            "--recover",
            help="Re-mint the owner's key when the deployment is initialized but no owner key can authenticate "
            "(host-side: reads the deployment's manifest and stores; needs the setup token).",
        ),
    ] = False,
    manifest_path: Annotated[
        str | None,
        typer.Option("--manifest-path", help="With --recover: the deployment's manifest (default: TAI_MANIFEST_PATH)."),
    ] = None,
) -> None:
    """Initialize a fresh deployment: create the owner, mint its key, optionally attach a login.

    The owner key is printed ONCE — capture it now. A password (``--password``/
    ``--password-file`` with ``--email``) or an invite (``--invite`` with ``--email``)
    attaches the owner's login when a login-attaching provider is configured;
    ``--no-login`` keeps it keys-only.

    Example: ``tai setup --token - --display-name 'Acme owner' --invite --email owner@example.com``

    ``--recover`` re-mints one surviving owner key on the deployment host instead of
    initializing (see the module help for its outcomes).
    """
    ctx_obj = app_context(ctx)
    _validate_recover_flags(
        recover, manifest_path, display_name, user, password, password_file, invite, no_login, email
    )
    reject_double_stdin(token, password_file, param_hint="--token/--password-file")
    resolved_token = _resolve_token(token)
    if recover:
        _run_recovery(ctx_obj, resolved_token, key_user, key_description, manifest_path)
        return
    assert display_name is not None  # noqa: S101 — _validate_recover_flags requires it on the initialize path
    password_value = read_secret_arg(
        password, password_file, param_hint="--password", file_param_hint="--password-file"
    )
    _validate_login_flags(password_value, invite, no_login, email)
    with ctx_obj.client(anonymous=True) as client:
        methods = client.get("/api/login/methods")
    _require_needs_setup(methods)
    login = _resolve_login(methods.get("setup_login"), password_value, invite, no_login, email)
    body = compact(
        {
            "setup_token": resolved_token,
            "owner_display_name": display_name,
            "owner_user_id": user,
            "key_user_id": key_user,
            "key_description": key_description,
            "login": login,
        }
    )
    with ctx_obj.client(anonymous=True) as client:
        data = client.post("/api/setup", json=body)
    emit_result(ctx_obj, data)
    _announce(ctx_obj, data)


def _announce(ctx_obj: AppContext, data: Any) -> None:
    """The human-mode reminders printed to stderr after the result table.

    The key is shown once; an attached invite names the path and one-time token the
    operator completes the owner's login with. Under ``--json`` nothing extra is printed.
    """
    if ctx_obj.json_output or not isinstance(data, dict):
        return
    echo_stderr("Owner key shown once — store it now.")
    if data.get("invite_token"):
        echo_stderr(
            f"Complete the owner's login at {data.get('login_path')} with invite token "
            f"{data['invite_token']} (one-time)."
        )
