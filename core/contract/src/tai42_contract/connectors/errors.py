"""Errors raised by the connectors contract."""

from __future__ import annotations

from tai42_contract.errors import ErrorKind


class ConnectorError(Exception):
    """Base exception for connector store / control-plane failures."""

    # Base store/control-plane fault — the backend the caller delegates to, like the other error-family bases.
    __tai_error_kind__ = ErrorKind.UPSTREAM_ERROR


class OperatorMisconfiguredError(RuntimeError):
    """A required operator-supplied env var is unset or empty.

    Distinct from a bare RuntimeError so the operation layer maps it to a named HTTP
    501 (``NotSupportedError``) carrying a machine-readable ``code`` and the env-var name.
    """

    # Maps to the 501 NotSupportedError door outcome; a capability this deployment cannot serve until configured.
    __tai_error_kind__ = ErrorKind.UNAVAILABLE

    def __init__(self, env_var: str, provider_id: str):
        super().__init__(
            f"Provider {provider_id!r} is enabled but env var {env_var} is "
            f"unset. Set {env_var} on the API process environment."
        )
        self.env_var = env_var
        self.provider_id = provider_id


class MalformedConnectionIdError(ConnectorError):
    """A ``connection_id`` that is not a well-formed identifier (a uuid4) and can key no
    record. The persistence boundary maps it to the same not-found outcome as a
    genuinely-absent record, so a door is no oracle for the id's shape.
    """

    # Deliberately indistinguishable from a genuinely-absent record (no id-shape oracle), so it shares NOT_FOUND.
    __tai_error_kind__ = ErrorKind.NOT_FOUND
