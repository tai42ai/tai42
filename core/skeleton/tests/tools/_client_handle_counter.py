"""Shared state of the client-handle fixture tools, importable without registering them."""

from __future__ import annotations

BODY_RUNS: dict[str, int] = {}

OVERLONG_NAME = "handle_" + "x" * 58
