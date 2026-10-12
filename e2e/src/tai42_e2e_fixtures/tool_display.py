"""Display fields and behaviour hints the probe tools declare, importable without registering a tool.

A test reads them back from the MCP listing of the tool and of a preset over it.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations

E2E_ECHO_TITLE = "Echo"
E2E_ECHO_ANNOTATIONS = ToolAnnotations(readOnlyHint=True)
