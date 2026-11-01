"""Verifying the plugin environments against the install store and this image.

Every case lays out a real plugins directory: builds with their records, a relative
``current`` symlink, and hand-written distributions in the layer; the install store is a
list of rows.
"""

from __future__ import annotations

import json
import sys
import sysconfig
from pathlib import Path

import pytest
from tests._fakes.plugin_environment_dists import write_dist_info
from tests.marketplace._specs import install_row, make_spec, mcp_server_item

from tai42_skeleton.marketplace.compat import running_contract_version
from tai42_skeleton.marketplace.environments.layout import (
    BUILD_RECORD,
    BuildInput,
    Excluded,
    layer_dir,
    layer_site_dirs,
    server_dir,
    write_build_record,
)
from tai42_skeleton.marketplace.environments.swap import point_current
from tai42_skeleton.marketplace.environments.verify import verify_environments

_RUNNING = f"Python {sys.version_info.major}.{sys.version_info.minor} on {sysconfig.get_platform()}"

_TOOLBOX = make_spec(name="toolbox", package="tai42-fixture-toolbox")
_OTHER = make_spec(name="other", package="tai42-fixture-other", version="2.0.0")
_SERVER = make_spec(name="srv", package="tai42-fixture-srv", provides=[mcp_server_item()], contract=None)
_DESCRIPTOR = make_spec(name="desc", package=None, provides=[mcp_server_item()], contract=None)


def _input(spec, version: str | None = None) -> BuildInput:
    assert spec.package is not None
    return BuildInput(ref=spec.ref, package=spec.package, version=version or spec.version, source="pypi", sha256=None)


def _lay_layer(
    plugins_dir: Path,
    inputs: list[BuildInput],
    *,
    build_id: str = "b1",
    record: bool = True,
    excluded: tuple[Excluded, ...] = (),
) -> Path:
    environment = layer_dir(plugins_dir)
    build = environment.build(build_id)
    site = layer_site_dirs(build)[0]
    site.mkdir(parents=True)
    for item in inputs:
        write_dist_info(site, item.package, item.version)
    if record:
        write_build_record(build, inputs, list(excluded))
    point_current(environment, build_id)
    return build


def _lay_server(plugins_dir: Path, item: BuildInput, *, venv_version: str | None = None) -> Path:
    environment = server_dir(plugins_dir, item.package)
    build = environment.build("s1")
    (build / "bin").mkdir(parents=True)
    (build / "bin" / "python").symlink_to(sys.executable)
    version = venv_version or f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    (build / "pyvenv.cfg").write_text(f"home = /usr/bin\nversion = {version}\n", encoding="utf-8")
    write_build_record(build, [item], [])
    point_current(environment, "s1")
    return build


def _rewrite_record(build: Path, **fields: str) -> None:
    path = build / BUILD_RECORD
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(fields)
    path.write_text(json.dumps(document), encoding="utf-8")


def _texts(plugins_dir: Path | None, rows) -> list[str]:
    return [finding.report_text() for finding in verify_environments(plugins_dir, rows).findings]


# -- passing ------------------------------------------------------------------------


def test_environments_matching_the_store_and_this_image_pass(tmp_path: Path) -> None:
    _lay_layer(tmp_path, [_input(_TOOLBOX), _input(_OTHER)])
    _lay_server(tmp_path, _input(_SERVER))
    rows = [install_row(_TOOLBOX), install_row(_OTHER), install_row(_SERVER), install_row(_DESCRIPTOR)]

    verification = verify_environments(tmp_path, rows)

    assert verification.findings == []
    assert [status.label for status in verification.environments] == ["layer", "server tai42-fixture-srv"]
    assert verification.orphans == ()


def test_an_empty_store_with_no_layer_build_passes(tmp_path: Path) -> None:
    assert _texts(tmp_path, []) == []


def test_no_plugins_directory_with_no_packaged_plugin_passes() -> None:
    assert _texts(None, [install_row(_DESCRIPTOR)]) == []


# -- out of date ----------------------------------------------------------------------


def test_packaged_plugins_with_no_plugins_directory_need_it(tmp_path: Path) -> None:
    verification = verify_environments(None, [install_row(_TOOLBOX), install_row(_SERVER)])

    assert [finding.boot_text() for finding in verification.findings] == [
        "installed plugins need TAI_PLUGINS_DIR — refusing to start: 2 packaged plugins are recorded "
        "(tai42/srv, tai42/toolbox); set TAI_PLUGINS_DIR to the directory they were installed into"
    ]
    assert verification.environments == ()


def test_a_layer_with_no_current_build_is_out_of_date(tmp_path: Path) -> None:
    verification = verify_environments(tmp_path, [install_row(_TOOLBOX)])

    assert [finding.boot_text() for finding in verification.findings] == [
        "plugin environment is out of date — refusing to start: the plugins directory "
        f"{tmp_path} does not match the install store (no current build, missing tai42/toolbox 1.0.0). "
        "Run 'tai plugins rebuild' against this deployment, then start again"
    ]


def test_a_layer_holding_other_plugins_than_the_store_names_both(tmp_path: Path) -> None:
    _lay_layer(tmp_path, [_input(_TOOLBOX), _input(_OTHER, "1.0.0")])

    assert _texts(tmp_path, [install_row(_TOOLBOX), install_row(_OTHER)]) == [
        "plugin environment is out of date: the plugins directory "
        f"{tmp_path} does not match the install store (missing tai42/other 2.0.0, "
        "holds tai42/other 1.0.0 not recorded). Run 'tai plugins rebuild' against this deployment, then start again"
    ]


def test_a_current_build_without_its_record_is_incomplete(tmp_path: Path) -> None:
    _lay_layer(tmp_path, [_input(_TOOLBOX)], record=False)

    assert _texts(tmp_path, [install_row(_TOOLBOX)]) == [
        f"plugin environment has no build record {layer_dir(tmp_path).build('b1') / BUILD_RECORD}: "
        "its current build b1 is incomplete. Run 'tai plugins rebuild' against this deployment, then start again"
    ]


def _corrupt_record(build: Path) -> Path:
    path = build / BUILD_RECORD
    path.write_text("{truncated", encoding="utf-8")
    return path


def _assert_unreadable(text: str, subject: str, path: Path) -> None:
    assert text.startswith(f"{subject} has an unreadable build record {path}: Expecting property name"), text
    assert text.endswith(". Run 'tai plugins rebuild' against this deployment, then start again"), text


def test_a_layer_whose_current_record_is_unreadable_needs_a_rebuild(tmp_path: Path) -> None:
    path = _corrupt_record(_lay_layer(tmp_path, [_input(_TOOLBOX)]))

    verification = verify_environments(tmp_path, [install_row(_TOOLBOX)])

    [text] = [finding.report_text() for finding in verification.findings]
    _assert_unreadable(text, "plugin environment", path)
    assert verification.environments[0].record is None
    [boot] = [finding.boot_text() for finding in verification.findings]
    assert boot.startswith(f"plugin environment has an unreadable build record {path} — refusing to start: "), boot


def test_a_server_package_whose_current_record_is_unreadable_needs_a_rebuild(tmp_path: Path) -> None:
    _lay_layer(tmp_path, [])
    path = _corrupt_record(_lay_server(tmp_path, _input(_SERVER)))

    [text] = _texts(tmp_path, [install_row(_SERVER)])

    _assert_unreadable(text, "plugin environment of server package tai42-fixture-srv", path)


def test_a_server_directory_the_store_does_not_name_with_an_unreadable_record_needs_a_rebuild(tmp_path: Path) -> None:
    _lay_layer(tmp_path, [])
    path = _corrupt_record(_lay_server(tmp_path, _input(_SERVER)))

    verification = verify_environments(tmp_path, [])

    assert [orphan.label for orphan in verification.orphans] == ["server tai42-fixture-srv"]
    [text] = [finding.report_text() for finding in verification.findings]
    _assert_unreadable(text, "plugin environment of server package tai42-fixture-srv", path)


def test_a_server_package_with_no_environment_is_out_of_date(tmp_path: Path) -> None:
    _lay_layer(tmp_path, [])

    assert _texts(tmp_path, [install_row(_SERVER)]) == [
        "plugin environment of server package tai42-fixture-srv is out of date: the plugins directory "
        f"{tmp_path} does not match the install store (no current build, missing tai42/srv 1.0.0). "
        "Run 'tai plugins rebuild' against this deployment, then start again"
    ]


def test_a_server_directory_the_store_does_not_name_holding_a_current_build_is_out_of_date(tmp_path: Path) -> None:
    _lay_layer(tmp_path, [])
    _lay_server(tmp_path, _input(_SERVER))

    verification = verify_environments(tmp_path, [])

    assert [(orphan.label, orphan.environment) for orphan in verification.orphans] == [
        ("server tai42-fixture-srv", server_dir(tmp_path, "tai42-fixture-srv"))
    ]
    assert [finding.report_text() for finding in verification.findings] == [
        "plugin environment of server package tai42-fixture-srv is out of date: the plugins directory "
        f"{tmp_path} does not match the install store (holds tai42/srv 1.0.0 not recorded). "
        "Run 'tai plugins rebuild' against this deployment, then start again"
    ]


def test_a_server_directory_the_store_does_not_name_with_no_current_build_is_left_over_not_a_finding(
    tmp_path: Path,
) -> None:
    _lay_layer(tmp_path, [])
    server_dir(tmp_path, "tai42-fixture-srv").builds.mkdir(parents=True)

    verification = verify_environments(tmp_path, [])

    assert verification.findings == []
    assert [(orphan.label, orphan.finding) for orphan in verification.orphans] == [("server tai42-fixture-srv", None)]


# -- built for another interpreter ------------------------------------------------------


def test_a_layer_built_for_another_interpreter_needs_a_rebuild_against_this_image(tmp_path: Path) -> None:
    build = _lay_layer(tmp_path, [_input(_TOOLBOX)])
    _rewrite_record(build, python="3.0", platform="other-platform")

    assert _texts(tmp_path, [install_row(_TOOLBOX)]) == [
        f"plugin environment was built for Python 3.0 on other-platform but this image runs {_RUNNING}. "
        "Run 'tai plugins rebuild' against this image"
    ]


def test_a_server_venv_of_another_python_needs_a_rebuild_against_this_image(tmp_path: Path) -> None:
    _lay_layer(tmp_path, [])
    _lay_server(tmp_path, _input(_SERVER), venv_version="3.0.1")

    assert _texts(tmp_path, [install_row(_SERVER)]) == [
        "plugin environment of server package tai42-fixture-srv was built for Python 3.0 on "
        f"{sysconfig.get_platform()} but this image runs {_RUNNING}. Run 'tai plugins rebuild' against this image"
    ]


def test_a_server_build_without_its_interpreter_needs_a_rebuild(tmp_path: Path) -> None:
    _lay_layer(tmp_path, [])
    build = _lay_server(tmp_path, _input(_SERVER))
    (build / "bin" / "python").unlink()

    assert _texts(tmp_path, [install_row(_SERVER)]) == [
        f"plugin environment of server package tai42-fixture-srv has no interpreter at {build / 'bin' / 'python'}. "
        "Run 'tai plugins rebuild' against this image"
    ]


# -- the layer beside this image ---------------------------------------------------------


def test_a_layer_distribution_the_image_also_provides_conflicts_with_this_image(tmp_path: Path) -> None:
    build = _lay_layer(tmp_path, [_input(_TOOLBOX)])
    write_dist_info(layer_site_dirs(build)[0], "pytest", "0.0.1")

    (text,) = _texts(tmp_path, [install_row(_TOOLBOX)])

    assert text.startswith("plugin environment conflicts with this image: pytest 0.0.1 is in the plugin layer")
    assert text.endswith(f"and the image provides {pytest.__version__}. Run 'tai plugins rebuild' against this image")


def test_a_layer_requirement_the_image_no_longer_meets_is_inconsistent_with_this_image(tmp_path: Path) -> None:
    build = _lay_layer(tmp_path, [_input(_TOOLBOX)])
    write_dist_info(layer_site_dirs(build)[0], "tai42-fixture-dep", "1.0.0", requires=["tai42-fixture-absent>=1"])

    assert _texts(tmp_path, [install_row(_TOOLBOX)]) == [
        "plugin environment is inconsistent with this image: tai42-fixture-dep 1.0.0 requires "
        "tai42-fixture-absent>=1; neither the plugin layer nor the image provides tai42-fixture-absent. "
        "Run 'tai plugins rebuild' against this image"
    ]


# -- plugins this image's contract rejects ------------------------------------------------

# A contract range no running tai42-contract is inside, as a core upgrade leaves a store row.
_STRANDED = make_spec(name="stranded", package="tai42-fixture-stranded", contract=">=99,<100")


def _left_out(spec) -> Excluded:
    return Excluded(input=_input(spec), modules=("tai42_fixture_stranded.tools",), reason="its contract range")


def test_a_layer_holding_a_plugin_this_contract_rejects_is_out_of_date(tmp_path: Path) -> None:
    _lay_layer(tmp_path, [_input(_TOOLBOX), _input(_STRANDED)])

    verification = verify_environments(tmp_path, [install_row(_TOOLBOX), install_row(_STRANDED)])

    assert [finding.boot_text() for finding in verification.findings] == [
        "plugin environment is out of date — refusing to start: it holds tai42/stranded 1.0.0, which this "
        f"image's tai42-contract {running_contract_version()} does not support. "
        "Run 'tai plugins rebuild' against this image"
    ]


def test_a_layer_leaving_out_a_plugin_this_contract_supports_is_out_of_date(tmp_path: Path) -> None:
    _lay_layer(tmp_path, [], excluded=(_left_out(_TOOLBOX),))

    assert _texts(tmp_path, [install_row(_TOOLBOX)]) == [
        "plugin environment is out of date: it leaves out tai42/toolbox 1.0.0, which this image's "
        f"tai42-contract {running_contract_version()} supports. Run 'tai plugins rebuild' against this image"
    ]


def test_a_recorded_exclusion_counts_as_matching_the_store(tmp_path: Path) -> None:
    _lay_layer(tmp_path, [_input(_TOOLBOX)], excluded=(_left_out(_STRANDED),))

    assert _texts(tmp_path, [install_row(_TOOLBOX), install_row(_STRANDED)]) == []


def test_a_recorded_exclusion_the_store_no_longer_names_is_held_not_recorded(tmp_path: Path) -> None:
    _lay_layer(tmp_path, [_input(_TOOLBOX)], excluded=(_left_out(_STRANDED),))

    (text,) = _texts(tmp_path, [install_row(_TOOLBOX)])

    assert "(holds tai42/stranded 1.0.0 not recorded)" in text
