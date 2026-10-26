"""Op-level oracles for the tool-extension operations' defensive branches that the
route-level suite (``tests/routers/test_tool_extensions.py``) cannot reach without
bypassing the boot-time duplicate-name collision: the multi-owner reject and the
other-config mcp mapper. The happy paths and every 400/404/409/503 the route emits
are pinned by the route suite over the real reload path."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from tai42_contract.app import tai42_app
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import BadRequestError
from tai42_skeleton.operations import tool_extensions as ops
from tai42_skeleton.operations.response_models_group_b import ToolExtensionCombosModified, ToolExtensionsView

from .._fakes.bus import FakeBus
from .._fakes.combo_check_app import CHECK_DOCUMENT, LiveStore, booted_check_app, clean_server, live_store
from .._fakes.write_span import ExclusiveWriteStore


def _fake_manifest(*, tools: list, mcp: list) -> Manifest:
    return cast(Manifest, SimpleNamespace(tools=tools, mcp=mcp))


def test_other_mappers_reports_mcp_config_carrying_the_name():
    # Owner is a tools config; an mcp config OTHER than the owner also maps the
    # name — it is reported so the route can 409 rather than orphan those combos.
    manifest = _fake_manifest(
        tools=[SimpleNamespace(module="mod", extensions={})],
        mcp=[SimpleNamespace(title="srv", extensions={"shout": [["marka"]]})],
    )
    others = ops._other_mappers(manifest, "shout", ("tools", "mod"))
    assert others == ["mcp:srv"]


def test_other_mappers_excludes_the_owning_mcp_config():
    manifest = _fake_manifest(
        tools=[],
        mcp=[SimpleNamespace(title="srv", extensions={"shout": [["marka"]]})],
    )
    assert ops._other_mappers(manifest, "shout", ("mcp", "srv")) == []


async def test_set_tool_extensions_multiple_owners_is_bad_request(monkeypatch):
    monkeypatch.setattr(ops, "_live_manifest", lambda: SimpleNamespace())
    monkeypatch.setattr(
        ops,
        "_owning_configs",
        lambda _manifest, _name: [("tools", "a"), ("mcp", "b")],
    )
    with pytest.raises(BadRequestError, match="provided by multiple configs"):
        await ops.set_tool_extensions("shout", [["marka"]])


async def test_modify_combos_multiple_owners_is_bad_request(monkeypatch):
    # The registration check passes, but two configs claim the tool — the shared
    # owner-resolution refuses (400) before any membership or write, exactly as the
    # set door does (this branch is unreachable at route level: two configs binding
    # one name collide at boot).
    monkeypatch.setattr(instance.app.tools, "has_tool", AsyncMock(return_value=True))
    monkeypatch.setattr(ops, "_live_manifest", lambda: SimpleNamespace())
    monkeypatch.setattr(ops, "_owning_configs", lambda _manifest, _name: [("tools", "a"), ("mcp", "b")])
    with pytest.raises(BadRequestError, match="provided by multiple configs"):
        await ops.modify_tool_extension_combos("shout", add=[["marka"]])


class _MutateStore(ExclusiveWriteStore):
    """A config manager whose ``mutate_manifest`` runs the guarded mutator on a copy of
    the stored document and persists only if it returns without raising — the seam the
    set_tool_extensions pipeline drives (a raise inside leaves the store untouched). The
    stored env the manifest's markers are validated against is empty."""

    def __init__(self, *, manifest: dict) -> None:
        super().__init__()
        self.manifest = manifest
        self.persisted: list[dict] = []

    def mutate_manifest(self, mutator: Any) -> dict:
        document = deepcopy(self.manifest)
        mutator(document)  # a raise here propagates before any persist
        self.manifest = document
        self.persisted.append(deepcopy(document))
        return document

    def read_manifest_preserved(self) -> dict:
        return deepcopy(self.manifest)

    def read_env(self) -> dict:
        return {}


class _ReloadAdmin:
    def reload_config(self) -> dict:
        return {"status": "ok", "env_keys": 0}

    async def served_mcp_names(self, config: Any) -> list[str]:
        return []


async def test_set_tool_extensions_backend_without_bus_maps_to_400(monkeypatch):
    # The owner-resolution / conflict / registry checks pass, so the write reaches
    # ConfigService; the persisted manifest registers a task backend with no bus, so the
    # pipeline raises the RuntimeError ``BackendNeedsBusError`` at MUTATE time. The op
    # must map it to a loud 400 naming TAI_BUS_REDIS_URL, not let it escape as a 500.
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()
    try:
        monkeypatch.setattr(ops, "_live_manifest", lambda: SimpleNamespace())
        monkeypatch.setattr(ops, "_owning_configs", lambda _manifest, _name: [("tools", "mod")])
        monkeypatch.setattr(ops, "_other_mappers", lambda _manifest, _name, _owner: [])
        monkeypatch.setattr(ops, "_validate_combos_against_registry", lambda _combos: None)

        store = _MutateStore(manifest={"backend_module": "myapp.backend", "tools": [{"title": "T", "module": "mod"}]})
        impl = SimpleNamespace(
            config=SimpleNamespace(config_manager=store),
            admin=_ReloadAdmin(),
            backends=SimpleNamespace(backend=None),
        )
        monkeypatch.setattr(tai42_app, "_impl", impl)
        monkeypatch.setattr(instance.app, "_bus", FakeBus())

        with pytest.raises(BadRequestError, match="TAI_BUS_REDIS_URL"):
            await ops.set_tool_extensions("shout", [["marka"]])

        assert store.persisted == []  # rejected in validation, before any persist
    finally:
        reset_all_settings()


async def test_modify_combos_backend_without_bus_maps_to_400(monkeypatch):
    # The registration/owner/membership/registry checks pass, so the merge write
    # reaches ConfigService; the persisted manifest registers a task backend with no
    # bus, so the pipeline raises ``BackendNeedsBusError`` at MUTATE time. The op maps
    # it to a loud 400 naming TAI_BUS_REDIS_URL, exactly as the set door does.
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()
    try:
        monkeypatch.setattr(instance.app.tools, "has_tool", AsyncMock(return_value=True))
        monkeypatch.setattr(ops, "_live_manifest", lambda: SimpleNamespace())
        monkeypatch.setattr(ops, "_owning_configs", lambda _manifest, _name: [("tools", "mod")])
        monkeypatch.setattr(ops, "_other_mappers", lambda _manifest, _name, _owner: [])
        monkeypatch.setattr(ops, "_stored_owner_combos", lambda _document, _owner, _name: [])
        monkeypatch.setattr(ops, "_validate_combos_against_registry", lambda _combos: None)

        store = _MutateStore(manifest={"backend_module": "myapp.backend", "tools": [{"title": "T", "module": "mod"}]})
        impl = SimpleNamespace(
            config=SimpleNamespace(config_manager=store),
            admin=_ReloadAdmin(),
            backends=SimpleNamespace(backend=None),
        )
        monkeypatch.setattr(tai42_app, "_impl", impl)
        monkeypatch.setattr(instance.app, "_bus", FakeBus())

        with pytest.raises(BadRequestError, match="TAI_BUS_REDIS_URL"):
            await ops.modify_tool_extension_combos("shout", add=[["marka"]])

        assert store.persisted == []  # rejected before any persist
    finally:
        reset_all_settings()


# -- the stored combos over a booted app (tests.app._fixtures.ext_check on base ``echo``) ---

_MARKED_LABEL = {"name": "labelled", "config": {"label": "!ENV ${EXT_TOKEN}"}}
_ACCEPTED = {"name": "refusing", "config": {"label": "ok"}}
_TOKEN = "s3cr3t-token"
_REQUIRED_LABEL = (
    "invalid extensions for tool 'echo': tools[a].extensions[echo][0]: extension 'labelled' on tool 'echo' cannot "
    "bind: extension 'labelled' config: value does not match schema at $: breaks the schema's 'required' rule: "
    "'label' (the value is not shown)"
)


def _booted(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    async def run() -> None:
        async with booted_check_app():
            await body()

    asyncio.run(run())


def _view_json(answer: dict) -> str:
    return ToolExtensionsView.model_validate(answer).model_dump_json()


@pytest.mark.usefixtures(clean_server.__name__, live_store.__name__)
def test_settings_breaking_the_declared_schema_are_refused_and_matching_ones_persist(live_store: LiveStore) -> None:
    async def body() -> None:
        for write in (
            lambda: ops.set_tool_extensions("echo", [[{"name": "labelled", "config": {}}]]),
            lambda: ops.modify_tool_extension_combos("echo", add=[[{"name": "labelled", "config": {}}]]),
        ):
            with pytest.raises(BadRequestError) as caught:
                await write()
            assert str(caught.value) == _REQUIRED_LABEL
            assert live_store.manifest == CHECK_DOCUMENT
            assert live_store.manifest_writes == 0

        await ops.set_tool_extensions("echo", [[{"name": "labelled", "config": {"label": "ok"}}]])
        assert live_store.manifest["tools"][0]["extensions"] == {
            "echo": [[{"name": "labelled", "config": {"label": "ok"}}]]
        }

    _booted(body)


@pytest.mark.usefixtures(clean_server.__name__, live_store.__name__)
def test_a_dangling_marker_is_refused_before_the_settings_are_checked(
    live_store: LiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EXT_UNSET", raising=False)

    async def body() -> None:
        with pytest.raises(BadRequestError) as caught:
            await ops.set_tool_extensions("echo", [[{"name": "labelled_n", "config": {"n": "!ENV ${EXT_UNSET}"}}]])
        assert str(caught.value) == (
            "invalid extensions for tool 'echo': Refusing a change that leaves manifest !ENV markers dangling — "
            'these references resolve to no env var and would silently run on "N/A": EXT_UNSET '
            "(at /tools/0/extensions/echo/0/0/config/n)."
        )
        assert live_store.manifest_writes == 0

    _booted(body)


@pytest.mark.usefixtures(clean_server.__name__, live_store.__name__)
def test_the_combos_are_served_as_stored_with_their_markers(
    live_store: LiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXT_TOKEN", _TOKEN)

    async def body() -> None:
        await ops.set_tool_extensions("echo", [[_MARKED_LABEL]])
        assert live_store.manifest["tools"][0]["extensions"] == {"echo": [[_MARKED_LABEL]]}

        served = await ops.get_tool_extensions("echo")
        assert served["combos"] == [[_MARKED_LABEL]]
        assert _TOKEN not in _view_json(served)

        added = await ops.modify_tool_extension_combos("echo", add=[[_ACCEPTED]])
        assert added["combos"] == [[_MARKED_LABEL], [_ACCEPTED]]
        assert _TOKEN not in _view_json(added)
        assert live_store.manifest["tools"][0]["extensions"] == {"echo": [[_MARKED_LABEL], [_ACCEPTED]]}

        removed = await ops.modify_tool_extension_combos("echo", remove=[[_MARKED_LABEL]])
        assert removed["combos"] == [[_ACCEPTED]]
        cleared = await ops.modify_tool_extension_combos("echo", remove=[[_ACCEPTED]])
        assert cleared["combos"] == []
        assert "extensions" not in live_store.manifest["tools"][0]

    _booted(body)


def test_the_stored_combos_of_a_base_are_answered_in_the_manifest_union_order() -> None:
    document = {
        "tools": [{"title": "t", "module": "m", "extensions": {"echo": [["exta"]]}}],
        "mcp": [{"title": "s", "extensions": {"echo": ["extb"], "other": [["exta"]]}}],
        "api_tools": {"extensions": {"echo": [[_MARKED_LABEL]]}},
    }
    assert ops._stored_tool_combos(document, "echo") == [["exta"], ["extb"], [_MARKED_LABEL]]
    assert ops._stored_owner_combos(document, ("mcp", "s"), "echo") == [["extb"]]


@pytest.mark.usefixtures(clean_server.__name__, live_store.__name__)
def test_a_never_written_store_answers_no_combos(live_store: LiveStore, monkeypatch: pytest.MonkeyPatch) -> None:
    def never_written() -> dict:
        raise FileNotFoundError("no stored manifest")

    monkeypatch.setattr(instance.app.config.config_manager, "read_manifest_preserved", never_written)

    async def body() -> None:
        assert (await ops.get_tool_extensions("echo"))["combos"] == []

    _booted(body)


@pytest.mark.usefixtures(clean_server.__name__, live_store.__name__)
def test_a_combo_edit_whose_span_fails_to_release_answers_the_store_s_error(live_store: LiveStore) -> None:
    async def body() -> None:
        live_store.fail_exit = OSError("releasing the span failed")
        added = await ops.modify_tool_extension_combos("echo", add=[[_ACCEPTED]])
        assert added["combos"] == [[_ACCEPTED]]
        assert added["span_release_error"] == "OSError: releasing the span failed"
        assert ToolExtensionCombosModified.model_validate(added).span_release_error == added["span_release_error"]
        assert live_store.manifest["tools"][0]["extensions"] == {"echo": [[_ACCEPTED]]}

        live_store.fail_exit = None
        removed = await ops.modify_tool_extension_combos("echo", remove=[[_ACCEPTED]])
        assert removed["span_release_error"] is None

    _booted(body)


@pytest.mark.usefixtures(clean_server.__name__, live_store.__name__)
def test_two_concurrent_combo_adds_both_land(live_store: LiveStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXT_TOKEN", _TOKEN)

    async def body() -> None:
        first, second = await asyncio.gather(
            ops.modify_tool_extension_combos("echo", add=[[_ACCEPTED]]),
            ops.modify_tool_extension_combos("echo", add=[[_MARKED_LABEL]]),
        )
        stored = live_store.manifest["tools"][0]["extensions"]["echo"]
        assert sorted(map(str, stored)) == sorted(map(str, [[_ACCEPTED], [_MARKED_LABEL]]))
        assert [_ACCEPTED] in first["combos"]
        assert [_MARKED_LABEL] in second["combos"]
        assert stored in (first["combos"], second["combos"])

    _booted(body)


@pytest.mark.usefixtures(clean_server.__name__, live_store.__name__)
def test_a_combo_edit_answers_the_combos_as_the_seal_stored_them(
    live_store: LiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXT_TOKEN", _TOKEN)
    resolved_label = {"name": "labelled", "config": {"label": _TOKEN}}

    async def body() -> None:
        await ops.set_tool_extensions("echo", [[_MARKED_LABEL]])
        answer = await ops.modify_tool_extension_combos("echo", remove=[[_MARKED_LABEL]], add=[[resolved_label]])
        stored = live_store.manifest["tools"][0]["extensions"]["echo"]
        assert stored == [[_MARKED_LABEL]]
        assert answer["combos"] == stored
        assert _TOKEN not in _view_json(answer)

    _booted(body)
