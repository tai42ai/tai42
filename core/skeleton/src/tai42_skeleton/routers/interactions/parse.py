"""Shared JSON-body parse-error classification for the interactions HTTP doors."""

from __future__ import annotations

# Invalid JSON in an untrusted body converts to a loud 400; any exception outside
# this set is a server bug and propagates as a 500. RecursionError covers a
# deeply-nested body blowing up the parser; ``json.JSONDecodeError`` is a
# ``ValueError``.
JSON_PARSE_ERRORS = (ValueError, RecursionError)
