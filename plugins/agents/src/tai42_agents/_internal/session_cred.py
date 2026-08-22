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

Both variants ``forbid`` extra keys: a mistyped or misplaced field (e.g. a ``delivery`` on a
static entry, which has no bearer refresh path) is a LOUD config error, never a silent drop.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class StaticCred(BaseModel):
    """A plain static session cred: a fixed ``env_name`` set to a long-lived secret.

    Baked into the CLEAN session env at create and a session-lifetime constant (no refresh
    path — a rotated value reaches the session only after it is recreated). For the coding
    agent the model credential (§A1) is the canonical static entry.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["static"] = "static"
    env_name: str = Field(min_length=1)
    value: SecretStr


class ConnectionCred(BaseModel):
    """A per-caller connection-reference cred resolved through
    ``tai42_app.connectors.resolve_connection_auth`` (which fails CLOSED on an identity-less
    door and takes ``connection_id`` from operator settings, never a session-supplied value).

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
