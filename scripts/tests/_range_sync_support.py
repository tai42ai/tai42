"""Shared sample-tree builders + constants for the range_sync test modules."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from textwrap import dedent

_PENDING_REPIN_WINDOW = not (
    os.environ.get("GITHUB_HEAD_REF", "") and not os.environ["GITHUB_HEAD_REF"].startswith("release-please--")
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text).lstrip("\n"))


def _build_tree(root: Path) -> None:
    """A minimal but representative workspace: contract + kit cores, one plugin
    with extras / marker / version-less / [tool.uv.sources] refs, both yml copies."""
    _write(
        root / "pyproject.toml",
        """
        [tool.uv.workspace]
        members = ["core/*", "plugins/*"]

        [dependency-groups]
        dev = ["pytest>=8"]
        """,
    )
    _write(
        root / "core/contract/pyproject.toml",
        """
        [project]
        name = "tai42-contract"
        version = "0.3.0"
        dependencies = ["pydantic>=2.12"]
        """,
    )
    _write(
        root / "core/kit/pyproject.toml",
        """
        [project]
        name = "tai42-kit"
        version = "0.3.0"
        dependencies = [
            "tai42-contract>=0.2,<0.4",
            "httpx>=0.28",
        ]

        [project.optional-dependencies]
        redis = ["redis>=5"]

        [tool.uv.sources]
        tai42-contract = { workspace = true }
        """,
    )
    _write(
        root / "plugins/demo/pyproject.toml",
        """
        [project]
        name = "tai42-demo"
        version = "0.2.1"
        dependencies = [
            # patch-level floor must normalize to the minor floor
            "tai42-contract>=0.3.0,<0.4",
            # extras must be preserved verbatim
            "tai42-kit[llm,jq,redis]>=0.2,<0.4",
            # environment marker must be preserved
            "tai42-kit>=0.2,<0.4; python_version >= '3.13'",
            # version-less first-party ref must be left untouched
            "tai42-kit[curl]",
            "click>=8.3",
        ]

        [tool.uv.sources]
        tai42-contract = { workspace = true }
        tai42-kit = { workspace = true }
        """,
    )
    contract_yaml = """
        spec_version: 1
        package: tai42-demo
        version: 0.2.1
        contract: '>=0.2,<0.4'
        """
    _write(root / "plugins/demo/tai-plugin.yml", contract_yaml)
    _write(root / "plugins/demo/src/tai42_demo/tai-plugin.yml", contract_yaml)


def _kit_deps(root: Path) -> list[str]:
    with (root / "plugins/demo/pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["dependencies"]


def _build_major_tree(root: Path, *, pin: bool) -> None:
    """A workspace where kit's declared contract cap (``<2``) is crossed by the
    released contract major (2.0.0), so the derived range is ``>=2.0,<3``."""
    _write(root / "pyproject.toml", '[tool.uv.workspace]\nmembers = ["core/*"]\n')
    _write(
        root / "core/contract/pyproject.toml",
        """
        [project]
        name = "tai42-contract"
        version = "2.0.0"
        dependencies = []
        """,
    )
    pin_table = '\n[tool.range-sync]\npinned = ["tai42-contract"]\n' if pin else ""
    _write(
        root / "core/kit/pyproject.toml",
        f"""
        [project]
        name = "tai42-kit"
        version = "2.0.0"
        dependencies = ["tai42-contract>=1.2,<2"]
        {pin_table}""",
    )


def _build_descriptor_pin_tree(root: Path) -> None:
    """A plugin that pins ``tai42-contract`` to ``>=1.2,<2`` while the released
    contract is 2.0.0 (global derived range ``>=2.0,<3``). Both descriptor
    copies start consistent with the pin."""
    _write(root / "pyproject.toml", '[tool.uv.workspace]\nmembers = ["core/*", "plugins/*"]\n')
    _write(
        root / "core/contract/pyproject.toml",
        """
        [project]
        name = "tai42-contract"
        version = "2.0.0"
        dependencies = []
        """,
    )
    _write(
        root / "plugins/demo/pyproject.toml",
        """
        [project]
        name = "tai42-demo"
        version = "0.1.0"
        dependencies = ["tai42-contract>=1.2,<2"]

        [tool.range-sync]
        pinned = ["tai42-contract"]
        """,
    )
    contract_yaml = """
        spec_version: 1
        package: tai42-demo
        version: 0.1.0
        contract: '>=1.2,<2'
        """
    _write(root / "plugins/demo/tai-plugin.yml", contract_yaml)
    _write(root / "plugins/demo/src/tai42_demo/tai-plugin.yml", contract_yaml)


_DESC_YMLS = ("plugins/demo/tai-plugin.yml", "plugins/demo/src/tai42_demo/tai-plugin.yml")


def _build_underivable_pin_tree(root: Path) -> None:
    """A plugin that pins ``tai42-contract`` with a spec that has NO derivable
    floor (bare ``>1.2``) while the released contract is 2.0.0 (global derived
    range ``>=2.0,<3``). The pin preserves the dep, but no floor can be derived
    to re-range the descriptor — so both descriptor copies must be left
    untouched entirely, never forced to the global range the pin refuses."""
    _write(root / "pyproject.toml", '[tool.uv.workspace]\nmembers = ["core/*", "plugins/*"]\n')
    _write(
        root / "core/contract/pyproject.toml",
        """
        [project]
        name = "tai42-contract"
        version = "2.0.0"
        dependencies = []
        """,
    )
    _write(
        root / "plugins/demo/pyproject.toml",
        """
        [project]
        name = "tai42-demo"
        version = "0.1.0"
        dependencies = ["tai42-contract>1.2"]

        [tool.range-sync]
        pinned = ["tai42-contract"]
        """,
    )
    contract_yaml = """
        spec_version: 1
        package: tai42-demo
        version: 0.1.0
        contract: '>=1.2,<2'
        """
    _write(root / "plugins/demo/tai-plugin.yml", contract_yaml)
    _write(root / "plugins/demo/src/tai42_demo/tai-plugin.yml", contract_yaml)


def _build_descriptor_only_tree(root: Path) -> None:
    """A workspace with a DESCRIPTOR-ONLY connector: a ``plugins/*`` dir carrying
    a ``tai-plugin.yml`` but NO ``pyproject.toml`` (listed under the root's
    ``[tool.uv.workspace].exclude``). It ships no package, so it is never a member
    — yet its ``contract:`` still tracks the global released contract range. Here
    the released contract is 2.0.0 (global derived ``>=2.0,<3``) while the
    connector advertises a stale ``>=1.1,<2``."""
    _write(
        root / "pyproject.toml",
        """
        [tool.uv.workspace]
        members = ["core/*", "plugins/*"]
        exclude = ["plugins/connector-demo"]
        """,
    )
    _write(
        root / "core/contract/pyproject.toml",
        """
        [project]
        name = "tai42-contract"
        version = "2.0.0"
        dependencies = []
        """,
    )
    _write(
        root / "plugins/connector-demo/tai-plugin.yml",
        """
        spec_version: 1
        namespace: tai42
        name: connector-demo
        version: 1.0.0
        contract: '>=1.1,<2'
        """,
    )


_CONNECTOR_YML = "plugins/connector-demo/tai-plugin.yml"


def _build_scaffold_tree(root: Path) -> None:
    """A workspace whose CLI member ships plugin SCAFFOLDS as package data under
    its ``src/`` tree: one scaffold declaring a ``contract:`` range, one declaring
    none. Beside them sit copies that are NOT shipped — a build output and a test
    fixture, under the CLI and under a packaged plugin. The released contract is
    2.0.0 (global derived ``>=2.0,<3``) while every descriptor on disk advertises a
    stale ``>=1.1,<2``."""
    _write(
        root / "pyproject.toml",
        """
        [tool.uv.workspace]
        members = ["core/*", "plugins/*"]
        """,
    )
    _write(
        root / "core/contract/pyproject.toml",
        """
        [project]
        name = "tai42-contract"
        version = "2.0.0"
        dependencies = []
        """,
    )
    _write(
        root / "core/cli/pyproject.toml",
        """
        [project]
        name = "tai42-cli"
        version = "2.0.0"
        dependencies = ["tai42-contract>=2.0,<3"]
        """,
    )
    _write(
        root / "plugins/demo/pyproject.toml",
        """
        [project]
        name = "tai42-demo"
        version = "1.0.0"
        dependencies = ["tai42-contract>=2.0,<3"]
        """,
    )
    stale_yaml = """
        spec_version: 1
        namespace: acme
        name: demo
        version: 1.0.0
        contract: '>=1.1,<2'
        """
    _write(root / "core/cli/src/tai42_cli/templates/connector/tai-plugin.yml", stale_yaml)
    _write(
        root / "core/cli/src/tai42_cli/templates/server/tai-plugin.yml",
        """
        spec_version: 1
        namespace: acme
        name: server
        version: 1.0.0
        """,
    )
    _write(root / "core/cli/build/lib/tai42_cli/templates/connector/tai-plugin.yml", stale_yaml)
    _write(root / "core/cli/tests/fixtures/tai-plugin.yml", stale_yaml)
    _write(root / "plugins/demo/tai-plugin.yml", stale_yaml)
    _write(root / "plugins/demo/src/tai42_demo/tai-plugin.yml", stale_yaml)
    _write(root / "plugins/demo/build/lib/tai42_demo/tai-plugin.yml", stale_yaml)


_SCAFFOLD_YML = "core/cli/src/tai42_cli/templates/connector/tai-plugin.yml"
_CONTRACTLESS_SCAFFOLD_YML = "core/cli/src/tai42_cli/templates/server/tai-plugin.yml"
_UNSHIPPED_YMLS = (
    "core/cli/build/lib/tai42_cli/templates/connector/tai-plugin.yml",
    "core/cli/tests/fixtures/tai-plugin.yml",
    "plugins/demo/build/lib/tai42_demo/tai-plugin.yml",
)
