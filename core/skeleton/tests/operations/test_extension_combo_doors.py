"""Every manifest write door refuses a combo the live dry-run bind refuses, with the gate's text.

The app serves base tool ``echo`` under the ``tools`` entry ``a`` with the extensions of
:mod:`tests.app._fixtures.ext_check`; ``refusing`` refuses the setting ``label == "x"`` with
``bad label``. Each door answers the refusal through its own error, and the stored manifest and
env are left as they were.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Coroutine
from typing import Any, cast

import pytest
from tai42_kit.utils.data import dump_manifest

from tai42_skeleton.marketplace.installer import Installer
from tai42_skeleton.operations import BadRequestError
from tai42_skeleton.operations import config as config_ops
from tai42_skeleton.operations import marketplace as marketplace_ops
from tai42_skeleton.operations import tool_extensions as tool_extension_ops
from tai42_skeleton.operations.backup import import_backup
from tai42_skeleton.operations.manifest import api_tools as api_tools_ops
from tai42_skeleton.operations.manifest import runtime as runtime_ops
from tai42_skeleton.operations.manifest import secret_env as secret_env_ops
from tai42_skeleton.operations.manifest import sections as section_ops

from .._fakes.combo_check_app import CHECK_DOCUMENT, LiveStore, booted_check_app, clean_server, live_store
from ..marketplace._specs import make_resolved, make_spec, tool_item
from ..marketplace.test_installer import Harness

pytestmark = pytest.mark.usefixtures(clean_server.__name__, live_store.__name__)

_REFUSED = {"name": "refusing", "config": {"label": "x"}}
_REFUSAL = "tools[a].extensions[echo][0]: extension 'refusing' on tool 'echo' cannot bind: bad label"
_MARKED = {"name": "refusing", "config": {"label": "!ENV ${EXT_L}"}}


def _with_extensions(combos: list[list[Any]]) -> dict[str, Any]:
    document = copy.deepcopy(CHECK_DOCUMENT)
    document["tools"][0]["extensions"] = {"echo": combos}
    return document


def _booted(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    async def run() -> None:
        async with booted_check_app():
            await body()

    asyncio.run(run())


def _assert_untouched(store: LiveStore, manifest: dict[str, Any], env: dict[str, str]) -> None:
    assert store.manifest == manifest
    assert store.env == env
    assert store.manifest_writes == 0
    assert store.env_writes == 0


def test_the_tool_extension_doors_refuse_with_the_gate_text(live_store: LiveStore) -> None:
    async def body() -> None:
        with pytest.raises(BadRequestError) as replaced:
            await tool_extension_ops.set_tool_extensions("echo", [[_REFUSED]])
        assert str(replaced.value) == f"invalid extensions for tool 'echo': {_REFUSAL}"

        with pytest.raises(BadRequestError) as added:
            await tool_extension_ops.modify_tool_extension_combos("echo", add=[[_REFUSED]])
        assert str(added.value) == f"invalid extensions for tool 'echo': {_REFUSAL}"
        _assert_untouched(live_store, CHECK_DOCUMENT, {})

    _booted(body)


def test_the_tools_section_door_refuses_with_the_gate_text(live_store: LiveStore) -> None:
    async def body() -> None:
        entry = _with_extensions([[_REFUSED]])["tools"][0]
        with pytest.raises(BadRequestError) as caught:
            await section_ops.add_tools_entries([entry], replace=True)
        assert str(caught.value) == f"invalid tools config: {_REFUSAL}"
        _assert_untouched(live_store, CHECK_DOCUMENT, {})

    _booted(body)


def test_the_api_tools_door_refuses_a_document_carrying_a_refused_combo(live_store: LiveStore) -> None:
    stored = {**copy.deepcopy(CHECK_DOCUMENT), "api_tools": {"extensions": {"echo": [["sameret"], [_REFUSED]]}}}
    live_store.manifest = copy.deepcopy(stored)

    async def body() -> None:
        with pytest.raises(BadRequestError) as caught:
            await api_tools_ops.update_api_tools(include_add=["list_tools"])
        assert str(caught.value) == (
            "invalid api_tools config: api_tools.extensions[echo][0]: extension 'sameret' on tool 'echo' cannot "
            "bind: Extension 'sameret' returned the same name 'echo' as the original tool. Extensions must return "
            "a new name to create a branch."
        )
        _assert_untouched(live_store, stored, {})

    _booted(body)


def test_the_whole_manifest_door_refuses_with_the_gate_text(live_store: LiveStore) -> None:
    async def body() -> None:
        with pytest.raises(BadRequestError) as caught:
            await runtime_ops.update_manifest(dump_manifest(cast("Any", _with_extensions([[_REFUSED]]))))
        assert str(caught.value) == f"invalid manifest: {_REFUSAL}"
        _assert_untouched(live_store, CHECK_DOCUMENT, {})

    _booted(body)


def test_the_secret_env_door_refuses_a_marker_breaking_a_declared_schema(live_store: LiveStore) -> None:
    secret = "s3cr3t-door-value"
    stored = {
        **copy.deepcopy(CHECK_DOCUMENT),
        "mcp": [
            {
                "title": "m",
                "config": {"url": "http://127.0.0.1:1/mcp"},
                "extensions": {"fetch": [[{"name": "labelled_n", "config": {"n": 1}}]]},
            }
        ],
    }
    live_store.manifest = copy.deepcopy(stored)

    async def body() -> None:
        with pytest.raises(BadRequestError) as caught:
            await secret_env_ops.set_mcp_secret_env(secret, "mcp/0/extensions/fetch/0/0/config/n", key_hint="ext n")
        assert str(caught.value) == (
            "mcp[m].extensions[fetch][0]: extension 'labelled_n' on tool 'fetch' cannot bind: extension "
            "'labelled_n' config: value does not match schema at $.n: breaks the schema's 'type' rule: 'integer' "
            "(the value is not shown)"
        )
        assert secret not in str(caught.value)
        _assert_untouched(live_store, stored, {})

    _booted(body)


def test_the_env_door_refuses_a_marker_turning_a_setting_refused(live_store: LiveStore) -> None:
    stored = _with_extensions([[_MARKED]])
    live_store.manifest = copy.deepcopy(stored)
    live_store.env = {"EXT_L": "ok"}

    async def body() -> None:
        with pytest.raises(BadRequestError) as caught:
            await config_ops.write_env({"EXT_L": "x"})
        assert str(caught.value) == _REFUSAL
        _assert_untouched(live_store, stored, {"EXT_L": "ok"})

    _booted(body)


def test_the_backup_restore_records_the_refusal_as_the_section_error(live_store: LiveStore) -> None:
    stored = _with_extensions([[_MARKED]])
    live_store.manifest = copy.deepcopy(stored)
    live_store.env = {"EXT_L": "ok"}

    async def body() -> None:
        restored = (
            await import_backup(
                {"version": 1, "sections": {"manifest": _with_extensions([[_REFUSED]]), "env": {"EXT_L": "x"}}},
                ["manifest", "env"],
            )
        ).payload
        assert restored.ok is False
        assert restored.sections["manifest"].errors == [_REFUSAL]
        assert restored.sections["env"].errors == [_REFUSAL]
        _assert_untouched(live_store, stored, {"EXT_L": "ok"})

    _booted(body)


def _stored_with_refused_combo(store: LiveStore, entry: dict[str, Any] | None = None) -> dict[str, Any]:
    """Store a manifest whose ``a`` entry carries a combo the bind refuses, plus ``entry`` when given."""
    stored = _with_extensions([[_REFUSED]])
    if entry is not None:
        stored["tools"].append(entry)
    store.manifest = copy.deepcopy(stored)
    return stored


def _marketplace(monkeypatch: pytest.MonkeyPatch, harness: Harness) -> None:
    """Serve the marketplace operations through an installer over the live config pipeline."""
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "x")
    monkeypatch.setattr(
        marketplace_ops,
        "Installer",
        lambda: Installer(
            registry=cast("Any", harness.registry),
            pip_runner=harness.pip,
            store=cast("Any", harness.store),
            fleet_lock=harness.fleet,
        ),
    )


def test_the_marketplace_install_door_refuses_with_the_gate_text(
    live_store: LiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness()
    spec = make_spec(provides=[tool_item(module="pkg.tools.uuid")])
    harness.registry.resolved = make_resolved(spec, version="1.0.0")
    _marketplace(monkeypatch, harness)
    stored = _stored_with_refused_combo(live_store)

    async def body() -> None:
        with pytest.raises(BadRequestError) as caught:
            await marketplace_ops.marketplace_install("tai42/toolbox")
        assert str(caught.value) == _REFUSAL
        _assert_untouched(live_store, stored, {})
        # The refusal unwinds the install: the just-installed package is removed, no row is recorded.
        assert harness.pip.calls[-1] == ["uninstall", "--yes", "tai42-toolbox"]
        assert harness.store.record_calls == []

    _booted(body)


def test_the_marketplace_update_door_refuses_with_the_gate_text(
    live_store: LiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness()
    old = make_spec(version="1.0.0", provides=[tool_item(module="pkg.old")])
    harness.store.preload(old, version="1.0.0")
    harness.registry.resolved = make_resolved(make_spec(version="2.0.0", provides=[tool_item(module="pkg.new")]))
    _marketplace(monkeypatch, harness)
    stored = _stored_with_refused_combo(live_store, {"title": "pkg.old", "module": "pkg.old"})

    async def body() -> None:
        with pytest.raises(BadRequestError) as caught:
            await marketplace_ops.marketplace_update("tai42/toolbox")
        assert str(caught.value) == _REFUSAL
        _assert_untouched(live_store, stored, {})
        # The refusal unwinds the update: the prior version is reinstalled and its row stays.
        assert harness.pip.calls[-1][0] == "install"
        assert "tai42-toolbox==1.0.0" in harness.pip.calls[-1]
        assert harness.store.rows["tai42/toolbox"].version == "1.0.0"
        assert harness.store.record_calls == []

    _booted(body)


def test_the_marketplace_uninstall_door_refuses_with_the_gate_text(
    live_store: LiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness()
    spec = make_spec(provides=[tool_item(module="pkg.tools.uuid")])
    harness.store.preload(spec, version="1.0.0")
    _marketplace(monkeypatch, harness)
    stored = _stored_with_refused_combo(live_store, {"title": "pkg.tools.uuid", "module": "pkg.tools.uuid"})

    async def body() -> None:
        with pytest.raises(BadRequestError) as caught:
            await marketplace_ops.marketplace_uninstall("tai42/toolbox")
        assert str(caught.value) == _REFUSAL
        _assert_untouched(live_store, stored, {})
        # Refused before anything changed: the package stays installed and its row stays.
        assert harness.pip.calls == []
        assert "tai42/toolbox" in harness.store.rows

    _booted(body)


def test_a_combo_the_bind_accepts_passes_every_door(live_store: LiveStore) -> None:
    async def body() -> None:
        await tool_extension_ops.set_tool_extensions("echo", [[{"name": "refusing", "config": {"label": "ok"}}]])
        assert live_store.manifest["tools"][0]["extensions"] == {
            "echo": [[{"name": "refusing", "config": {"label": "ok"}}]]
        }

    _booted(body)
