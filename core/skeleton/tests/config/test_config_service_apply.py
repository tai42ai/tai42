"""Unit oracles for :class:`~tai42_skeleton.config.service.ConfigService` — the manifest
document doors: ``apply_change`` (read-modify-write) and ``apply_replace`` (whole-document),
and the secret seal that keeps a resolved ``!ENV`` secret from ever baking to disk.

Each test drives the service against a fake config store (the transactional seams), a fake
reload admin, and a fake worker bus, asserting the pipeline validates on the RESOLVED
projection and rejects before any persist, that the mutator is pure / re-runnable, that
``apply_replace`` validates BEFORE it persists, and that a resolved round-trip restores the
operator's marker (or is refused when the secret is stranded).
"""

from __future__ import annotations

import copy
from typing import Any, cast

import jsonschema
import pytest
from pyaml_env import parse_config
from pydantic import ValidationError
from tai42_kit.utils.data import dump_manifest
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError

from tai42_skeleton.app import epoch as epoch_module
from tai42_skeleton.app import instance
from tai42_skeleton.app.boot_rules import BackendNeedsBusError
from tai42_skeleton.app.bus import OpOutcome
from tai42_skeleton.config.secret_seal import ResolvedSecretError
from tai42_skeleton.operations._broadcast import apply_response
from tai42_skeleton.tools.binding.errors import ExtensionComboRefusedError

from .._fakes.bus import FakeBus
from .._fakes.combo_check_app import booted_check_app, clean_server, factory_calls
from .fake_pipeline import (
    ComboChecks,
    FakeConfigStore,
    FakeReloadAdmin,
    RetryingConfigStore,
    _no_bus,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
    _with_bus,
)

# ---------------------------------------------------------------------------
# apply_change
# ---------------------------------------------------------------------------


async def test_apply_change_mutates_validates_persists_reloads_broadcasts(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [*document.get("mcp", []), {"title": "srv", "config": {"url": "http://x"}}]

    result = await service.apply_change(add_server)

    # Persisted the mutated document exactly once, then reloaded locally, then
    # broadcast the reload to the WHOLE fleet with the local result as `local`.
    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert admin.calls == 1
    assert len(bus.publish_calls) == 1
    op, targets, local = bus.publish_calls[0]
    assert op == {"op": "reload_config"}
    assert targets is None
    assert local is not None
    assert local.outcome == OpOutcome.applied
    # ApplyResult carries the persisted document, the local reload result, and the report.
    assert result.document == {"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}
    assert result.local == {"status": "ok", "env_keys": 0}
    assert result.fleet.op == "reload_config"
    assert result.fleet.ok is True


async def test_apply_result_fanout_is_the_apply_response_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    # fleet_fanout reads the process bus origin to decide local-only vs fleet, so drive
    # the pipeline through that same bus (installed as instance.app.bus) — the value the
    # connector writers thread must equal exactly what apply_response embeds.
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = FakeBus(origin="serve-test", remotes=["serve-w1"])
    monkeypatch.setattr(instance.app, "_bus", bus)
    service, _admin, _bus = _service(store, bus=cast("Any", bus))

    result = await service.apply_replace({"mcp": []})

    assert result.fanout == apply_response(result)["fanout"]
    assert result.fanout["mode"] == "fleet"
    assert {r["name"] for r in result.fanout["results"]} == {"serve-test", "serve-w1"}


async def test_apply_change_invalid_manifest_raises_with_nothing_persisted(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    def break_manifest(document: dict[str, Any]) -> None:
        document["tools"] = "not-a-list"  # fails Manifest schema validation

    with pytest.raises(ValidationError):
        await service.apply_change(break_manifest)

    # Validation rejected the mutated document before the transaction ran: nothing persisted, no reload,
    # no broadcast.
    assert store.persisted == []
    assert store.manifest == {"mcp": []}
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_apply_change_refuses_an_unknown_nested_key_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    def misspell(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x", "header": {"X-Key": "v"}}}]

    with pytest.raises(ValidationError, match=r"mcp\.0\.config\.header\n  Extra inputs are not permitted"):
        await service.apply_change(misspell)

    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_apply_replace_refuses_an_unknown_nested_key_naming_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    with pytest.raises(ValidationError, match=r"api_tools\.expose_destrutive\n  Extra inputs are not permitted"):
        await service.apply_replace({"api_tools": {"expose_destrutive": False}})

    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_apply_change_mutator_rerun_is_tolerated(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    # The store re-runs the mutator (a simulated concurrency conflict) before it
    # persists; a pure mutator yields the same document and persists once.
    store = RetryingConfigStore(manifest={"mcp": []})
    service, admin, _bus = _service(store)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [*document.get("mcp", []), {"title": "srv", "config": {"url": "http://x"}}]

    result = await service.apply_change(add_server)

    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert result.document == {"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}
    assert admin.calls == 1


# ---------------------------------------------------------------------------
# apply_replace
# ---------------------------------------------------------------------------


async def test_apply_replace_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": [{"title": "old", "config": {"url": "http://old"}}]})
    service, admin, bus = _service(store)

    document = {"mcp": [{"title": "new", "config": {"url": "http://new"}}]}
    result = await service.apply_replace(document)

    assert store.manifest == document
    assert admin.calls == 1
    assert bus.publish_calls[0][0] == {"op": "reload_config"}
    assert bus.publish_calls[0][1] is None
    assert result.document == document


async def test_apply_replace_validates_before_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    with pytest.raises(ValidationError):
        await service.apply_replace({"tools": "not-a-list"})

    # A replace has no mutator to abort, so validation must precede the persist.
    assert store.persisted == []
    assert store.manifest == {"mcp": []}
    assert admin.calls == 0
    assert bus.publish_calls == []


# ---------------------------------------------------------------------------
# Secret seal — a resolved !ENV secret never bakes to disk
# ---------------------------------------------------------------------------

# A manifest's mcp section is only ever read through the RESOLVED view, so the natural
# round-trip (read the resolved view → edit → post it back) hands the pipeline resolved
# secret values. The seal retags them back to the operator's !ENV marker before persist,
# and refuses a stranded resolved secret with no marker origin.
_TOKEN = "super-secret-token-value"


def _resolved(document: dict[str, Any]) -> dict[str, Any]:
    """The RESOLVED view a client reads through ``GET /api/manifest`` — ``!ENV``
    markers materialized against the current env, exactly as the live manifest
    exposes them."""
    return cast("dict[str, Any]", parse_config(data=dump_manifest(cast("Any", document))) or {})


def _marker_manifest() -> dict[str, Any]:
    """A manifest whose one mcp server carries an ``!ENV`` auth header marker."""
    return {
        "mcp": [
            {"title": "srv", "config": {"url": "http://x", "headers": {"Authorization": "!ENV ${TOKEN}"}}},
        ]
    }


async def test_apply_change_resolved_round_trip_restores_env_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    # The exact set_mcp_config round-trip: the client reads the RESOLVED mcp section,
    # edits it, and posts it back as document["mcp"] = <resolved list>. The seal must
    # restore the operator's !ENV marker so no resolved token bakes to disk.
    _with_bus(monkeypatch)
    monkeypatch.setenv("TOKEN", _TOKEN)
    store = FakeConfigStore(manifest=_marker_manifest())
    service, _admin, _bus = _service(store)
    resolved = _resolved(store.manifest)
    # The client read the resolved view: its Authorization is the plaintext token.
    assert resolved["mcp"][0]["config"]["headers"]["Authorization"] == _TOKEN

    def post_resolved(document: dict[str, Any]) -> None:
        # set_mcp_config's mutator: wholesale-replace mcp with the client-supplied
        # (resolved) list.
        document["mcp"] = copy.deepcopy(resolved["mcp"])

    result = await service.apply_change(post_resolved)

    # The persisted document restored the !ENV marker; the resolved token never landed.
    assert result.document is not None
    assert result.document["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${TOKEN}"
    assert _TOKEN not in str(store.persisted)


async def test_apply_replace_resolved_round_trip_restores_env_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    # A whole-document replace carrying a resolved secret where the current doc has a
    # marker is retagged, so the marker persists.
    _with_bus(monkeypatch)
    monkeypatch.setenv("TOKEN", _TOKEN)
    store = FakeConfigStore(manifest=_marker_manifest())
    service, _admin, _bus = _service(store)

    replacement = copy.deepcopy(_resolved(store.manifest))
    result = await service.apply_replace(replacement)

    assert result.document is not None
    assert result.document["mcp"][0]["config"]["headers"]["Authorization"] == "!ENV ${TOKEN}"
    assert _TOKEN not in str(store.persisted)


async def test_apply_replace_stranded_resolved_secret_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # A replacement carrying a resolved secret whose entry has NO marker origin (its
    # identity was renamed away) is a stranded plaintext secret — refused loudly.
    _with_bus(monkeypatch)
    monkeypatch.setenv("TOKEN", _TOKEN)
    store = FakeConfigStore(manifest=_marker_manifest())
    service, admin, bus = _service(store)

    stranded = copy.deepcopy(_resolved(store.manifest))
    stranded["mcp"][0]["title"] = "renamed"  # no marker origin now; the token is plaintext

    with pytest.raises(ResolvedSecretError) as exc:
        await service.apply_replace(stranded)

    # ValueError-mappable to a 400 by the operations layer, and it names the offending path.
    assert isinstance(exc.value, ValueError)
    assert "mcp[0]" in str(exc.value)
    # Refused before any persist.
    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_apply_change_pure_marker_mutator_persists_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pure in-place mutator whose leaves already carry !ENV markers (a connector-style
    # append of a preserved-marker entry) is a retag no-op and passes the leak net with
    # no false rejection — the markers persist verbatim.
    _with_bus(monkeypatch)
    monkeypatch.setenv("TOKEN", _TOKEN)
    monkeypatch.setenv("OTHER", "other-secret-value")
    store = FakeConfigStore(manifest={"mcp": [{"title": "srv", "config": {"env": {"KEY": "!ENV ${TOKEN}"}}}]})
    service, _admin, _bus = _service(store)

    def append_marker_entry(document: dict[str, Any]) -> None:
        document["mcp"].append({"title": "srv2", "config": {"env": {"KEY2": "!ENV ${OTHER}"}}})

    result = await service.apply_change(append_marker_entry)

    assert result.document is not None
    # Both the untouched and the appended entries keep their !ENV markers verbatim.
    assert result.document["mcp"][0]["config"]["env"]["KEY"] == "!ENV ${TOKEN}"
    assert result.document["mcp"][1]["config"]["env"]["KEY2"] == "!ENV ${OTHER}"
    assert _TOKEN not in str(store.persisted)


# ---------------------------------------------------------------------------
# Extension combos — every combo of the candidate is dry-run bound before the write
# ---------------------------------------------------------------------------

_CHECK_DOCUMENT: dict[str, Any] = {
    "tools": [{"title": "a", "module": "tests.app._fixtures.tools_check", "include": ["echo"]}],
    "mcp": [{"title": "m", "config": {"url": "http://x"}}],
}


def _attach(section: str, entry: int | None, tool: str, combos: list[list[Any]]) -> Any:
    """A mutator attaching ``combos`` to ``tool`` on ``section`` (entry ``entry``, or the ``api_tools`` block)."""

    def mutator(document: dict[str, Any]) -> None:
        if entry is None:
            document.setdefault(section, {})["extensions"] = {tool: combos}
        else:
            document[section][entry]["extensions"] = {tool: combos}

    return mutator


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and every link reached through ``__cause__`` / ``__context__``."""
    links: list[BaseException] = []
    pending: list[BaseException] = [exc]
    while pending:
        link = pending.pop()
        if any(link is seen for seen in links):
            continue
        links.append(link)
        pending.extend(nxt for nxt in (link.__cause__, link.__context__) if nxt is not None)
    return links


@pytest.mark.parametrize(
    ("section", "entry", "tool", "combos", "path"),
    [
        ("tools", 0, "echo", [[{"name": "labelled", "config": {"label": "x"}}]], "tools[a].extensions[echo][0]"),
        ("mcp", 0, "fetch", [[{"name": "labelled", "config": {"label": "x"}}]], "mcp[m].extensions[fetch][0]"),
        (
            "api_tools",
            None,
            "op",
            [["cache"], [{"name": "labelled", "config": {"label": "x"}}]],
            "api_tools.extensions[op][1]",
        ),
    ],
)
async def test_apply_change_refuses_a_combo_the_check_refuses(
    monkeypatch: pytest.MonkeyPatch, section: str, entry: int | None, tool: str, combos: list[list[Any]], path: str
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest=copy.deepcopy(_CHECK_DOCUMENT))
    service, admin, bus = _service(store, combo_check=ComboChecks(refuse="x"))

    with pytest.raises(ValueError, match="cannot bind: refused") as caught:
        await service.apply_change(_attach(section, entry, tool, combos))

    assert str(caught.value) == f"{path}: extension 'labelled' on tool '{tool}' cannot bind: refused"
    assert isinstance(caught.value.__cause__, ExtensionComboRefusedError)
    assert store.manifest == _CHECK_DOCUMENT
    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_apply_replace_refuses_a_combo_the_check_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest=copy.deepcopy(_CHECK_DOCUMENT))
    service, admin, bus = _service(store, combo_check=ComboChecks(refuse="x"))
    document = copy.deepcopy(_CHECK_DOCUMENT)
    _attach("tools", 0, "echo", [[{"name": "labelled", "config": {"label": "x"}}]])(document)

    with pytest.raises(ValueError, match=r"^tools\[a\]\.extensions\[echo\]\[0\]: extension 'labelled' on tool"):
        await service.apply_replace(document)

    assert store.manifest == _CHECK_DOCUMENT
    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


class _ConcurrentWriteStore(FakeConfigStore):
    """A store another writer changes between the service's read and its transaction."""

    def __init__(self, *, manifest: dict[str, Any], concurrent: Any) -> None:
        super().__init__(manifest=manifest)
        self._concurrent = concurrent

    def mutate_manifest(self, mutator: Any) -> dict[str, Any]:
        self._concurrent(self.manifest)
        return super().mutate_manifest(mutator)


async def test_apply_change_validates_the_document_a_concurrent_write_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The change is valid against the manifest it was built on; the transaction runs it on a
    # manifest another writer changed meanwhile, and that result is validated and refused.
    _with_bus(monkeypatch)
    refused = _attach("mcp", 0, "fetch", [[{"name": "labelled", "config": {"label": "x"}}]])
    store = _ConcurrentWriteStore(manifest=copy.deepcopy(_CHECK_DOCUMENT), concurrent=refused)
    service, admin, bus = _service(store, combo_check=ComboChecks(refuse="x"))

    with pytest.raises(ValueError, match=r"^mcp\[m\]\.extensions\[fetch\]\[0\]: extension 'labelled' on tool"):
        await service.apply_change(_attach("tools", 0, "echo", [["cache"]]))

    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


class _ListingAdmin(FakeReloadAdmin):
    """A reload admin whose server listing first runs ``during``, as a change landing mid-listing does."""

    def __init__(self, during: Any) -> None:
        super().__init__(served={"m": ["m_a", "m_b"]})
        self._during = during

    async def served_mcp_names(self, config: Any) -> list[str]:
        self._during()
        return await super().served_mcp_names(config)


def _pick(include: list[str], then: Any = None) -> Any:
    """A mutator that narrows the ``m`` server to ``include`` (so its server is listed), then runs ``then``."""

    def mutator(document: dict[str, Any]) -> None:
        document["mcp"][0]["include"] = include
        if then is not None:
            then(document)

    return mutator


async def _write(service: Any, store: FakeConfigStore, door: str, mutator: Any) -> None:
    """Drive ``mutator`` through ``apply_change``, or as the whole document through ``apply_replace``."""
    if door == "change":
        await service.apply_change(mutator)
        return
    document = copy.deepcopy(store.manifest)
    mutator(document)
    await service.apply_replace(document)


_DOORS = ["change", "replace"]


@pytest.mark.parametrize("door", _DOORS)
async def test_an_env_change_during_the_listing_refuses_the_marker_it_leaves_dangling(
    monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    _with_bus(monkeypatch)
    monkeypatch.setenv("PICK_RACE", "m_b")
    admin = _ListingAdmin(lambda: monkeypatch.delenv("PICK_RACE"))
    store = FakeConfigStore(manifest=copy.deepcopy(_CHECK_DOCUMENT))
    service, _admin, bus = _service(store, admin=admin)

    with pytest.raises(ValueError, match=r"markers dangling .*PICK_RACE"):
        await _write(service, store, door, _pick(["m_a", "!ENV ${PICK_RACE}"]))

    assert admin.listed == ["m"]
    assert store.persisted == []
    assert store.manifest == _CHECK_DOCUMENT
    assert bus.publish_calls == []


@pytest.mark.parametrize("door", _DOORS)
async def test_a_bus_removed_during_the_listing_refuses_the_backend_it_leaves(
    monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    # The hooks store is shared, so the bus removed mid-listing is the only invariant the backend breaks.
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/0")
    # No referee holds the unpicked names, so the held-unpick check reads no live hooks store.
    monkeypatch.setattr(type(instance.app.tools), "rename_referees", lambda _self: [])
    _with_bus(monkeypatch)
    admin = _ListingAdmin(lambda: _no_bus(monkeypatch))
    store = FakeConfigStore(manifest=copy.deepcopy(_CHECK_DOCUMENT))
    service, _admin, bus = _service(store, admin=admin)

    def add_backend(document: dict[str, Any]) -> None:
        document["backend_module"] = "myapp.backend"

    with pytest.raises(BackendNeedsBusError):
        await _write(service, store, door, _pick(["m_a"], then=add_backend))

    assert admin.listed == ["m"]
    assert store.persisted == []
    assert bus.publish_calls == []


@pytest.mark.parametrize("door", _DOORS)
async def test_a_serving_generation_swapped_during_the_listing_rechecks_the_combos(
    monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    # The combo check dry-runs against the live generation's registry; one installed during
    # the listing refuses the combo the outgoing one accepted.
    _with_bus(monkeypatch)
    swapped = object()
    admin = _ListingAdmin(lambda: monkeypatch.setattr(epoch_module, "_current", swapped))
    store = FakeConfigStore(manifest=copy.deepcopy(_CHECK_DOCUMENT))
    checks: list[bool] = []

    def combo_check(tool: str, combo: Any) -> None:
        checks.append(epoch_module.current_epoch_or_none() is swapped)
        if checks[-1]:
            raise ExtensionComboRefusedError(f"extension 'labelled' on tool '{tool}' cannot bind: refused")

    service, _admin, bus = _service(store, admin=admin, combo_check=combo_check)
    attach = _attach("tools", 0, "echo", [[{"name": "labelled", "config": {"label": "y"}}]])

    with pytest.raises(ValueError, match=r"^tools\[a\]\.extensions\[echo\]\[0\]: .* cannot bind: refused$"):
        await _write(service, store, door, _pick(["m_a"], then=attach))

    assert checks == [False, True]
    assert store.persisted == []
    assert bus.publish_calls == []


async def test_the_check_receives_every_combo_once_with_settings_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    monkeypatch.setenv("EXT_LABEL", "resolved-label")
    store = FakeConfigStore(manifest=copy.deepcopy(_CHECK_DOCUMENT))
    checks = ComboChecks()
    service, _admin, _bus = _service(store, combo_check=checks)
    marked = {"name": "labelled", "config": {"label": "!ENV ${EXT_LABEL}"}}

    def attach_everywhere(document: dict[str, Any]) -> None:
        document["tools"][0]["extensions"] = {"echo": [[marked], ["cache"]]}
        document["mcp"][0]["extensions"] = {"fetch": [["cache", marked]]}
        document["api_tools"] = {"extensions": {"op": [[marked]]}}

    await service.apply_change(attach_everywhere)

    resolved = {"name": "labelled", "config": {"label": "resolved-label"}}
    assert checks.calls == [
        ("echo", [resolved]),
        ("echo", ["cache"]),
        ("fetch", ["cache", resolved]),
        ("op", [resolved]),
    ]
    assert store.manifest["tools"][0]["extensions"]["echo"][0][0]["config"]["label"] == "!ENV ${EXT_LABEL}"


async def test_a_dangling_marker_in_a_setting_is_refused_before_the_check_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    monkeypatch.delenv("EXT_UNSET", raising=False)
    store = FakeConfigStore(manifest=copy.deepcopy(_CHECK_DOCUMENT))
    checks = ComboChecks()
    service, _admin, _bus = _service(store, combo_check=checks)

    with pytest.raises(ValueError, match="leaves manifest !ENV markers dangling") as caught:
        await service.apply_change(
            _attach("tools", 0, "echo", [[{"name": "labelled_n", "config": {"n": "!ENV ${EXT_UNSET}"}}]])
        )

    assert str(caught.value) == (
        "Refusing a change that leaves manifest !ENV markers dangling — these references resolve to no env var "
        'and would silently run on "N/A": EXT_UNSET (at /tools/0/extensions/echo/0/0/config/n).'
    )
    assert checks.calls == []
    assert store.manifest == _CHECK_DOCUMENT


_LABELLED_N_TYPE = (
    "tools[a].extensions[echo][0]: extension 'labelled_n' on tool 'echo' cannot bind: extension 'labelled_n' "
    "config: value does not match schema at $.n: breaks the schema's 'type' rule: 'integer' (the value is not shown)"
)


@pytest.mark.usefixtures(clean_server.__name__)
async def test_the_live_check_refuses_settings_breaking_a_declared_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    secret = "s3cr3t-env-value"
    monkeypatch.setenv("EXT_SECRET", secret)
    monkeypatch.setenv("EXT_LABEL", "hello")
    async with booted_check_app():
        store = FakeConfigStore(manifest=copy.deepcopy(_CHECK_DOCUMENT))
        service, admin, _bus = _service(store, combo_check=instance.app.extensions.check_combo)
        factory_calls().clear()

        with pytest.raises(ValueError, match="'required' rule") as missing:
            await service.apply_change(_attach("tools", 0, "echo", [[{"name": "labelled", "config": {}}]]))
        assert str(missing.value) == (
            "tools[a].extensions[echo][0]: extension 'labelled' on tool 'echo' cannot bind: extension 'labelled' "
            "config: value does not match schema at $: breaks the schema's 'required' rule: 'label' "
            "(the value is not shown)"
        )

        with pytest.raises(ValueError, match="'type' rule") as typed:
            await service.apply_change(
                _attach("tools", 0, "echo", [[{"name": "labelled_n", "config": {"n": "!ENV ${EXT_SECRET}"}}]])
            )
        assert str(typed.value) == _LABELLED_N_TYPE
        links = _chain(typed.value)
        for link in links:
            assert secret not in str(link)
            assert secret not in repr(link.args)
            assert not isinstance(link, (JsonSchemaValidationError, jsonschema.ValidationError))
        refused = [link for link in links if isinstance(link, ExtensionComboRefusedError)]
        assert len(refused) == 1
        assert refused[0].__cause__ is None
        assert refused[0].__context__ is None
        assert store.manifest == _CHECK_DOCUMENT
        assert admin.calls == 0

        await service.apply_change(
            _attach("tools", 0, "echo", [[{"name": "labelled", "config": {"label": "!ENV ${EXT_LABEL}"}}]])
        )
        assert store.manifest["tools"][0]["extensions"] == {
            "echo": [[{"name": "labelled", "config": {"label": "!ENV ${EXT_LABEL}"}}]]
        }
        assert factory_calls() == ["labelled"]

        factory_calls().clear()
        await service.apply_change(_attach("tools", 0, "echo", [[{"name": "not_loaded", "config": {"anything": 1}}]]))
        assert store.manifest["tools"][0]["extensions"] == {
            "echo": [[{"name": "not_loaded", "config": {"anything": 1}}]]
        }
        assert factory_calls() == []
