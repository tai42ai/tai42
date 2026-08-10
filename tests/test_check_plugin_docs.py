"""scripts/check_plugin_docs.py: the fleet docs gate rejects loudly and accepts
a valid tree. Driven by synthetic plugin fixtures, never the real (concurrently
authored) plugin docs, so the rejection path is deterministic now."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_GATE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_plugin_docs.py"
_spec = importlib.util.spec_from_file_location("check_plugin_docs", _GATE_PATH)
assert _spec is not None and _spec.loader is not None
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

_VALID_INDEX = b"""---
title: Example
description: An example plugin docs page.
---

# Example

Body text with an [in-set link](other.mdx).
"""

_OTHER_PAGE = b"""---
title: Other
description: A second page in the set.
---

More body text.
"""


def _make_plugin(root: Path, *, package: str = "tai42_example") -> Path:
    """A plugin root with the src/<package>/docs/ layout the gate discovers."""
    docs = root / "src" / package / "docs"
    docs.mkdir(parents=True)
    return root


def test_accepts_a_valid_docs_tree(tmp_path: Path):
    plugin = _make_plugin(tmp_path)
    docs = plugin / "src" / "tai42_example" / "docs"
    (docs / "index.mdx").write_bytes(_VALID_INDEX)
    (docs / "other.mdx").write_bytes(_OTHER_PAGE)
    (docs / "images").mkdir()
    (docs / "images" / "shot.png").write_bytes(b"\x89PNG\r\n")

    assert gate.main([str(plugin)]) == 0


def test_skips_a_plugin_without_docs(tmp_path: Path):
    plugin = tmp_path / "no-docs-plugin"
    (plugin / "src" / "tai42_nodocs").mkdir(parents=True)

    assert gate.main([str(plugin)]) == 0


def test_rejects_missing_index(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    plugin = _make_plugin(tmp_path)
    (plugin / "src" / "tai42_example" / "docs" / "guide.mdx").write_bytes(_VALID_INDEX)

    assert gate.main([str(plugin)]) == 1
    err = capsys.readouterr().err
    assert "FAILED" in err
    assert "index.mdx" in err


def test_rejects_bad_front_matter(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    plugin = _make_plugin(tmp_path)
    (plugin / "src" / "tai42_example" / "docs" / "index.mdx").write_bytes(
        b"# no front matter\n"
    )

    assert gate.main([str(plugin)]) == 1
    err = capsys.readouterr().err
    assert "FAILED" in err
    assert "front" in err.lower()


def test_rejects_non_raster_image(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    plugin = _make_plugin(tmp_path)
    docs = plugin / "src" / "tai42_example" / "docs"
    (docs / "index.mdx").write_bytes(
        _VALID_INDEX.replace(b"[in-set link](other.mdx)", b"text")
    )
    (docs / "images").mkdir()
    (docs / "images" / "diagram.svg").write_bytes(b"<svg></svg>")

    assert gate.main([str(plugin)]) == 1
    err = capsys.readouterr().err
    assert "FAILED" in err
    assert ".svg" in err


def test_rejects_disallowed_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    plugin = _make_plugin(tmp_path)
    docs = plugin / "src" / "tai42_example" / "docs"
    (docs / "index.mdx").write_bytes(
        _VALID_INDEX.replace(b"[in-set link](other.mdx)", b"text")
    )
    (docs / "notes.txt").write_bytes(b"stray file")

    assert gate.main([str(plugin)]) == 1
    err = capsys.readouterr().err
    assert "FAILED" in err
    assert "notes.txt" in err


def test_names_the_plugin_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    plugin = _make_plugin(tmp_path / "channel-example")
    (plugin / "src" / "tai42_example" / "docs" / "guide.mdx").write_bytes(_VALID_INDEX)

    assert gate.main([str(plugin)]) == 1
    assert "channel-example" in capsys.readouterr().err
