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
        """Build the error naming the unset ``env_var`` and the ``provider_id`` that requires it."""
        super().__init__(
            f"Provider {provider_id!r} is enabled but env var {env_var} is "
            f"unset. Set {env_var} on the API process environment."
        )
        self.env_var = env_var
        self.provider_id = provider_id


class MalformedConnectionIdError(ConnectorError):
    """A ``connection_id`` that is not a well-formed identifier (a uuid4) and can key no record.

    The persistence boundary maps it to the same not-found outcome as a genuinely-absent record, so a
    door is no oracle for the id's shape.
    """

    # Deliberately indistinguishable from a genuinely-absent record (no id-shape oracle), so it shares NOT_FOUND.
    __tai_error_kind__ = ErrorKind.NOT_FOUND


class ConnectorClientRequiredError(ConnectorError):
    """A provider needs an OAuth client this deployment does not have.

    No env client is declared and no stored client can be resolved for the provider's authorization
    server, so an administrator has to register an app by hand and set its credentials.
    """

    # A capability this deployment cannot serve until configured, like OperatorMisconfiguredError.
    __tai_error_kind__ = ErrorKind.UNAVAILABLE

    def __init__(self, provider_id: str, detail: str | None = None):
        """Build the error naming the ``provider_id`` that has no usable client, with an optional ``detail``."""
        message = f"provider {provider_id!r} has no OAuth client: set client credentials for it"
        super().__init__(f"{message} ({detail})" if detail else message)
        self.provider_id = provider_id
