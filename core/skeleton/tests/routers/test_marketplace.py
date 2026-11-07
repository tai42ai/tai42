"""The marketplace routes as thin adapters over their operations.

Each handler is called directly with a built request; the operation-layer
constructions (``RegistryClient``/``Installer``/``MarketplaceInstallStore``/the
advisory module) are monkeypatched in the ``operations.marketplace`` namespace,
which is the per-request construction seam. The tests pin the envelope, the
search param forwarding (repeated ``tags`` via ``getlist``), the detail compose,
the installed-inventory per-row ``missing_upstream`` handling, and the error→
status mapping (502/404/409/500 and the retriable 503s).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from starlette.requests import Request
from tai42_contract.plugins.spec import PluginSpec

import tai42_skeleton.operations.marketplace as mkt_ops
import tai42_skeleton.routers.marketplace as router
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.app.writer_exit import WRITER_EXIT_REASON
from tai42_skeleton.marketplace.advisories import AdvisoryState
from tai42_skeleton.marketplace.compat import declared_compat
from tai42_skeleton.marketplace.errors import (
    ArtifactIntegrityError,
    ContractIncompatibleError,
    InstallStateError,
    InstallUnwindError,
    ListingNotFoundError,
    LocalStateError,
    MalformedRefError,
    ManifestCollisionError,
    ManifestComposeError,
    OperationInProgressError,
    PipFailedError,
    PipUnavailableError,
    PluginDependencyConflictError,
    PluginMigrationError,
    PluginsDirError,
    PublicRoutesNotAcceptedError,
    RegistryUnreachableError,
    ReservedRoutePrefixError,
    RouteCollisionError,
    RouteMountError,
    ServerCommandCollisionError,
    UndeclaredServerCommandError,
    VersionRefusedError,
)
from tai42_skeleton.marketplace.store import InstallRecord
from tai42_skeleton.operations import NotSupportedError

from ..marketplace._specs import make_spec, mcp_server_item, tool_item

# These tests call the route handlers below the HTTP edge a request runs under.
pytestmark = pytest.mark.usefixtures("http_edge")


@pytest.fixture(autouse=True)
def _marketplace_store_configured(monkeypatch):
    # the marketplace surface answers OFF with no store configured. These tests
    # exercise the ON feature (a fake DB stands in), so satisfy the presence gate.
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "x")


def _get(path: str = "/", query: bytes = b"", **path_params: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": query,
        "headers": [],
        "path_params": path_params,
    }
    return Request(scope)


def _post(body: Any) -> Request:
    async def _json() -> Any:
        return body

    return cast(Request, SimpleNamespace(json=_json, path_params={}, query_params={}, headers={}))


def _data(resp) -> Any:
    return json.loads(bytes(resp.body))


def _mcp_item(name: str) -> dict[str, Any]:
    return {"kind": "mcp-server", "name": name, "description": f"The {name} server", "mcp": {"command": "run"}}


def _descriptor_spec(ref: str, *items: dict[str, Any]) -> dict[str, Any]:
    """A valid stored descriptor-only spec (no package) for ``ref``, providing ``items`` (default: one mcp-server)."""
    namespace, name = ref.split("/")
    document = {
        "spec_version": 1,
        "namespace": namespace,
        "name": name,
        "version": "1.0.0",
        "description": "A test plugin",
        "license": "Apache-2.0",
        "categories": ["dev"],
        "provides": list(items) or [_mcp_item(name)],
    }
    return PluginSpec.model_validate(document).model_dump(mode="json")


def _record(ref: str, version: str = "1.0.0") -> InstallRecord:
    return InstallRecord(
        ref=ref,
        version=version,
        source="pypi",
        repository_url=None,
        tag=None,
        spec=_descriptor_spec(ref),
        installed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


class _FakeRegistry:
    def __init__(self, **behaviour: Any) -> None:
        self._b = behaviour
        self.search_params: Any = None

    async def search(self, params):
        self.search_params = params
        return self._b.get("search", {"items": []})

    async def plugin(self, ns, name):
        result = self._b["plugin"](ns, name) if callable(self._b.get("plugin")) else self._b.get("plugin", {})
        if isinstance(result, Exception):
            raise result
        return result

    async def versions(self, ns, name):
        result = self._b["versions"](ns, name) if callable(self._b.get("versions")) else self._b.get("versions", [])
        if isinstance(result, Exception):
            raise result
        return result

    async def categories(self):
        return self._b.get("categories", [])

    async def kinds(self):
        return self._b.get("kinds", [])


def _use_registry(monkeypatch: pytest.MonkeyPatch, registry: _FakeRegistry) -> None:
    monkeypatch.setattr(mkt_ops, "RegistryClient", lambda *a, **k: registry)


def _use_store(monkeypatch: pytest.MonkeyPatch, rows: list[InstallRecord]) -> None:
    class _Store:
        async def list_installed(self):
            return rows

    monkeypatch.setattr(mkt_ops, "MarketplaceInstallStore", lambda: _Store())


def _use_installer(monkeypatch: pytest.MonkeyPatch, *, install: Callable[[str, str | None], Awaitable[Any]]) -> None:
    class _Installer:
        async def install(
            self, ref, version=None, *, env=None, secret_keys=None, route_mounts=None, accept_public_routes=False
        ):
            return await install(ref, version)

        async def uninstall(self, ref):
            return await install(ref, None)

        async def update(
            self, ref, version=None, *, env=None, secret_keys=None, route_mounts=None, accept_public_routes=False
        ):
            return await install(ref, version)

        async def preview(self, ref, version=None, *, route_mounts=None):
            return await install(ref, version)

        async def upgrade_all(self):
            return await install("--all--", None)

    monkeypatch.setattr(mkt_ops, "Installer", lambda *a, **k: _Installer())


# -- search ------------------------------------------------------------------


async def test_search_forwards_params_and_wraps_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _FakeRegistry(search={"items": [{"ref": "tai42/toolbox"}]})
    _use_registry(monkeypatch, registry)
    resp = await router.marketplace_search(_get(query=b"q=uuid&tags=a&tags=b&tier=official&page_size=5"))
    assert resp.status_code == 200
    assert _data(resp) == {"data": {"items": [{"ref": "tai42/toolbox"}]}}
    # Repeated tags survive multi-value; single facets forwarded; unknown dropped.
    assert registry.search_params["tags"] == ["a", "b"]
    assert registry.search_params["q"] == "uuid"
    assert registry.search_params["tier"] == "official"
    assert registry.search_params["page_size"] == "5"


async def test_search_maps_upstream_failure_to_502(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom(_FakeRegistry):
        async def search(self, params):
            raise RegistryUnreachableError("registry down at https://reg")

    _use_registry(monkeypatch, _Boom())
    resp = await router.marketplace_search(_get())
    assert resp.status_code == 502
    assert "registry down" in _data(resp)["error"]


# -- detail ------------------------------------------------------------------


async def test_detail_composes_versions_into_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _FakeRegistry(plugin={"ref": "tai42/toolbox", "display_name": "T"}, versions=[{"version": "1.0.0"}])
    _use_registry(monkeypatch, registry)
    resp = await router.marketplace_plugin_detail(_get(ns="tai42", name="toolbox"))
    body = _data(resp)["data"]
    assert body["display_name"] == "T"
    assert body["versions"] == [{"version": "1.0.0"}]


async def test_detail_unknown_listing_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _FakeRegistry(plugin=ListingNotFoundError("marketplace listing not found: tai42/nope"))
    _use_registry(monkeypatch, registry)
    resp = await router.marketplace_plugin_detail(_get(ns="tai42", name="nope"))
    assert resp.status_code == 404


# -- categories --------------------------------------------------------------


async def test_categories_proxies_the_vocabulary(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_registry(monkeypatch, _FakeRegistry(categories=["dev", "data"]))
    resp = await router.marketplace_categories(_get())
    assert _data(resp) == {"data": ["dev", "data"]}


# -- kinds -------------------------------------------------------------------


async def test_kinds_proxies_the_vocabulary(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_registry(monkeypatch, _FakeRegistry(kinds=["tool", "agent"]))
    resp = await router.marketplace_kinds(_get())
    assert _data(resp) == {"data": ["tool", "agent"]}


# -- installed ---------------------------------------------------------------


def _published(version: str, contract_range: str | None = ">=0.3,<0.4") -> dict[str, Any]:
    return {"version": version, "status": "published", "contract_range": contract_range}


@pytest.fixture(autouse=True)
def _pin_contract(monkeypatch: pytest.MonkeyPatch):
    # The installed listing computes against the RUNNING contract; pin it so the
    # tests are independent of the environment's tai42-contract version.
    monkeypatch.setattr(mkt_ops, "running_contract_version", lambda: "0.3.0")


async def test_installed_happy_computes_update_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_store(monkeypatch, [_record("tai42/toolbox", "1.0.0")])
    _use_registry(monkeypatch, _FakeRegistry(versions=[_published("2.0.0"), _published("1.0.0")]))
    resp = await router.marketplace_installed(_get())
    body = _data(resp)["data"]
    row = body["installed"][0]
    assert row["latest"] == "2.0.0"
    assert row["update_available"] is True
    assert row["incompatible_newer"] is None
    assert row["missing_upstream"] is False
    # A row whose stored spec names no package cannot be verdicted — surfaced
    # as unknown, never a silent "compatible".
    assert row["compat"]["status"] == "unknown"
    assert row["delivery"] == "descriptor"


async def test_installed_exposes_provided_item_names(monkeypatch: pytest.MonkeyPatch) -> None:
    # Studio joins these names against the manifest's mcp-entry titles; the row
    # carries {kind, name} from LOCAL spec truth, in spec order.
    record = InstallRecord(
        ref="tai42/postgres-mcp",
        version="1.0.0",
        source="pypi",
        repository_url=None,
        tag=None,
        spec=_descriptor_spec("tai42/postgres-mcp", _mcp_item("postgres"), _mcp_item("replica")),
        installed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _use_store(monkeypatch, [record, _record("tai42/toolbox", "1.0.0")])
    _use_registry(monkeypatch, _FakeRegistry(versions=[_published("1.0.0")]))
    resp = await router.marketplace_installed(_get())
    rows = {r["ref"]: r for r in _data(resp)["data"]["installed"]}
    assert rows["tai42/postgres-mcp"]["items"] == [
        {"kind": "mcp-server", "name": "postgres"},
        {"kind": "mcp-server", "name": "replica"},
    ]
    assert rows["tai42/toolbox"]["items"] == [{"kind": "mcp-server", "name": "toolbox"}]


async def test_installed_corrupt_stored_spec_is_500_naming_the_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    # A stored spec that no longer validates is corrupt local state: the listing fails
    # loudly naming the row, never serving a guessed shape.
    corrupt = InstallRecord(
        ref="tai42/toolbox",
        version="1.0.0",
        source="pypi",
        spec={"provides": "not-a-list"},
        installed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _use_store(monkeypatch, [corrupt])
    _use_registry(monkeypatch, _FakeRegistry(versions=[_published("1.0.0")]))
    resp = await router.marketplace_installed(_get())
    assert resp.status_code == 500
    assert "tai42/toolbox" in json.dumps(_data(resp))


async def test_installed_corrupt_stored_ref_is_500_naming_the_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    record = InstallRecord(
        ref="Tai42/Toolbox",
        version="1.0.0",
        source="pypi",
        spec=_descriptor_spec("tai42/toolbox"),
        installed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _use_store(monkeypatch, [record])
    _use_registry(monkeypatch, _FakeRegistry(versions=[_published("1.0.0")]))
    resp = await router.marketplace_installed(_get())
    assert resp.status_code == 500
    assert "the stored ref 'Tai42/Toolbox' is corrupt" in json.dumps(_data(resp))


async def test_installed_exposes_stored_route_mounts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Studio seeds its update flow and renders routes at the ACTUAL mounted base
    # from this per-row {item_name: base}; a row with no remap answers {}.
    remapped = InstallRecord(
        ref="tai42/gateway",
        version="1.0.0",
        source="pypi",
        repository_url=None,
        tag=None,
        spec=_descriptor_spec("tai42/gateway"),
        route_mounts={"gateway": "custom/base"},
        installed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _use_store(monkeypatch, [remapped, _record("tai42/toolbox", "1.0.0")])
    _use_registry(monkeypatch, _FakeRegistry(versions=[_published("1.0.0")]))
    resp = await router.marketplace_installed(_get())
    rows = {r["ref"]: r for r in _data(resp)["data"]["installed"]}
    assert rows["tai42/gateway"]["route_mounts"] == {"gateway": "custom/base"}
    assert rows["tai42/toolbox"]["route_mounts"] == {}


async def test_installed_incompatible_newer_never_advertises_as_update(monkeypatch: pytest.MonkeyPatch) -> None:
    # 2.0.0 needs a different contract: it must NOT drive update_available, but
    # it must be named in incompatible_newer — visible, never silently dropped.
    _use_store(monkeypatch, [_record("tai42/toolbox", "1.0.0")])
    _use_registry(
        monkeypatch,
        _FakeRegistry(versions=[_published("2.0.0", ">=0.4,<0.5"), _published("1.5.0"), _published("1.0.0")]),
    )
    resp = await router.marketplace_installed(_get())
    row = _data(resp)["data"]["installed"][0]
    assert row["latest"] == "2.0.0"
    assert row["update_available"] is True  # 1.5.0 is the compatible target
    assert row["incompatible_newer"] == "2.0.0"


async def test_installed_row_with_vanished_upstream_is_marked(monkeypatch: pytest.MonkeyPatch) -> None:
    def _versions(ns, name):
        if name == "gone":
            return ListingNotFoundError("marketplace listing not found: tai42/gone")
        return [_published("2.0.0")]

    _use_store(monkeypatch, [_record("tai42/gone", "1.0.0"), _record("tai42/live", "1.0.0")])
    _use_registry(monkeypatch, _FakeRegistry(versions=_versions))
    resp = await router.marketplace_installed(_get())
    rows = {r["ref"]: r for r in _data(resp)["data"]["installed"]}
    assert rows["tai42/gone"]["missing_upstream"] is True
    assert rows["tai42/gone"]["latest"] is None
    assert rows["tai42/live"]["latest"] == "2.0.0"  # other rows intact


async def test_installed_zero_published_versions_is_not_a_500(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_store(monkeypatch, [_record("tai42/toolbox", "1.0.0")])
    _use_registry(monkeypatch, _FakeRegistry(versions=[{"version": "2.0.0", "status": "yanked"}]))
    resp = await router.marketplace_installed(_get())
    row = _data(resp)["data"]["installed"][0]
    assert row["latest"] is None
    assert row["update_available"] is False
    assert row["missing_upstream"] is False  # the listing is there, just unpublished


async def test_installed_transport_failure_still_502(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_store(monkeypatch, [_record("tai42/toolbox", "1.0.0")])
    _use_registry(monkeypatch, _FakeRegistry(versions=RegistryUnreachableError("down")))
    resp = await router.marketplace_installed(_get())
    assert resp.status_code == 502


async def test_installed_non_pep440_published_version_is_502(monkeypatch: pytest.MonkeyPatch) -> None:
    # A registry-supplied published version that is not PEP 440 is garbled
    # upstream data — a 502, never a raw this-server 500 from the version compare.
    _use_store(monkeypatch, [_record("tai42/toolbox", "1.0.0")])
    _use_registry(monkeypatch, _FakeRegistry(versions=[_published("not-a-version")]))
    resp = await router.marketplace_installed(_get())
    assert resp.status_code == 502
    assert "error" in _data(resp)


async def test_installed_non_string_published_version_is_502(monkeypatch: pytest.MonkeyPatch) -> None:
    # A ``version`` that is not even a string (an int here) makes ``Version()``
    # raise TypeError — NOT InvalidVersion — so the compare would otherwise
    # escape as a raw this-server 500. Garbled upstream data is a 502.
    _use_store(monkeypatch, [_record("tai42/toolbox", "1.0.0")])
    _use_registry(monkeypatch, _FakeRegistry(versions=[_published(123)]))  # pyright: ignore[reportArgumentType]
    resp = await router.marketplace_installed(_get())
    assert resp.status_code == 502
    assert "error" in _data(resp)


async def test_installed_malformed_contract_range_is_502(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_store(monkeypatch, [_record("tai42/toolbox", "1.0.0")])
    _use_registry(monkeypatch, _FakeRegistry(versions=[_published("2.0.0", "not a specifier !!")]))
    resp = await router.marketplace_installed(_get())
    assert resp.status_code == 502
    assert "error" in _data(resp)


async def test_installed_garbled_local_version_is_500(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unparsable STORED version is corrupt local state — a 500, never blamed
    # on the registry as a 502.
    _use_store(monkeypatch, [_record("tai42/toolbox", "not-a-version")])
    _use_registry(monkeypatch, _FakeRegistry(versions=[_published("2.0.0")]))
    resp = await router.marketplace_installed(_get())
    assert resp.status_code == 500
    assert "error" in _data(resp)


def _installed_record(spec: PluginSpec) -> InstallRecord:
    return InstallRecord(
        ref=spec.ref,
        version=spec.version,
        source="pypi",
        spec=spec.model_dump(mode="json"),
        installed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def test_installed_serves_the_verdict_of_each_row_s_declared_contract_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A range no running tai42-contract is inside, as a core upgrade leaves a store row.
    spec = make_spec(package="acme-toolbox", provides=[tool_item()], contract=">=99,<100")
    _use_store(monkeypatch, [_installed_record(spec)])
    _use_registry(monkeypatch, _FakeRegistry(versions=[_published("1.0.0")]))

    resp = await router.marketplace_installed(_get())

    row = _data(resp)["data"]["installed"][0]
    assert row["compat"] == declared_compat("acme-toolbox", ">=99,<100").as_payload()
    assert row["compat"]["status"] == "incompatible"
    assert row["compat"]["reason"].startswith("acme-toolbox requires tai42-contract <100,>=99, but ")


async def test_installed_serves_unknown_for_a_row_declaring_no_contract_range(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = make_spec(package="acme-srv", provides=[mcp_server_item()], contract=None)
    _use_store(monkeypatch, [_installed_record(spec)])
    _use_registry(monkeypatch, _FakeRegistry(versions=[_published("1.0.0")]))

    resp = await router.marketplace_installed(_get())

    row = _data(resp)["data"]["installed"][0]
    assert row["compat"] == {
        "status": "unknown",
        "reason": "distribution 'acme-srv' declares no tai42-contract requirement; treating it as compatible",
    }


# -- advisories --------------------------------------------------------------


async def test_advisories_serves_the_cache_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched = datetime(2026, 6, 1, tzinfo=UTC)

    class _Adv:
        async def current(self, max_age_s):
            return AdvisoryState(advisories=[{"summary": "x"}], fetched_at=fetched)

    monkeypatch.setattr(mkt_ops, "advisories", _Adv())
    resp = await router.marketplace_advisories(_get())
    body = _data(resp)["data"]
    assert body["advisories"] == [{"summary": "x"}]
    assert body["fetched_at"] == fetched.isoformat()


# -- install: gates, envelope, error mapping ---------------------------------


async def test_install_happy_wraps_data(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(ref, version):
        return {"ref": ref, "version": "1.0.0", "uninstalled": False, "reload": None}

    _use_installer(monkeypatch, install=_ok)
    resp = await router.marketplace_install(_post({"ref": "tai42/toolbox"}))
    assert resp.status_code == 200
    assert _data(resp)["data"]["ref"] == "tai42/toolbox"


def _install_result(*, stopped: bool) -> Any:
    """An install-time env change's structured outcome: a converged roll (the writer armed) or a stopped one."""
    from tai42_contract.app.responses import AwaitingWorker

    from tai42_skeleton.app.bus import FleetResult, WorkerIdentity, WorkerKind
    from tai42_skeleton.app.recycle import RECYCLED, TIMED_OUT, RecycleReport, RecycleRow, RecycleStop
    from tai42_skeleton.config.service import ApplyResult

    if stopped:
        roll = RecycleReport(
            rows=[
                RecycleRow(
                    name="backend-1",
                    kind="backend",
                    generation_before=1,
                    status=TIMED_OUT,
                    detail="slow",
                    keys=["BACKEND_MANIFEST_KEY"],
                )
            ],
            stopped=RecycleStop(kind="backend", name="backend-1", detail="slow"),
        )
        verdict = [
            AwaitingWorker(name="backend-1", keys=["BACKEND_MANIFEST_KEY"]),
            AwaitingWorker(name="serve-1", keys=["BACKEND_MANIFEST_KEY"]),
        ]
    else:
        roll = RecycleReport(
            rows=[
                RecycleRow(
                    name="backend-1",
                    kind="backend",
                    generation_before=1,
                    status=RECYCLED,
                    keys=["BACKEND_MANIFEST_KEY"],
                )
            ]
        )
        verdict = []
    return ApplyResult(
        fleet=FleetResult(op="reload_config", results=[]),
        local={"status": "ok", "env_keys": 2, "awaiting_respawn": []},
        decision=_writer_moved_decision(WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=2)),
        recycle=roll,
        awaiting_respawn=verdict,
        self_exit_armed=not stopped,
    )


def _writer_moved_decision(writer: Any) -> Any:
    """The decision of a reload whose writer and ``backend-1`` report BACKEND_MANIFEST_KEY moved."""
    from tai42_skeleton.config.recycle_policy import RecycleDecision, RecycleReason, ReportedKeys

    return RecycleDecision(
        writer=writer,
        reported={"backend-1": ReportedKeys(generation=1, keys=("BACKEND_MANIFEST_KEY",))},
        unconfirmed={},
        clean=frozenset(),
        writer_reason=RecycleReason(keys=("BACKEND_MANIFEST_KEY",)),
        withheld={},
        addressed=None,
        report_unreachable=False,
    )


def _with_bus(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve the routes over a fake bus; the graceful exits asked of its latch, by reason."""
    from tai42_skeleton.app import instance

    from .._fakes.bus import FakeBus

    bus = FakeBus(origin="serve-1")
    monkeypatch.setattr(instance.app, "_bus", bus)
    return bus.exit_requests


async def test_an_install_whose_env_recycle_converged_carries_the_writers_own_deferred_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)

    async def _ok(ref, version):
        return {"ref": ref, "version": "1.0.0", "reload": _install_result(stopped=False)}

    _use_installer(monkeypatch, install=_ok)
    resp = await router.marketplace_install(_post({"ref": "tai42/toolbox"}))
    assert resp.status_code == 200
    reload = _data(resp)["data"]["reload"]
    assert [entry["status"] for entry in reload["recycle"]["recycle"]] == ["recycled", "self-deferred"]
    assert reload["awaiting_respawn"] == []


async def test_an_install_whose_env_recycle_stopped_names_the_workers_awaiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)

    async def _ok(ref, version):
        return {"ref": ref, "version": "1.0.0", "reload": _install_result(stopped=True)}

    _use_installer(monkeypatch, install=_ok)
    resp = await router.marketplace_install(_post({"ref": "tai42/toolbox"}))
    reload = _data(resp)["data"]["reload"]
    assert [w["name"] for w in reload["awaiting_respawn"]] == ["backend-1", "serve-1"]


async def test_a_projected_install_whose_env_recycle_converged_runs_the_writer_self_exit_after_its_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from tai42_contract.manifest import ApiToolsConfig

    from tai42_skeleton.operations import OperationRegistry, operation_metadata_of
    from tai42_skeleton.operations.projection import project_operations

    exits = _with_bus(monkeypatch)

    async def _ok(ref, version):
        # The install's env roll converged with the writer needing its own restart: the roll owes
        # the writer's exit into the call's open scope, as ``decide_and_roll`` does.
        from tai42_skeleton.app import instance
        from tai42_skeleton.app.writer_exit import open_writer_exit_scope

        bus = instance.app.bus
        scope = open_writer_exit_scope("the install")
        scope.hold_exit(bus, "the install")
        scope.owe(bus)
        return {"ref": ref, "version": "1.0.0", "reload": _install_result(stopped=False)}

    _use_installer(monkeypatch, install=_ok)
    monkeypatch.setattr(mkt_ops, "component_store_configured", lambda _component: True)
    reg = OperationRegistry()
    reg.register(operation_metadata_of(mkt_ops.marketplace_install))
    registered: dict[str, Any] = {}

    class _Tools:
        def tool(self, *, force, name, tags, annotations):  # type: ignore[no-untyped-def]
            def decorator(func):  # type: ignore[no-untyped-def]
                registered[name] = func
                return func

            return decorator

    project_operations(
        SimpleNamespace(tools=_Tools()),
        ApiToolsConfig(include=["marketplace_install"], expose_destructive=True),
        registry=reg,
    )
    reply = await registered["marketplace_install"](ref="tai42/toolbox")

    assert [entry["status"] for entry in reply["reload"]["recycle"]["recycle"]] == ["recycled", "self-deferred"]
    assert reply["reload"]["awaiting_respawn"] == []
    assert exits == [WRITER_EXIT_REASON]


async def test_install_malformed_body_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    # A body missing the required ``ref`` fails the request-model parse (422).
    resp = await router.marketplace_install(_post({"version": "1.0.0"}))
    assert resp.status_code == 422


async def test_install_bad_ref_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _bad(ref, version):
        raise MalformedRefError("ref must be 'namespace/name', got 'noslash'")

    _use_installer(monkeypatch, install=_bad)
    resp = await router.marketplace_install(_post({"ref": "noslash"}))
    assert resp.status_code == 400


async def test_install_reload_gate_locked_is_503_reloading(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _never(ref, version):  # pragma: no cover - the gate rejects first
        raise AssertionError("the operation must not run while the gate is locked")

    _use_installer(monkeypatch, install=_never)
    async with reload_gate.synchronized():
        resp = await router.marketplace_install(_post({"ref": "tai42/toolbox"}))
    assert resp.status_code == 503
    body = _data(resp)
    assert body["reloading"] is True
    assert resp.headers["Retry-After"] == "5"


async def test_install_operation_in_progress_is_retriable_503(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _busy(ref, version):
        raise OperationInProgressError("another marketplace operation is in progress; retry shortly")

    _use_installer(monkeypatch, install=_busy)
    resp = await router.marketplace_install(_post({"ref": "tai42/toolbox"}))
    assert resp.status_code == 503
    assert "in progress" in _data(resp)["error"]


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (VersionRefusedError("killed"), 409),
        (ManifestCollisionError("collides"), 409),
        (ContractIncompatibleError("range"), 409),
        (PluginDependencyConflictError("cannot install tai42/toolbox 1.0.0: its dependencies conflict"), 409),
        (UndeclaredServerCommandError("tai42/srv: mcp-server item srv names command 'python'"), 409),
        (ServerCommandCollisionError("cannot install tai42/b 1.0.0: its package tai42-b declares 'x'"), 409),
        (RouteCollisionError([{"item": "relay", "full_path": "/api/x", "methods": ["GET"]}]), 409),
        (ReservedRoutePrefixError(["/api/auth/x"], ["/api/auth"]), 409),
        (RouteMountError("unknown item"), 400),
        (PublicRoutesNotAcceptedError([{"item": "relay", "full_path": "/api/x", "methods": ["GET"]}]), 400),
        (InstallStateError("already installed"), 409),
        (InstallStateError("not installed", not_installed=True), 404),
        (ListingNotFoundError("not found"), 404),
        (RegistryUnreachableError("down"), 502),
        (PipFailedError(["install", "x"], 1, "boom"), 500),
        (
            ArtifactIntegrityError(expected_sha256="a" * 64, actual_sha256="b" * 64, artifact_ref="https://x/y.tgz"),
            500,
        ),
        # The to_operation_error fall-through: an environment/invariant fault the
        # operation could not complete maps to a terminal 500.
        (ManifestComposeError("composed manifest invalid"), 500),
        (LocalStateError("stored spec corrupt"), 500),
        (InstallUnwindError(RuntimeError("step"), RuntimeError("unwind")), 500),
        (PipUnavailableError("no pip"), 500),
        (PluginsDirError("packaged plugins need TAI_PLUGINS_DIR"), 500),
        (PluginMigrationError("tai42/acct: its migrations failed"), 500),
    ],
)
async def test_install_error_families_map_to_status(
    monkeypatch: pytest.MonkeyPatch, error: Exception, status: int
) -> None:
    async def _raise(ref, version):
        raise error

    _use_installer(monkeypatch, install=_raise)
    resp = await router.marketplace_install(_post({"ref": "tai42/toolbox"}))
    assert resp.status_code == status
    assert "error" in _data(resp)


_RESTORED = "the previous plugin environment was restored"


def _restored(error: Exception) -> Exception:
    error.add_note(_RESTORED)
    return error


@pytest.mark.parametrize(
    ("error", "text", "status"),
    [
        (ManifestCollisionError("collides"), "collides", 409),
        (LocalStateError("stored spec corrupt"), "stored spec corrupt", 500),
        (PluginMigrationError("tai42/acct: its migrations failed"), "tai42/acct: its migrations failed", 500),
    ],
)
async def test_install_error_envelope_ends_with_the_restored_notice(
    monkeypatch: pytest.MonkeyPatch, error: Exception, text: str, status: int
) -> None:
    async def _raise(ref, version):
        raise _restored(error)

    _use_installer(monkeypatch, install=_raise)
    resp = await router.marketplace_install(_post({"ref": "tai42/toolbox"}))
    assert resp.status_code == status
    assert _data(resp)["error"] == f"{text}\n{_RESTORED}"


async def test_install_pip_failure_keeps_its_output_and_ends_with_the_restored_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(ref, version):
        raise _restored(PipFailedError(["install", "x"], 1, "pip said no"))

    _use_installer(monkeypatch, install=_raise)
    resp = await router.marketplace_install(_post({"ref": "tai42/toolbox"}))
    body = _data(resp)
    assert body["error"].endswith(f"\n{_RESTORED}")
    assert body["pip_output"] == "pip said no"


async def test_install_artifact_integrity_carries_digests_in_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    # The integrity failure is a terminal 500 whose envelope carries the digests
    # and the rejected artifact URL for the operator.
    async def _raise(ref, version):
        raise ArtifactIntegrityError(
            expected_sha256="a" * 64, actual_sha256="b" * 64, artifact_ref="https://codeload/x.tgz"
        )

    _use_installer(monkeypatch, install=_raise)
    resp = await router.marketplace_install(_post({"ref": "tai42/toolbox"}))
    assert resp.status_code == 500
    body = _data(resp)
    assert body["expected_sha256"] == "a" * 64
    assert body["actual_sha256"] == "b" * 64
    assert body["artifact_ref"] == "https://codeload/x.tgz"


async def test_install_route_collision_carries_code_and_list(monkeypatch: pytest.MonkeyPatch) -> None:
    # A route collision is a 409 whose envelope carries the stable code + the list so
    # the UI keys a remap flow on it.
    collisions = [{"item": "relay", "full_path": "/api/relay/status", "methods": ["GET"], "conflict_owner": "core"}]

    async def _raise(ref, version):
        raise RouteCollisionError(collisions)

    _use_installer(monkeypatch, install=_raise)
    resp = await router.marketplace_install(_post({"ref": "acme/relay"}))
    assert resp.status_code == 409
    body = _data(resp)
    assert body["code"] == "ROUTE_COLLISION"
    assert body["collisions"] == collisions


async def test_install_public_not_accepted_carries_code_and_list(monkeypatch: pytest.MonkeyPatch) -> None:
    # An unaccepted public route is a 400 whose envelope carries the stable code + the
    # rows so the UI renders the acceptance gate.
    rows = [{"item": "relay", "full_path": "/api/relay/status", "methods": ["GET"]}]

    async def _raise(ref, version):
        raise PublicRoutesNotAcceptedError(rows)

    _use_installer(monkeypatch, install=_raise)
    resp = await router.marketplace_install(_post({"ref": "acme/relay"}))
    assert resp.status_code == 400
    body = _data(resp)
    assert body["code"] == "PUBLIC_ROUTES_NOT_ACCEPTED"
    assert body["public_routes"] == rows


async def test_uninstall_and_update_wrap_data(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(ref, version):
        return {"ref": ref, "ok": True, "reload": None}

    _use_installer(monkeypatch, install=_ok)
    r1 = await router.marketplace_uninstall(_post({"ref": "tai42/toolbox"}))
    r2 = await router.marketplace_update(_post({"ref": "tai42/toolbox", "version": "2.0.0"}))
    assert _data(r1)["data"]["ok"] is True
    assert _data(r2)["data"]["ok"] is True


async def test_install_preview_wraps_data(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _preview(ref, version):
        return {"ref": ref, "items": [], "collisions": [], "requires_public_acceptance": False}

    _use_installer(monkeypatch, install=_preview)
    resp = await router.marketplace_install_preview(_post({"ref": "acme/relay"}))
    assert resp.status_code == 200
    assert _data(resp)["data"]["ref"] == "acme/relay"


# -- upgrade-all --------------------------------------------------------------


async def test_upgrade_all_wraps_the_per_ref_report(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.marketplace.installer_update import UpgradeAllReport

    report = [
        {"ref": "tai42/a", "outcome": "upgraded", "detail": "1.0.0 -> 1.2.0"},
        {"ref": "tai42/b", "outcome": "no-compatible-version", "detail": "no published version supports 0.3.0"},
    ]

    async def _run(ref, version):
        return UpgradeAllReport(results=report, roll=None)

    _use_installer(monkeypatch, install=_run)
    resp = await router.marketplace_upgrade_all(_post({}))
    assert resp.status_code == 200
    # The batch reloaded nothing, so no roll ran and nobody awaits a respawn.
    assert _data(resp)["data"] == {
        "results": report,
        "recycle": None,
        "awaiting_respawn": [],
        "roll_deferred": False,
        "roll_deferred_to": None,
    }


async def test_an_upgrade_all_whose_roll_converged_carries_it_with_the_writers_own_deferred_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_skeleton.app.bus import WorkerIdentity, WorkerKind
    from tai42_skeleton.app.recycle import RECYCLED, RecycleReport, RecycleRow
    from tai42_skeleton.config.recycle_roll import RollOutcome
    from tai42_skeleton.marketplace.installer_update import UpgradeAllReport

    _with_bus(monkeypatch)
    rows = [{"ref": "tai42/a", "outcome": "upgraded", "detail": "1.0.0 -> 1.2.0"}]
    roll = RollOutcome(
        decision=_writer_moved_decision(WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=2)),
        recycle=RecycleReport(
            rows=[
                RecycleRow(
                    name="backend-1",
                    kind="backend",
                    generation_before=1,
                    status=RECYCLED,
                    keys=["BACKEND_MANIFEST_KEY"],
                )
            ]
        ),
        awaiting_respawn=[],
        self_exit_armed=True,
    )

    async def _run(ref, version):
        return UpgradeAllReport(results=rows, roll=roll)

    _use_installer(monkeypatch, install=_run)
    monkeypatch.setattr(mkt_ops, "component_store_configured", lambda _component: True)
    resp = await router.marketplace_upgrade_all(_post({}))

    assert resp.status_code == 200
    data = _data(resp)["data"]
    assert data["results"] == rows
    assert [(e["name"], e["status"], e["keys"]) for e in data["recycle"]["recycle"]] == [
        ("backend-1", "recycled", ["BACKEND_MANIFEST_KEY"]),
        ("serve-1", "self-deferred", ["BACKEND_MANIFEST_KEY"]),
    ]
    assert data["awaiting_respawn"] == []


async def test_upgrade_all_busy_fleet_lock_is_retriable_503(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _busy(ref, version):
        raise OperationInProgressError("another marketplace operation is in progress; retry shortly")

    _use_installer(monkeypatch, install=_busy)
    resp = await router.marketplace_upgrade_all(_post({}))
    assert resp.status_code == 503
    assert "in progress" in _data(resp)["error"]


async def test_upgrade_all_reload_gate_locked_is_503_reloading(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _never(ref, version):  # pragma: no cover - the gate rejects first
        raise AssertionError("the operation must not run while the gate is locked")

    _use_installer(monkeypatch, install=_never)
    async with reload_gate.synchronized():
        resp = await router.marketplace_upgrade_all(_post({}))
    assert resp.status_code == 503
    assert _data(resp)["reloading"] is True


# -- OFF state: no install-attribution store configured -----------------


def _off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the OFF gate: drop the feature's own password AND the shared default so
    # the fresh-read presence probe answers unconfigured (overrides the autouse ON).
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)


async def test_installed_off_answers_empty_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no store the inventory is an empty installed list.
    _off(monkeypatch)
    body = await mkt_ops.marketplace_installed()
    assert body["installed"] == []


async def test_advisories_off_answers_empty_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    # With nothing installed the advisory snapshot is empty, fetched now.
    _off(monkeypatch)
    body = await mkt_ops.marketplace_advisories()
    assert body["advisories"] == []
    assert isinstance(body["fetched_at"], str)


async def test_install_off_refuses_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # An install with no attribution store refuses with a named, machine-readable reason.
    _off(monkeypatch)
    with pytest.raises(NotSupportedError) as exc_info:
        await mkt_ops.marketplace_install("tai42/toolbox")
    assert exc_info.value.extra["code"] == "marketplace-not-configured"


async def test_uninstall_off_refuses_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _off(monkeypatch)
    with pytest.raises(NotSupportedError) as exc_info:
        await mkt_ops.marketplace_uninstall("tai42/toolbox")
    assert exc_info.value.extra["code"] == "marketplace-not-configured"


async def test_update_off_refuses_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _off(monkeypatch)
    with pytest.raises(NotSupportedError) as exc_info:
        await mkt_ops.marketplace_update("tai42/toolbox")
    assert exc_info.value.extra["code"] == "marketplace-not-configured"


async def test_upgrade_all_off_refuses_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _off(monkeypatch)
    with pytest.raises(NotSupportedError) as exc_info:
        await mkt_ops.marketplace_upgrade_all()
    assert exc_info.value.extra["code"] == "marketplace-not-configured"
