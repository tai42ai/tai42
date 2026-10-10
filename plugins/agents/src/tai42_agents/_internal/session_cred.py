"""The canonical operator session-cred model shared by both sandbox-session agents.

Both the ``claude_code`` coding agent and the durable ``langchain_deep_agent`` inject an
operator-configured list of session creds into a CLEAN sandbox session (never the host env).
The two agents parse the SAME model — a discriminated union of a plain static value and a
per-caller connection reference — so an operator's ``creds`` entry means ONE thing across the
plugin and a new cred variant is added in exactly ONE place.

* :class:`StaticCred` — a fixed ``env_name`` set to a long-lived secret, baked into the CLEAN
  session env at create. Long-lived with NO refresh path (a session-lifetime constant: a
  rotated value reaches the session only after it is recreated), so it has no delivery knob —
  a static value always rides the create-time env.
* :class:`ConnectionCred` — a per-caller connection reference resolved through
  ``tai42_app.connectors.resolve_connection_auth`` (which fails CLOSED on an identity-less door
  and takes ``connection_id`` from operator settings, never a session-supplied value).
  ``delivery="bearer"`` (the DEFAULT, REQUIRED for any refreshable/expiring cred — the primary
  OAuth case) materializes an ``Authorization: Bearer`` credential-helper FILE under the
  session's ``.creds`` tree EVERY TURN from the fresh resolution, so a refreshed token reaches a
  reused session on the next turn. ``delivery="env"`` is allowed ONLY where the resolved value
  is effectively static (a fixed key that never rotates): it is baked into the CLEAN session env
  at create and is a session-lifetime constant.
* :data:`SessionCredSpec` — ``Annotated[StaticCred | ConnectionCred, Field(discriminator="kind")]``
  so a list mixing both variants parses unambiguously off the ``kind`` tag.
* :func:`materialize_session_creds` — the ONE rule turning a cred list into what a session
  receives (:class:`SessionCredMaterial`); both agents materialize through it.

Both variants ``forbid`` extra keys: a mistyped or misplaced field (e.g. a ``delivery`` on a
static entry, which has no bearer refresh path) is a LOUD config error, never a silent drop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from tai42_contract.app import tai42_app
from tai42_contract.connectors import ResolvedConnectionAuth


class StaticCred(BaseModel):
    """A plain static session cred: a fixed ``env_name`` set to a long-lived secret.

    Baked into the CLEAN session env at create and a session-lifetime constant (no refresh
    path — a rotated value reaches the session only after it is recreated). For the coding
    agent the model credential is the canonical static entry.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["static"] = "static"
    env_name: str = Field(min_length=1)
    value: SecretStr


class ConnectionCred(BaseModel):
    """A per-caller connection-reference cred resolved through ``tai42_app.connectors.resolve_connection_auth``.

    The resolver fails CLOSED on an identity-less door and takes ``connection_id`` from
    operator settings, never a session-supplied value.

    ``delivery="bearer"`` (the DEFAULT, REQUIRED for any refreshable/expiring cred — the primary
    OAuth case) re-materializes an ``Authorization: Bearer`` credential-helper FILE under the
    session's ``.creds`` tree EVERY TURN from the fresh resolution, so a refreshed token reaches
    a reused session on the next turn. ``delivery="env"`` is allowed ONLY for a value that never
    rotates: it is baked into the CLEAN session env at create and is a session-lifetime constant.
    A ``required`` entry resolving to nothing usable raises loudly; a non-required one injects
    nothing.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["connection"] = "connection"
    env_name: str = Field(min_length=1)
    connection_id: str
    provider_id: str
    sub_service: str
    delivery: Literal["bearer", "env"] = "bearer"
    required: bool = True


# The operator's session-cred entry: a static value or a per-caller connection reference,
# discriminated by ``kind`` so a list mixing both parses unambiguously.
SessionCredSpec = Annotated[StaticCred | ConnectionCred, Field(discriminator="kind")]


class SessionCredError(RuntimeError):
    """A session cred cannot be materialized as configured for the current caller."""


@dataclass(frozen=True)
class BearerCred:
    """A refreshable cred re-written every turn as a credential-helper file named ``env_name``."""

    env_name: str
    token: SecretStr | None
    headers: dict[str, SecretStr]

    def file_body(self) -> str:
        """``Authorization: Bearer <token>`` (when a token) then one ``<Name>: <value>`` line per header."""
        lines: list[str] = []
        if self.token is not None:
            lines.append(f"Authorization: Bearer {self.token.get_secret_value()}")
        lines.extend(f"{name}: {value.get_secret_value()}" for name, value in self.headers.items())
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class SessionCredMaterial:
    """What a session receives: the create-time ``env`` and the per-turn ``bearer`` files."""

    env: dict[str, SecretStr] = field(default_factory=dict)
    env_names: list[str] = field(default_factory=list)
    """The ``env`` names in insertion order."""
    bearer: list[BearerCred] = field(default_factory=list)

    def secret_values(self) -> list[str]:
        """Every injected token, env and header value, for transcript redaction."""
        values = [value.get_secret_value() for value in self.env.values()]
        for cred in self.bearer:
            if cred.token is not None:
                values.append(cred.token.get_secret_value())
            values.extend(value.get_secret_value() for value in cred.headers.values())
        return [value for value in values if value]


async def materialize_session_creds(creds: list[SessionCredSpec]) -> SessionCredMaterial:
    """Materialize the operator's session creds for the current caller by the one rule.

    A static cred rides ``env`` under its ``env_name``. A connection cred resolves through
    ``tai42_app.connectors.resolve_connection_auth``; its ``env`` channel always rides ``env``
    under its own names. Under ``delivery="env"`` the raw token rides ``env`` under the cred's
    ``env_name`` and HTTP headers are refused (an env var cannot carry them); under
    ``delivery="bearer"`` the token and the headers become one :class:`BearerCred`. A required
    connection cred resolving to nothing usable raises :class:`SessionCredError`; a
    non-required one injects nothing.
    """
    material = SessionCredMaterial()
    for cred in creds:
        if isinstance(cred, StaticCred):
            _put_env(material, cred.env_name, cred.value)
            continue
        resolved = await tai42_app.connectors.resolve_connection_auth(
            cred.connection_id, cred.provider_id, cred.sub_service
        )
        _put_connection(material, cred, resolved)
    return material


def _put_env(material: SessionCredMaterial, name: str, value: SecretStr) -> None:
    if name not in material.env:
        material.env_names.append(name)
    material.env[name] = value


def _put_connection(
    material: SessionCredMaterial, cred: ConnectionCred, resolved: ResolvedConnectionAuth | None
) -> None:
    if resolved is None or (resolved.access_token is None and not resolved.env and not resolved.headers):
        if cred.required:
            raise SessionCredError(
                f"required connection cred {cred.env_name!r} resolved to no usable credential for the current "
                f"caller (connection {cred.connection_id!r})"
            )
        return
    if cred.delivery == "env" and resolved.headers:
        raise SessionCredError(
            f"connection cred {cred.env_name!r} resolved HTTP headers, which delivery='env' cannot carry; "
            "use delivery='bearer'"
        )
    for name, value in resolved.env.items():
        _put_env(material, name, value)
    if cred.delivery == "env":
        if resolved.access_token is not None:
            _put_env(material, cred.env_name, resolved.access_token)
    elif resolved.access_token is not None or resolved.headers:
        material.bearer.append(
            BearerCred(env_name=cred.env_name, token=resolved.access_token, headers=dict(resolved.headers))
        )
