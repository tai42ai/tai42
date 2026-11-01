"""The pi fixture's minimal stdio MCP server.

The ``tai-e2e-market-pi-mcp`` console script serves newline-delimited JSON-RPC over
stdin/stdout on the standard library alone: ``initialize``, ``ping``, ``tools/list``,
``tools/call`` and the empty resource and prompt listings, ignoring notifications. Its one
tool (``pool_url``) returns the ``E2E_PI_POOL_URL`` value the manifest entry's env handed it.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

_SERVER_INFO = {"name": "tai-e2e-market-pi", "version": "1"}
_DEFAULT_PROTOCOL = "2025-06-18"
_POOL_URL_TOOL = {
    "name": "pool_url",
    "description": "Return the pool URL the pi fixture server was launched with.",
    "inputSchema": {"type": "object", "properties": {}},
    "outputSchema": {"type": "object", "additionalProperties": True},
}
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    if params.get("name") != _POOL_URL_TOOL["name"]:
        raise _RpcError(_INVALID_PARAMS, f"unknown tool {params.get('name')!r}")
    payload = {"pool_url": os.environ.get("E2E_PI_POOL_URL")}
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "structuredContent": payload,
        "isError": False,
    }


def _answer(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", _DEFAULT_PROTOCOL),
            "capabilities": {"tools": {}},
            "serverInfo": _SERVER_INFO,
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [_POOL_URL_TOOL]}
    if method == "tools/call":
        return _call_tool(params)
    if method == "resources/list":
        return {"resources": []}
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "prompts/list":
        return {"prompts": []}
    raise _RpcError(_METHOD_NOT_FOUND, f"method {method!r} not found")


def _reply(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main() -> None:
    """Serve JSON-RPC requests from stdin until it closes (the console-script entry point)."""
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        if "id" not in request:
            continue  # a notification: nothing to answer
        try:
            result = _answer(request["method"], request.get("params") or {})
        except _RpcError as exc:
            _reply({"jsonrpc": "2.0", "id": request["id"], "error": {"code": exc.code, "message": str(exc)}})
            continue
        _reply({"jsonrpc": "2.0", "id": request["id"], "result": result})


if __name__ == "__main__":
    main()
