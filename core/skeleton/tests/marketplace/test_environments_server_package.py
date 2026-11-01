"""The distribution a server build exists for, found by its normalized name and read without importing it."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from tests._fakes.plugin_environment_dists import install_fake_server_dist, write_venv_config

from tai42_skeleton.marketplace.environments.server_package import server_build_distribution
from tai42_skeleton.marketplace.errors import PluginsDirError


def test_the_server_build_s_distribution_is_found_by_any_spelling(tmp_path: Path) -> None:
    build = tmp_path / "build"
    install_fake_server_dist(build, "tai42-srv", "1.0.0")

    distribution = server_build_distribution(build, "Tai42_Srv")

    assert distribution.metadata["Name"] == "tai42-srv"


def test_a_server_build_without_its_package_raises_naming_the_build_and_the_remedy(tmp_path: Path) -> None:
    build = tmp_path / "build"
    write_venv_config(build)

    with pytest.raises(
        PluginsDirError,
        match=(
            rf"^server build {re.escape(str(build))} holds no distribution tai42-srv; "
            r"run 'tai plugins rebuild' against this deployment$"
        ),
    ):
        server_build_distribution(build, "tai42-srv")
