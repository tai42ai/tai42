"""The ``claude_code`` failure surfaced to the caller."""

from __future__ import annotations


class ClaudeCodeError(RuntimeError):
    """A loud, constant-message ``claude_code`` failure surfaced to the caller."""
