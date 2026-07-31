"""Errors raised by the connectors contract."""

from __future__ import annotations


class ConnectorError(Exception):
    """Base exception for connector store / control-plane failures."""


class OperatorMisconfiguredError(RuntimeError):
    """A required operator-supplied env var is unset or empty.

    Distinct from a generic RuntimeError so the router maps it to HTTP 503 with
    the offending env-var name, instead of a generic 500.
    """

    def __init__(self, env_var: str, provider_id: str):
        super().__init__(
            f"Provider {provider_id!r} is enabled but env var {env_var} is "
            f"unset. Set {env_var} on the API process environment."
        )
        self.env_var = env_var
        self.provider_id = provider_id
