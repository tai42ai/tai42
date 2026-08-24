from __future__ import annotations

from tai42_contract.errors import ErrorKind


class TaiMCPServerError(Exception):
    """Base error for this MCP server's exception hierarchy."""


class TaiValidationError(TaiMCPServerError):
    """Raised when validating tool/extension parameters or return values fails."""

    # A parameter/return-value validation failure is a rejected input.
    __tai_error_kind__ = ErrorKind.BAD_INPUT


class TurnTimeoutError(TaiMCPServerError):
    """Raised when a synchronous turn exceeds the configured turn timeout."""

    # A turn that overran its timeout budget.
    __tai_error_kind__ = ErrorKind.TIMED_OUT
