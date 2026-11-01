"""The image a plugin layer sits beside: its constraints, and the checks a layer build passes.

Distributions are hand-written ``.dist-info`` directories in temporary site directories, and
``sys.path`` is set per test, so the image is exactly what each case lays out.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from tests._fakes.plugin_environment_dists import write_dist_info

from tai42_skeleton.marketplace.environments import image as image_module
from tai42_skeleton.marketplace.environments.consistency import overlaps, unmet_requirements
from tai42_skeleton.marketplace.environments.image import (
    distributions_by_name,
    environment_constraints,
    image_paths,
)
from tai42_skeleton.marketplace.environments.layout import layer_dir, layer_site_dirs
from tai42_skeleton.marketplace.environments.swap import point_current
from tai42_skeleton.marketplace.errors import PluginsDirError


def _nameless(site: Path) -> None:
    info = site / "mystery-0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nVersion: 9.9\n", encoding="utf-8")


def _layer_build(plugins_dir: Path) -> Path:
    environment = layer_dir(plugins_dir)
    build = environment.build("b1")
    for site_dir in layer_site_dirs(build):
        site_dir.mkdir(parents=True, exist_ok=True)
    point_current(environment, "b1")
    return layer_site_dirs(environment.current)[0]


# -- the image's constraints ------------------------------------------------------


def test_the_constraints_pin_every_image_distribution_first_wins_sorted_and_never_the_layer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_a, env_b = tmp_path / "env_a", tmp_path / "env_b"
    write_dist_info(env_a, "widget", "1.1")
    write_dist_info(env_a, "alpha", "0.3")
    write_dist_info(env_b, "widget", "9.9")  # masked: env_a precedes env_b on sys.path
    layer_site = _layer_build(tmp_path / "plugins")
    write_dist_info(layer_site, "pluginpkg", "5.0")  # read through ``current``: the layer, excluded
    build_site = layer_site_dirs(layer_dir(tmp_path / "plugins").build("b1"))[0]
    monkeypatch.setattr(sys, "path", [str(env_a), str(env_b), str(layer_site), str(build_site)])

    assert environment_constraints(layer_dir(tmp_path / "plugins").root) == ["alpha==0.3", "widget==1.1"]


def test_without_a_plugins_directory_every_path_entry_is_the_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "path", ["/a", "/b"])

    assert image_paths(None) == ["/a", "/b"]


def test_a_nameless_distribution_is_passed_over(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = tmp_path / "env"
    write_dist_info(env, "widget", "1.0")
    _nameless(env)
    monkeypatch.setattr(sys, "path", [str(env)])

    assert environment_constraints(tmp_path / "plugins" / "layer") == ["widget==1.0"]


def test_a_named_image_distribution_without_a_version_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = tmp_path / "env"
    write_dist_info(env, "widget", None)
    monkeypatch.setattr(sys, "path", [str(env)])

    with pytest.raises(PluginsDirError, match="'widget' reports no version; cannot pin it"):
        environment_constraints(tmp_path / "plugins" / "layer")


def test_a_plugins_directory_holding_the_running_environment_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    layer_root = tmp_path / "plugins" / "layer"
    site_dir = layer_root / "lib" / "site-packages"
    site_dir.mkdir(parents=True)
    monkeypatch.setattr(image_module, "environment_site_dirs", lambda: [site_dir.resolve()])

    with pytest.raises(PluginsDirError, match="resolves onto the running environment"):
        image_paths(layer_root)


# -- the layer beside the image ------------------------------------------------------


def _distributions(tmp_path: Path, name: str, dists: list[tuple[str, str, list[str]]]) -> dict:
    site = tmp_path / name
    for package, version, requires in dists:
        write_dist_info(site, package, version, requires=requires)
    return distributions_by_name([str(site)])


def test_a_distribution_in_both_the_layer_and_the_image_is_an_overlap(tmp_path: Path) -> None:
    layer = _distributions(tmp_path, "layer", [("Widget", "1.0", []), ("plugin", "1.0", [])])
    image = _distributions(tmp_path, "image", [("widget", "2.0", [])])

    assert [(o.name, o.layer_version, o.image_version) for o in overlaps(layer, image)] == [("Widget", "1.0", "2.0")]


def test_a_requirement_the_image_meets_is_met(tmp_path: Path) -> None:
    layer = _distributions(tmp_path, "layer", [("plugin", "1.0", ["widget>=2"])])
    image = _distributions(tmp_path, "image", [("widget", "2.0", [])])

    assert unmet_requirements(layer, image) == []


def test_a_requirement_whose_marker_does_not_apply_is_skipped(tmp_path: Path) -> None:
    layer = _distributions(tmp_path, "layer", [("plugin", "1.0", ['absent; python_version < "3"', 'x; extra == "a"'])])

    assert unmet_requirements(layer, {}) == []


def test_an_unmet_requirement_names_the_plugin_and_what_provides_it(tmp_path: Path) -> None:
    layer = _distributions(tmp_path, "layer", [("plugin", "1.0", ["widget<2", "absent"])])
    image = _distributions(tmp_path, "image", [("widget", "2.0", [])])

    assert [item.describe() for item in unmet_requirements(layer, image)] == [
        "plugin 1.0 requires widget<2; the image provides widget 2.0",
        "plugin 1.0 requires absent; neither the plugin layer nor the image provides absent",
    ]


def test_an_unparseable_requirement_raises_naming_the_distribution(tmp_path: Path) -> None:
    layer = _distributions(tmp_path, "layer", [("plugin", "1.0", ["not a requirement !!"])])

    with pytest.raises(PluginsDirError, match=r"plugin layer distribution plugin 1\.0 declares"):
        unmet_requirements(layer, {})
