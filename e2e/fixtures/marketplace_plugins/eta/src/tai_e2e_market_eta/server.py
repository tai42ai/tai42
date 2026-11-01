"""The eta fixture's minimal stdio MCP server.

The fixture provides a single ``mcp-server`` item whose ``mcp.command`` launches
this server over stdio (the ``tai-e2e-market-eta-mcp`` console script). Installing
the fixture builds its own server environment, writes the manifest ``mcp`` entry,
and the skeleton's MCP loader spawns this process and binds its one tool onto the
serving ``/mcp`` under the entry title — so a test proves an installed mcp-server
item really mounts and answers.

The server is written on the standard library alone: newline-delimited JSON-RPC
over stdin/stdout, answering ``initialize``, ``ping``, ``tools/list``,
``tools/call`` and the empty resource and prompt listings, and ignoring
notifications. Its one tool (``ping``) is implemented by the
``tai-e2e-market-etalib`` dependency, the only distribution its environment needs
beside the package itself."""

from __future__ import annotations

import json
import sys
from typing import Any

from tai_e2e_market_etalib import ping

_SERVER_INFO = {"name": "tai-e2e-market-eta", "version": "1"}
_DEFAULT_PROTOCOL = "2025-06-18"
_PING_TOOL = {
    "name": "ping",
    "description": "Return a fixed marker payload identifying the eta fixture MCP server.",
    "inputSchema": {"type": "object", "properties": {}},
    # A free-form object, so an MCP client hands the result back as a plain mapping.
    "outputSchema": {"type": "object", "additionalProperties": True},
}
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def _call_tool(params: dict[str, Any]) -> dict[str, Any]:
    if params.get("name") != _PING_TOOL["name"]:
        raise _RpcError(_INVALID_PARAMS, f"unknown tool {params.get('name')!r}")
    payload = ping()
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
        return {"tools": [_PING_TOOL]}
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
