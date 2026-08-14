class TaiMCPServerError(Exception):
    """Base error for this MCP server's exception hierarchy."""


class TaiValidationError(TaiMCPServerError):
    """Raised when validating tool/extension parameters or return values fails."""


class TurnTimeoutError(TaiMCPServerError):
    """Raised when a synchronous turn exceeds the configured turn timeout."""
