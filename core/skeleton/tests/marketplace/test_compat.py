"""Contract-compat verdicts and the update-target computation.

``compat.metadata`` (the module's importlib.metadata seam) is monkeypatched with
a stub namespace per test, so every verdict is driven by controlled dist
metadata — no dependence on the environment's installed distributions.
"""

from __future__ import annotations

from importlib.metadata import Distribution
from types import SimpleNamespace
from typing import cast

import pytest
from packaging.version import Version

from tai42_skeleton.marketplace import compat
from tai42_skeleton.marketplace.compat import (
    CompatVerdict,
    declared_compat,
    distribution_compat,
    distribution_map,
    module_compat,
    registry_range_admits,
    update_targets,
)
from tai42_skeleton.marketplace.environments.layout import (
    BUILD_RECORD,
    BuildInput,
    Excluded,
    layer_dir,
    write_build_record,
)
from tai42_skeleton.marketplace.environments.swap import point_current
from tai42_skeleton.marketplace.environments.verify import verify_environments
from tai42_skeleton.marketplace.errors import CurrentRecordError, LocalStateError, RegistryResponseError
from tai42_skeleton.marketplace.installed_packages import InstalledDistribution, InstalledPackages, LeftOutModule


def _read(name: str, requires: list[str] | None) -> InstalledDistribution:
    """A snapshot's read of distribution ``name`` whose ``Requires-Dist`` lines are ``requires``."""
    dist = cast(Distribution, SimpleNamespace(requires=requires))
    return InstalledDistribution(dist=dist, name=name, recorded=[], top_levels=[])


def _snapshot(top_levels: dict[str, list[str]], requires: dict[str, list[str] | None]) -> InstalledPackages:
    """An installed-packages snapshot mapping ``top_levels`` and holding a read of each ``requires`` key."""
    return InstalledPackages(
        top_levels=top_levels,
        editable_sources={},
        distributions={name: _read(name, lines) for name, lines in requires.items()},
    )


def _stub_metadata(
    monkeypatch: pytest.MonkeyPatch,
    *,
    contract_version: str = "0.3.0",
    requires: dict[str, list[str] | None] | None = None,
    top_levels: dict[str, list[str]] | None = None,
) -> None:
    """Install a fake ``compat.metadata`` whose ``version()`` answers only for tai42-contract,
    and the installed-packages snapshot from ``top_levels`` holding a read of each ``requires``
    distribution."""
    requires_map = requires or {}

    def _version(dist: str) -> str:
        assert dist == "tai42-contract"
        return contract_version

    stub = SimpleNamespace(version=_version)
    monkeypatch.setattr(compat, "metadata", stub)
    monkeypatch.setattr(
        compat,
        "installed_packages",
        lambda: _snapshot(dict(top_levels or {}), requires_map),
    )


# -- distribution_compat ------------------------------------------------------


def test_declared_and_satisfied_is_compatible(monkeypatch) -> None:
    _stub_metadata(monkeypatch)
    read = _read("acme-plugin", ["pydantic>=2", "tai42-contract>=0.3,<0.4"])
    assert distribution_compat(read) == CompatVerdict("compatible")


def test_declared_and_excluded_is_incompatible_naming_both_versions(monkeypatch) -> None:
    _stub_metadata(monkeypatch)
    verdict = distribution_compat(_read("acme-plugin", ["tai42-contract>=0.2,<0.3"]))
    assert verdict.status == "incompatible"
    assert verdict.reason is not None
    assert "acme-plugin" in verdict.reason  # the dist
    assert "<0.3" in verdict.reason  # its declared range...
    assert ">=0.2" in verdict.reason  # ...both halves
    assert "0.3.0 is running" in verdict.reason  # the running contract
    assert "tai plugins update" in verdict.reason  # the remedy


def test_no_declared_range_is_unknown(monkeypatch) -> None:
    _stub_metadata(monkeypatch)
    verdict = distribution_compat(_read("acme-plugin", ["pydantic>=2"]))
    assert verdict.status == "unknown"
    assert verdict.reason is not None
    assert "declares no tai42-contract requirement" in verdict.reason


def test_a_distribution_with_no_requirements_metadata_is_unknown(monkeypatch) -> None:
    _stub_metadata(monkeypatch)
    assert distribution_compat(_read("acme-plugin", None)).status == "unknown"


def test_extra_gated_declaration_is_not_a_runtime_range(monkeypatch) -> None:
    # A ``; extra == "dev"`` line is not part of the runtime dependency set, so
    # it must not produce a (possibly refusing) verdict.
    _stub_metadata(monkeypatch)
    assert distribution_compat(_read("acme-plugin", ['tai42-contract<0.1; extra == "dev"'])).status == "unknown"


def test_malformed_foreign_requirement_line_is_skipped(monkeypatch) -> None:
    # A stranger's garbled Requires-Dist line must not poison the verdict: the
    # tai42-contract line still decides.
    _stub_metadata(monkeypatch)
    read = _read("acme-plugin", ["¡not a requirement¡", "tai42-contract>=0.3,<0.4"])
    assert distribution_compat(read) == CompatVerdict("compatible")


def test_prerelease_running_contract_inside_the_range_passes(monkeypatch) -> None:
    # Same prereleases=True semantics as the installer's install-time check: a
    # dev-versioned editable contract checkout is never spuriously refused.
    _stub_metadata(monkeypatch, contract_version="0.3.1.dev3")
    assert distribution_compat(_read("acme-plugin", ["tai42-contract>=0.3,<0.4"])) == CompatVerdict("compatible")


def test_multiple_runtime_declarations_and_together(monkeypatch) -> None:
    _stub_metadata(monkeypatch)
    read = _read("acme-plugin", ["tai42-contract>=0.3", "tai42-contract<0.3.0"])
    assert distribution_compat(read).status == "incompatible"


# -- declared_compat ----------------------------------------------------------


def test_a_declared_range_the_running_contract_meets_is_compatible(monkeypatch) -> None:
    _stub_metadata(monkeypatch)
    assert declared_compat("acme-plugin", ">=0.3,<0.4") == CompatVerdict("compatible")


def test_a_declared_range_the_running_contract_is_outside_is_incompatible_in_the_dist_verdict_s_words(
    monkeypatch,
) -> None:
    _stub_metadata(monkeypatch)
    dist_verdict = distribution_compat(_read("acme-plugin", ["tai42-contract>=0.2,<0.3"]))
    assert declared_compat("acme-plugin", ">=0.2,<0.3") == dist_verdict
    assert declared_compat("acme-plugin", ">=0.2,<0.3") == CompatVerdict(
        "incompatible",
        "acme-plugin requires tai42-contract <0.3,>=0.2, but 0.3.0 is running — update the plugin to a release "
        "supporting 0.3.0 (tai plugins update) or move the core onto a contract the plugin supports",
    )


def test_no_declared_range_is_unknown_in_the_dist_verdict_s_words(monkeypatch) -> None:
    _stub_metadata(monkeypatch)
    assert declared_compat("acme-plugin", None) == distribution_compat(_read("acme-plugin", ["pydantic>=2"]))
    assert declared_compat("acme-plugin", None).status == "unknown"


# -- module_compat ------------------------------------------------------------


def test_module_with_no_dist_mapping_is_unknown(monkeypatch) -> None:
    _stub_metadata(monkeypatch, top_levels={})
    verdict = module_compat("acme_pkg.tools")
    assert verdict.status == "unknown"
    assert verdict.reason is not None
    assert "maps to no installed distribution" in verdict.reason


def test_module_maps_through_its_top_level_package(monkeypatch) -> None:
    _stub_metadata(
        monkeypatch,
        requires={"acme-plugin": ["tai42-contract>=0.2,<0.3"]},
        top_levels={"acme_pkg": ["acme-plugin"]},
    )
    assert module_compat("acme_pkg.sub.module").status == "incompatible"


def test_module_any_incompatible_owning_dist_decides(monkeypatch) -> None:
    _stub_metadata(
        monkeypatch,
        requires={"acme-a": ["tai42-contract>=0.3,<0.4"], "acme-b": ["tai42-contract<0.2"]},
        top_levels={"acme_pkg": ["acme-a", "acme-b"]},
    )
    verdict = module_compat("acme_pkg")
    assert verdict.status == "incompatible"
    assert verdict.reason is not None
    assert "acme-b" in verdict.reason


def test_module_uses_the_passed_snapshot_map(monkeypatch) -> None:
    # A passed snapshot bypasses the installed-packages read entirely — the one
    # boot pass reuses its snapshot instead of rescanning per module.
    def _boom():  # pragma: no cover - the snapshot must win
        raise AssertionError("the installed packages must not be re-read when a snapshot is passed")

    _stub_metadata(monkeypatch)
    monkeypatch.setattr(compat, "installed_packages", _boom)
    verdict = module_compat(
        "acme_pkg", _snapshot({"acme_pkg": ["acme-plugin"]}, {"acme-plugin": ["tai42-contract>=0.3,<0.4"]})
    )
    assert verdict == CompatVerdict("compatible")


def test_a_module_of_a_plugin_left_out_of_the_layer_is_incompatible_with_the_recorded_reason(monkeypatch) -> None:
    _stub_metadata(monkeypatch)
    snapshot = InstalledPackages(
        top_levels={},
        editable_sources={},
        distributions={},
        quarantined={"acme_pkg.tools": LeftOutModule("acme-plugin", "acme-plugin requires ...")},
    )
    assert module_compat("acme_pkg.tools", snapshot) == CompatVerdict("incompatible", "acme-plugin requires ...")


def test_an_installed_distribution_decides_before_a_recorded_exclusion(monkeypatch) -> None:
    _stub_metadata(monkeypatch)
    snapshot = InstalledPackages(
        top_levels={"acme_pkg": ["acme-plugin"]},
        editable_sources={},
        distributions={"acme-plugin": _read("acme-plugin", ["tai42-contract>=0.3,<0.4"])},
        quarantined={"acme_pkg.tools": LeftOutModule("acme-plugin", "acme-plugin requires ...")},
    )
    assert module_compat("acme_pkg.tools", snapshot) == CompatVerdict("compatible")


def test_the_distribution_map_holds_the_active_layer_s_exclusions_by_module(monkeypatch, tmp_path) -> None:
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(compat, "configured_plugins_dir", lambda: tmp_path)
    environment = layer_dir(tmp_path)
    build = environment.build("b1")
    build.mkdir(parents=True)
    left_out = Excluded(
        input=BuildInput(ref="acme/q", package="acme-q", version="1.0.0", source="pypi", sha256=None),
        modules=("acme_q.tools", "acme_q.routes"),
        reason="acme-q requires ...",
    )
    write_build_record(build, [], [left_out])
    point_current(environment, "b1")

    assert distribution_map().quarantined == {
        "acme_q.tools": LeftOutModule("acme-q", "acme-q requires ..."),
        "acme_q.routes": LeftOutModule("acme-q", "acme-q requires ..."),
    }


def test_a_left_out_plugin_sharing_a_namespace_package_with_a_layer_plugin_is_incompatible(
    monkeypatch, tmp_path
) -> None:
    # Two in-process plugins share the namespace package ``acme``: the layer installed acme-a
    # and left acme-b out, so only acme-a's distribution provides ``acme``.
    _stub_metadata(monkeypatch)
    snapshot = _snapshot({"acme": ["acme-a"]}, {"acme-a": ["tai42-contract>=0.3,<0.4"]})
    monkeypatch.setattr(compat, "installed_packages", lambda: snapshot)
    monkeypatch.setattr(compat, "configured_plugins_dir", lambda: tmp_path)
    environment = layer_dir(tmp_path)
    build = environment.build("b1")
    build.mkdir(parents=True)
    left_out = Excluded(
        input=BuildInput(ref="acme/b", package="acme-b", version="1.0.0", source="pypi", sha256=None),
        modules=("acme.b.tools",),
        reason="acme-b requires ...",
    )
    write_build_record(build, [], [left_out])
    point_current(environment, "b1")
    installed = distribution_map()

    assert module_compat("acme.b.tools", installed) == CompatVerdict("incompatible", "acme-b requires ...")
    assert module_compat("acme.b.tools.extra", installed) == CompatVerdict("incompatible", "acme-b requires ...")
    assert module_compat("acme.a.tools", installed) == CompatVerdict("compatible")


def test_a_current_layer_build_whose_record_does_not_read_refuses_in_the_verification_words(
    monkeypatch, tmp_path
) -> None:
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(compat, "configured_plugins_dir", lambda: tmp_path)
    environment = layer_dir(tmp_path)
    build = environment.build("b1")
    build.mkdir(parents=True)
    (build / BUILD_RECORD).write_text("{truncated", encoding="utf-8")
    point_current(environment, "b1")

    [finding] = verify_environments(tmp_path, []).findings

    with pytest.raises(CurrentRecordError) as raised:
        distribution_map()

    assert raised.value.finding == finding
    assert str(raised.value) == finding.report_text()


def test_a_current_layer_build_without_its_record_refuses_in_the_verification_words(monkeypatch, tmp_path) -> None:
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(compat, "configured_plugins_dir", lambda: tmp_path)
    environment = layer_dir(tmp_path)
    environment.build("b1").mkdir(parents=True)
    point_current(environment, "b1")
    [finding] = verify_environments(tmp_path, []).findings

    with pytest.raises(CurrentRecordError) as raised:
        distribution_map()

    assert str(raised.value) == finding.report_text()


def test_the_distribution_map_holds_no_exclusions_without_a_plugins_directory(monkeypatch) -> None:
    _stub_metadata(monkeypatch)
    monkeypatch.setattr(compat, "configured_plugins_dir", lambda: None)
    assert distribution_map().quarantined == {}


# -- registry_range_admits ----------------------------------------------------


@pytest.mark.parametrize(
    ("contract_range", "admitted"),
    [(">=0.3,<0.4", True), (">=0.2,<0.3", False), (">=0.3.0.dev0,<0.4", True)],
)
def test_a_registry_range_admits_the_contracts_it_contains(contract_range: str, admitted: bool) -> None:
    assert registry_range_admits(contract_range, "0.3.0") is admitted


def test_a_registry_range_admits_a_dev_contract_inside_it() -> None:
    assert registry_range_admits(">=0.3,<0.4", "0.3.1.dev3") is True


def test_a_registry_range_that_does_not_parse_is_the_typed_registry_fault() -> None:
    with pytest.raises(RegistryResponseError, match=r"^registry served an unusable contract_range 'not!!': "):
        registry_range_admits("not!!", "0.3.0")


def test_a_registry_range_that_does_not_parse_names_the_version_it_was_served_for() -> None:
    with pytest.raises(
        RegistryResponseError, match=r"^registry served an unusable contract_range 'not!!' for version 1\.2\.0: "
    ):
        registry_range_admits("not!!", "0.3.0", version=Version("1.2.0"))


# -- update_targets -----------------------------------------------------------


def _row(version: str, status: str = "published", contract_range: str | None = ">=0.3,<0.4") -> dict:
    return {"version": version, "status": status, "contract_range": contract_range}


def test_update_targets_full_picture() -> None:
    targets = update_targets(
        [
            _row("2.0.0", contract_range=">=0.4,<0.5"),  # newer but blocked
            _row("1.5.0"),  # the compatible target
            _row("1.6.0", status="yanked"),  # unpublished — never counts
            _row("1.0.0"),
        ],
        installed_version="1.0.0",
        contract_version="0.3.0",
    )
    assert targets.latest == "2.0.0"
    assert targets.latest_compatible == "1.5.0"
    assert targets.update_available is True
    assert targets.incompatible_newer == "2.0.0"


def test_update_targets_up_to_date_and_no_rows() -> None:
    targets = update_targets([_row("1.0.0")], installed_version="1.0.0", contract_version="0.3.0")
    assert targets.update_available is False
    assert targets.incompatible_newer is None
    empty = update_targets([], installed_version="1.0.0", contract_version="0.3.0")
    assert (empty.latest, empty.latest_compatible, empty.update_available) == (None, None, False)


def test_update_targets_null_contract_range_counts_compatible() -> None:
    # Same unknown-means-compatible semantics as the dist verdict: a version
    # published without a declared range must keep updating.
    targets = update_targets([_row("2.0.0", contract_range=None)], installed_version="1.0.0", contract_version="0.3.0")
    assert targets.latest_compatible == "2.0.0"
    assert targets.update_available is True


@pytest.mark.parametrize(
    "rows",
    [
        ["not-a-row"],  # non-object element
        [_row("not-a-version")],  # non-PEP440 published version
        [{"status": "published", "contract_range": ">=0.3"}],  # missing version
        [_row("1.0.0", contract_range="!! garbage")],  # malformed range
        [{"version": "1.0.0", "status": "published", "contract_range": 42}],  # non-string range
    ],
)
def test_update_targets_garbled_registry_rows_raise_the_typed_fault(rows) -> None:
    with pytest.raises(RegistryResponseError):
        update_targets(rows, installed_version="1.0.0", contract_version="0.3.0")


def test_update_targets_garbled_local_version_is_local_state() -> None:
    # The stored installed version is LOCAL truth; its garbling is never blamed
    # on the registry.
    with pytest.raises(LocalStateError, match="not PEP 440"):
        update_targets([_row("1.0.0")], installed_version="not-a-version", contract_version="0.3.0")
