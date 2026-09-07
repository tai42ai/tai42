"""``tai catalog`` — the marketplace-backed catalog.

Covers the static skeleton-builtin table (pinned to the ACTUAL builtin registrations
so it cannot rot silently), the projection of a marketplace item-enumeration row into
the catalog columns (mcp-server rows render an EMPTY module cell; the source cell is
``"<namespace>/<listing>"``), and the loud-offline behaviour (a dead registry is a CLI
error, never a silent empty list or cache).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner
from tai42_cli import app as app_module
from tai42_contract.plugins import PluginItemKind

from tai42_skeleton.cli.native import catalog
from tai42_skeleton.marketplace.errors import RegistryResponseError, RegistryUnreachableError

# The kinds a catalog entry may declare — the contract's plugin-item vocabulary.
# Pinned to the enum below so it cannot silently drift from the source of truth.
_VALID_KINDS = {
    "tool",
    "agent",
    "extension",
    "connector",
    "channel",
    "backend",
    "storage",
    "sandbox",
    "monitoring",
    "webhook-verifier",
    "config",
    "identity",
    "studio-plugin",
    "router",
    "middleware",
    "mcp-server",
}


class _FakeClient:
    """A stand-in :class:`RegistryClient`: ``items`` returns the seeded rows, or
    raises the seeded error to exercise the loud-offline path."""

    def __init__(self, *, items: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self._items = items or []
        self._error = error

    async def items(self, kind: str | None = None) -> list[dict[str, Any]]:
        if self._error is not None:
            raise self._error
        return self._items


def _patch_client(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    monkeypatch.setattr(catalog, "RegistryClient", lambda *a, **k: _FakeClient(**kwargs))


def test_valid_kinds_track_the_contract() -> None:
    # The enum is the single source of truth for kinds; if it grows or renames a
    # member this fails loudly so the catalog assertions get updated in lockstep.
    assert {kind.value for kind in PluginItemKind} == _VALID_KINDS


# The registrar attribute chain each builtin kind registers through → the catalog
# ``kind`` it corresponds to. Matched by suffix (the ``tai42_app.`` prefix is elided),
# so the guard reads BOTH the registered name AND its kind from the source of truth.
_REGISTRAR_KIND: dict[str, str] = {
    "tools.tool": "tool",
    "extensions.extension": "extension",
    "webhook_verifiers.register": "webhook-verifier",
}


def _attr_chain(node: Any) -> str | None:
    """The dotted attribute path of an ``a.b.c`` reference node, or ``None`` when it
    does not bottom out in a bare name."""
    import ast

    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _match_registrar(chain: str | None) -> str | None:
    """The catalog kind a registrar attribute chain corresponds to (suffix match), or
    ``None`` when the chain is not a builtin registrar."""
    if chain is None:
        return None
    for suffix, kind in _REGISTRAR_KIND.items():
        if chain.endswith(suffix):
            return kind
    return None


def _kw_str(call: Any, key: str) -> str | None:
    import ast

    for keyword in call.keywords:
        if keyword.arg == key and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
    return None


def _registrations(dotted: str) -> set[tuple[str, str]]:
    """The ``(kind, name)`` registrations a builtin module declares, read from its source
    WITHOUT executing it (``find_spec`` reads the origin only).

    Executing a builtin's module has global side effects (the registration decorators
    fire against the live app + a rebuilt registry that rejects duplicates), so the
    honesty check reads the source statically. Each builtin registers through exactly one
    registrar: a tool decorates a ``def`` (``@tai42_app.tools.tool(...)`` → the def name),
    an extension carries an explicit ``name=`` (``@tai42_app.extensions.extension(name=...)``),
    a verifier is a module-level ``webhook_verifiers.register("name", ...)`` call — so both
    the kind AND the registered name come from the source of truth, and a kind flip or a
    rename fails the guard."""
    import ast
    import importlib.util
    from pathlib import Path

    spec = importlib.util.find_spec(dotted)
    assert spec is not None, f"builtin module {dotted!r} not found"
    assert spec.origin, f"builtin module {dotted!r} has no source origin"
    tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    regs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                kind = _match_registrar(_attr_chain(decorator.func))
                if kind == "tool":
                    regs.add((kind, node.name))
                elif kind == "extension":
                    name = _kw_str(decorator, "name")
                    if name is not None:
                        regs.add((kind, name))
        elif isinstance(node, ast.Call):
            kind = _match_registrar(_attr_chain(node.func))
            if kind == "webhook-verifier" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    regs.add((kind, first.value))
    return regs


def test_builtin_rows_match_registrations() -> None:
    # The static _BUILTIN_ROWS table is the only place the skeleton builtins are
    # enumerated. Pin each row's (kind, name) to its module's ACTUAL registration read
    # from source, so a builtin renamed, moved, OR its kind flipped fails this test
    # rather than silently rotting the table.
    for row in catalog._BUILTIN_ROWS:
        assert row["source"] == "builtin"
        assert row["package"] == "tai42-skeleton"
        assert set(row) == set(catalog._COLUMNS)
        assert (row["kind"], row["name"]) in _registrations(row["module"]), (
            f"({row['kind']!r}, {row['name']!r}) is not the registration in {row['module']!r} — the "
            "builtin's kind or name changed; update _BUILTIN_ROWS to match the registration"
        )


def test_load_catalog_projects_marketplace_items(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        {
            "kind": "tool",
            "name": "generate_uuid",
            "module": "tai42_toolbox.tools.generate_uuid",
            "description": "Generate a random UUID (version 4).",
            "namespace": "tai42",
            "listing": "toolbox",
            "package": "tai42-toolbox",
        },
        {
            "kind": "mcp-server",
            "name": "postgres",
            "module": None,
            "description": "A managed Postgres MCP server.",
            "namespace": "tai42",
            "listing": "postgres-mcp",
            "package": "tai42-postgres-mcp",
        },
    ]
    _patch_client(monkeypatch, items=items)
    records = catalog.load_catalog()

    # The builtins lead, verbatim, followed by the projected marketplace rows.
    assert records[: len(catalog._BUILTIN_ROWS)] == catalog._BUILTIN_ROWS

    uuid_row = next(record for record in records if record["name"] == "generate_uuid")
    assert uuid_row["source"] == "tai42/toolbox"
    assert uuid_row["module"] == "tai42_toolbox.tools.generate_uuid"
    assert uuid_row["package"] == "tai42-toolbox"

    pg_row = next(record for record in records if record["name"] == "postgres")
    # An mcp-server item has no module — the cell is EMPTY, never a phantom "None".
    assert pg_row["module"] == ""
    assert pg_row["source"] == "tai42/postgres-mcp"
    assert set(pg_row) == set(catalog._COLUMNS)


def test_catalog_json_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(
        monkeypatch,
        items=[
            {
                "kind": "tool",
                "name": "generate_uuid",
                "module": "tai42_toolbox.tools.generate_uuid",
                "description": "d",
                "namespace": "tai42",
                "listing": "toolbox",
                "package": "tai42-toolbox",
            }
        ],
    )
    result = CliRunner().invoke(app_module.app, ["--json", "catalog"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    assert {"name", "kind", "source", "package", "module", "description"} <= set(data[0])


def test_catalog_json_trailing_flag_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    # The habitual trailing form ``tai catalog --json`` must parse the same as the
    # flag-first ``tai --json catalog``.
    _patch_client(monkeypatch, items=[])
    result = CliRunner().invoke(app_module.app, ["catalog", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list)
    # With no marketplace rows the builtins still render.
    assert {"name", "kind", "source"} <= set(data[0])


def test_catalog_table_renders_builtin_source(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, items=[])
    result = CliRunner().invoke(app_module.app, ["catalog"])
    assert result.exit_code == 0, result.output
    assert "source" in result.output
    assert "builtin" in result.output
    assert "ask_user" in result.output


def test_offline_registry_is_a_loud_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A dead registry surfaces as the uniform CLI error line — never a silent empty
    # list or a cached fallback, and never a raw traceback.
    _patch_client(monkeypatch, error=RegistryUnreachableError("marketplace registry unreachable at https://x"))
    result = CliRunner().invoke(app_module.app, ["catalog"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "unreachable" in result.output


def test_garbled_registry_response_is_a_loud_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch, error=RegistryResponseError("missing the 'items' key", status=None))
    result = CliRunner().invoke(app_module.app, ["catalog"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "items" in result.output


def test_item_row_missing_identity_field_is_a_loud_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A dict-shaped item row missing an identity field (here ``package``) must read as the
    # uniform CLI error, never a bare KeyError escaping as a raw traceback.
    _patch_client(
        monkeypatch,
        items=[
            {
                "kind": "tool",
                "name": "generate_uuid",
                "module": "tai42_toolbox.tools.generate_uuid",
                "description": "d",
                "namespace": "tai42",
                "listing": "toolbox",
                # "package" deliberately omitted
            }
        ],
    )
    result = CliRunner().invoke(app_module.app, ["catalog"])
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "package" in result.output
