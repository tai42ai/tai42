"""The installed-packages snapshot: which distribution provides a top-level import package.

A name the installed metadata records maps through it, without consulting the import
system. A name no metadata records — a package an import-hook editable install serves — is
found where the import system places it and attributed to the editable distribution whose
PEP 610 source directory holds it.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path

import pytest

from tai42_skeleton.marketplace.installed_packages import (
    InstalledMetadataError,
    editable_source_dir,
    hook_served_editable,
    installed_packages,
    recorded_files,
)

from ..db import _chain_plugin, _hook_editable


@pytest.fixture(autouse=True)
def _forget_activated_modules() -> Iterator[None]:
    yield
    _hook_editable.forget_modules()


def _write_dist_info(site: Path, dist: str, files: dict[str, str]) -> metadata.Distribution:
    """A ``.dist-info`` for ``dist`` in ``site`` holding ``files`` plus METADATA and a RECORD of them."""
    dist_info = site / f"{dist.replace('-', '_')}-1.0.0.dist-info"
    dist_info.mkdir(parents=True)
    files = {"METADATA": f"Metadata-Version: 2.4\nName: {dist}\nVersion: 1.0.0\n", **files}
    for name, body in files.items():
        (dist_info / name).write_text(body)
    rows = [*(f"{dist_info.name}/{name},," for name in files), f"{dist_info.name}/RECORD,,"]
    (dist_info / "RECORD").write_text("\n".join(rows) + "\n")
    return metadata.PathDistribution(dist_info)


def _refuse_find_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(name: str, package: str | None = None) -> None:
        raise AssertionError(f"find_spec({name!r}) consulted for a recorded name")

    monkeypatch.setattr(importlib.util, "find_spec", refuse)


def test_recorded_name_maps_through_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    site = tmp_path / "site"
    _chain_plugin.lay_out(site, spec_text=_chain_plugin.spec_yaml())
    snapshot = installed_packages([str(site)])
    _refuse_find_spec(monkeypatch)

    assert snapshot.distributions_for(_chain_plugin.TOP_LEVEL) == [_chain_plugin.DIST]


def test_name_served_by_a_pth_path_line_maps_through_metadata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    source = tmp_path / "project" / "src"
    (source / "tai42_pathprobe").mkdir(parents=True)
    (source / "tai42_pathprobe" / "__init__.py").write_text("raise RuntimeError('never imported')\n")
    site = tmp_path / "site"
    site.mkdir()
    (site / "tai42_pathprobe.pth").write_text(f"{source}\n")
    dist_info = site / "tai42_pathprobe-1.0.0.dist-info"
    _write_dist_info(site, "tai42-pathprobe", {})
    (dist_info / "RECORD").write_text((dist_info / "RECORD").read_text() + "tai42_pathprobe.pth,,\n")
    snapshot = installed_packages([str(site)])
    _refuse_find_spec(monkeypatch)

    assert snapshot.distributions_for("tai42_pathprobe") == ["tai42-pathprobe"]


@pytest.mark.parametrize("shape", _hook_editable.SHAPES)
def test_hook_served_name_is_attributed_to_its_editable_distribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shape: str
) -> None:
    install = _hook_editable.lay_out(tmp_path, shape, spec_text=_hook_editable.spec_yaml())
    _hook_editable.activate(monkeypatch, install)

    snapshot = installed_packages()

    assert _hook_editable.TOP_LEVEL not in snapshot.top_levels
    assert snapshot.distributions_for(_hook_editable.TOP_LEVEL) == [_hook_editable.DIST]


def test_nested_editable_source_directories_attribute_to_the_innermost(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outer = tmp_path / "outer"
    install = _hook_editable.lay_out(
        tmp_path, "editables", spec_text=_hook_editable.spec_yaml(), project=outer / "inner"
    )
    _write_dist_info(
        install.site,
        "tai42-outerprobe",
        {"direct_url.json": json.dumps({"url": outer.as_uri(), "dir_info": {"editable": True}})},
    )
    _hook_editable.activate(monkeypatch, install)

    assert installed_packages().distributions_for(_hook_editable.TOP_LEVEL) == [_hook_editable.DIST]


def test_name_nothing_serves_maps_to_no_distribution() -> None:
    assert installed_packages().distributions_for("tai42_no_such_top_level_xyz") == []


def test_editable_source_dir_reads_pep_610(tmp_path: Path) -> None:
    project = tmp_path / "project"
    dist = _write_dist_info(
        tmp_path / "site",
        "tai42-editprobe",
        {"direct_url.json": json.dumps({"url": project.as_uri(), "dir_info": {"editable": True}})},
    )

    assert editable_source_dir(dist) == project


@pytest.mark.parametrize(
    "direct_url",
    [
        None,
        {"url": "file:///srv/project", "dir_info": {}},
        {"url": "https://example.com/project-1.0.0.tar.gz", "archive_info": {}},
        {"url": "https://example.com/project.git", "vcs_info": {"vcs": "git", "commit_id": "abc"}},
    ],
    ids=["absent", "directory-not-editable", "archive", "vcs"],
)
def test_editable_source_dir_is_none_for_a_non_editable_install(tmp_path: Path, direct_url: dict | None) -> None:
    files = {} if direct_url is None else {"direct_url.json": json.dumps(direct_url)}
    dist = _write_dist_info(tmp_path / "site", "tai42-editprobe", files)

    assert editable_source_dir(dist) is None


@pytest.mark.parametrize("body", ["{not json", "[]", '{"url": "file:///p", "dir_info": "editable"}'])
def test_malformed_direct_url_is_loud(tmp_path: Path, body: str) -> None:
    dist = _write_dist_info(tmp_path / "site", "tai42-editprobe", {"direct_url.json": body})

    with pytest.raises(InstalledMetadataError, match="tai42-editprobe"):
        editable_source_dir(dist)


@pytest.mark.parametrize("shape", _hook_editable.SHAPES)
def test_hook_served_editable_install_is_recognised(tmp_path: Path, shape: str) -> None:
    install = _hook_editable.lay_out(tmp_path, shape, spec_text=None)
    dist = next(iter(metadata.distributions(path=[str(install.site)])))

    assert hook_served_editable(dist, recorded_files(dist)) is True


def test_path_based_editable_install_is_not_hook_served(tmp_path: Path) -> None:
    source = tmp_path / "project" / "src"
    source.mkdir(parents=True)
    site = tmp_path / "site"
    site.mkdir()
    (site / "tai42_pathprobe.pth").write_text(f"{source}\n")
    dist_info = site / "tai42_pathprobe-1.0.0.dist-info"
    dist = _write_dist_info(
        site,
        "tai42-pathprobe",
        {"direct_url.json": json.dumps({"url": (tmp_path / "project").as_uri(), "dir_info": {"editable": True}})},
    )
    (dist_info / "RECORD").write_text((dist_info / "RECORD").read_text() + "tai42_pathprobe.pth,,\n")

    assert hook_served_editable(dist, recorded_files(dist)) is False


def test_regular_install_is_not_hook_served(tmp_path: Path) -> None:
    site = tmp_path / "site"
    _chain_plugin.lay_out(site, spec_text=_chain_plugin.spec_yaml())
    dist = next(iter(metadata.distributions(path=[str(site)])))

    assert hook_served_editable(dist, recorded_files(dist)) is False


def test_snapshot_resolves_each_unrecorded_name_once(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = installed_packages()
    calls: list[str] = []
    real_find_spec = importlib.util.find_spec

    def counting(name: str, package: str | None = None):  # type: ignore[no-untyped-def]
        calls.append(name)
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", counting)
    snapshot.distributions_for("tai42_no_such_top_level_xyz")
    snapshot.distributions_for("tai42_no_such_top_level_xyz")

    assert calls == ["tai42_no_such_top_level_xyz"]
