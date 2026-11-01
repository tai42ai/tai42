"""Placing a plugin spec: the environment it belongs in and the commands a server build must expose."""

from __future__ import annotations

from tai42_contract.plugins import PluginSpec

from tai42_skeleton.marketplace.environments.targets import ServerCommand, place


def _spec(*items: dict[str, object], **fields: object) -> PluginSpec:
    return PluginSpec.model_validate(
        {
            **fields,
            "spec_version": 1,
            "namespace": "tai42",
            "name": "srv",
            "package": "tai42-srv",
            "version": "1.0.0",
            "description": "A fixture plugin.",
            "license": "Apache-2.0",
            "categories": ["dev"],
            "provides": list(items),
        }
    )


def _server_item(name: str, mcp: dict[str, object]) -> dict[str, object]:
    return {"kind": "mcp-server", "name": name, "description": "A fixture server.", "mcp": mcp}


def test_a_server_package_pin_carries_every_command_its_mcp_server_items_name() -> None:
    spec = _spec(
        _server_item("srv_mcp", {"command": "srv-mcp", "args": ["--stdio"]}),
        _server_item("srv_remote", {"url": "https://srv.example/mcp"}),
        _server_item("srv_admin", {"command": "srv-admin"}),
    )

    placed = place("tai42/srv", spec, "1.0.0", "pypi", None, None)

    assert placed is not None
    assert placed.runs_as_server
    assert placed.pin.server_commands == (
        ServerCommand(item="srv_mcp", command="srv-mcp"),
        ServerCommand(item="srv_admin", command="srv-admin"),
    )


def test_an_in_process_plugin_pin_carries_no_server_command() -> None:
    spec = _spec(
        {"kind": "tool", "name": "srv_tool", "description": "A fixture tool.", "module": "tai42_srv.tool"},
        contract=">=0.1,<1.0",
    )

    placed = place("tai42/srv", spec, "1.0.0", "pypi", None, None)

    assert placed is not None
    assert not placed.runs_as_server
    assert placed.pin.server_commands == ()
