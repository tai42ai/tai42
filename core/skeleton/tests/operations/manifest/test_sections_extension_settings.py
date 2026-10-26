"""The section doors check extension settings against the declared schema before storing.

The app serves base tool ``echo`` under the ``tools`` entry ``a``; ``labelled`` declares a required,
non-empty ``label`` and ``labelled_n`` a required integer ``n``. No base ``fetch`` is bound, so a
combo on it reaches no factory, yet its settings are still checked against the declared schema.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Callable, Coroutine
from typing import Any

import jsonschema
import pytest
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError

from tai42_skeleton.operations import BadRequestError
from tai42_skeleton.operations._broadcast import FleetBroadcastError
from tai42_skeleton.operations.manifest import sections as section_ops

from ..._fakes.combo_check_app import (
    CHECK_DOCUMENT,
    LiveStore,
    booted_check_app,
    clean_server,
    factory_calls,
    live_store,
)

pytestmark = pytest.mark.usefixtures(clean_server.__name__, live_store.__name__)

_REQUIRED_LABEL = (
    "extension 'labelled' config: value does not match schema at $: breaks the schema's 'required' rule: "
    "'label' (the value is not shown)"
)
_LATE_URL = "http://127.0.0.1:1/mcp"


def _late(extensions: dict[str, Any]) -> dict[str, Any]:
    return {"title": "late", "config": {"url": _LATE_URL}, "extensions": extensions}


def _booted(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    async def run() -> None:
        async with booted_check_app():
            factory_calls().clear()
            await body()
            assert factory_calls() == []

    asyncio.run(run())


def _chain(exc: BaseException) -> list[BaseException]:
    links: list[BaseException] = []
    pending: list[BaseException] = [exc]
    while pending:
        link = pending.pop()
        if any(link is seen for seen in links):
            continue
        links.append(link)
        pending.extend(nxt for nxt in (link.__cause__, link.__context__) if nxt is not None)
    return links


def test_a_new_mcp_entry_is_refused_on_a_base_not_bound(live_store: LiveStore) -> None:
    async def body() -> None:
        with pytest.raises(BadRequestError) as caught:
            await section_ops.add_mcp_entries([_late({"fetch": [[{"name": "labelled", "config": {}}]]})])
        assert str(caught.value) == (
            f"invalid mcp config: mcp[late].extensions[fetch][0]: extension 'labelled' on tool 'fetch' cannot bind: "
            f"{_REQUIRED_LABEL}"
        )
        assert live_store.manifest == CHECK_DOCUMENT
        assert live_store.manifest_writes == 0

    _booted(body)


def test_a_new_tools_entry_is_refused_on_a_base_not_bound(live_store: LiveStore) -> None:
    entry = {
        "title": "b",
        "module": "tests.app._fixtures.extended_tools",
        "include": ["echo"],
        "extensions": {"fetch": [[{"name": "labelled", "config": {}}]]},
    }

    async def body() -> None:
        with pytest.raises(BadRequestError) as caught:
            await section_ops.add_tools_entries([entry])
        assert str(caught.value) == (
            f"invalid tools config: tools[b].extensions[fetch][0]: extension 'labelled' on tool 'fetch' cannot bind: "
            f"{_REQUIRED_LABEL}"
        )
        assert live_store.manifest == CHECK_DOCUMENT
        assert live_store.manifest_writes == 0

    _booted(body)


def test_settings_beside_an_unregistered_extension_are_still_checked(live_store: LiveStore) -> None:
    def rewritten(element: dict[str, Any]) -> dict[str, Any]:
        entry = copy.deepcopy(CHECK_DOCUMENT["tools"][0])
        entry["extensions"] = {"echo": [[element, "not_loaded"]]}
        return entry

    async def body() -> None:
        with pytest.raises(BadRequestError) as caught:
            await section_ops.add_tools_entries([rewritten({"name": "labelled", "config": {}})], replace=True)
        assert str(caught.value) == (
            f"invalid tools config: tools[a].extensions[echo][0]: extension 'labelled' on tool 'echo' cannot bind: "
            f"{_REQUIRED_LABEL}"
        )
        assert live_store.manifest == CHECK_DOCUMENT
        assert live_store.manifest_writes == 0

        passing = rewritten({"name": "labelled", "config": {"label": "x"}})
        # The settings pass the gate and the write is stored; the reload then refuses the
        # extension that is not loaded, as it refuses any name it cannot find.
        with pytest.raises(FleetBroadcastError, match="'not_loaded' not found"):
            await section_ops.add_tools_entries([passing], replace=True)
        assert live_store.manifest["tools"][0] == passing

    _booted(body)


def test_a_marker_resolving_to_a_wrong_type_is_refused_without_its_value(
    live_store: LiveStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "s3cr3t-env-value"
    monkeypatch.setenv("EXT_SECRET", secret)

    async def body() -> None:
        with pytest.raises(BadRequestError) as caught:
            await section_ops.add_mcp_entries(
                [_late({"fetch": [[{"name": "labelled_n", "config": {"n": "!ENV ${EXT_SECRET}"}}]]})]
            )
        assert str(caught.value) == (
            "invalid mcp config: mcp[late].extensions[fetch][0]: extension 'labelled_n' on tool 'fetch' cannot bind: "
            "extension 'labelled_n' config: value does not match schema at $.n: breaks the schema's 'type' rule: "
            "'integer' (the value is not shown)"
        )
        for link in _chain(caught.value):
            assert secret not in str(link)
            assert secret not in repr(link.args)
            assert not isinstance(link, (JsonSchemaValidationError, jsonschema.ValidationError))
        assert live_store.manifest == CHECK_DOCUMENT
        assert live_store.manifest_writes == 0

    _booted(body)
