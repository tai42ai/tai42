#!/usr/bin/env python3
"""Fleet docs gate: validate every first-party plugin's in-package ``docs/``
tree against the canonical contract, failing loud on the first plugin whose
tree violates it.

Discovery is data-driven, never a hardcoded plugin list that silently rots: the
workspace members come from the root ``pyproject.toml``
``[tool.uv.workspace].members`` globs (the same source ``tests/test_fleet.py``
enumerates), filtered to ``plugins/*``; within each plugin the shipped ``docs/``
tree is located under ``src/`` — covering both the flat ``src/<pkg>/docs`` and
the shared-namespace ``src/tai42_connector/<name>/docs`` layouts. A plugin that
ships no ``docs/`` tree yet is skipped cleanly: docs land across the fleet
incrementally and the gate goes green once every present tree is valid.

Validation is kit's :func:`tai42_kit.plugins.validate_docs` — ONE implementation
of the rules, three enforcement points (this CI gate, marketplace ingest, the
docs-site generator); never a mirrored copy. Every monorepo plugin is the
``tai42`` namespace, so ``first_party=True``.

CLI:
  check_plugin_docs.py            # enumerate the workspace, gate every plugin
  check_plugin_docs.py DIR ...    # gate exactly these plugin roots
Exit 0 = every present docs tree is valid (or none ship yet); exit 1 = at least
one violation, each reported with the plugin, the docs path, and the reason.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import tomllib
from tai42_kit.plugins import PLUGIN_DOCS_DIRNAME, PluginDocsError, validate_docs


def _repo_root() -> Path:
    """The repo root is the parent of this script's ``scripts/`` directory."""
    return Path(__file__).resolve().parent.parent


def _rel(path: Path, root: Path) -> str:
    """``path`` relative to ``root`` when it lies under it (the enumerated case),
    else the path as given (an out-of-tree dir passed explicitly, e.g. a test
    fixture)."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _plugin_dirs(root: Path) -> list[Path]:
    members = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    globs = members["tool"]["uv"]["workspace"]["members"]
    dirs: list[Path] = []
    for pattern in globs:
        for hit in sorted(root.glob(pattern)):
            if not (hit.is_dir() and (hit / "pyproject.toml").is_file()):
                continue
            if hit.relative_to(root).parts[0] == "plugins":
                dirs.append(hit)
    return dirs


def _docs_dirs(plugin_dir: Path) -> list[Path]:
    """The plugin's in-package ``docs/`` roots (a directory named ``docs`` sitting
    inside its import package under ``src/``). Normally zero or one; a ``docs``
    nested inside another ``docs`` is excluded so only the package-level tree is
    validated as a set."""
    src = plugin_dir / "src"
    if not src.is_dir():
        return []
    hits = [d for d in src.rglob(PLUGIN_DOCS_DIRNAME) if d.is_dir()]
    return sorted(
        d for d in hits if PLUGIN_DOCS_DIRNAME not in d.relative_to(src).parts[:-1]
    )


def _read_docs(docs_dir: Path) -> dict[str, bytes]:
    """The docs tree as ``validate_docs`` expects it: ``docs/``-prefixed relative
    keys mapped to raw bytes."""
    files: dict[str, bytes] = {}
    for path in sorted(docs_dir.rglob("*")):
        if path.is_file():
            key = f"{PLUGIN_DOCS_DIRNAME}/{path.relative_to(docs_dir).as_posix()}"
            files[key] = path.read_bytes()
    return files


def _check_plugin(plugin_dir: Path, root: Path) -> list[str]:
    docs_dirs = _docs_dirs(plugin_dir)
    if not docs_dirs:
        print(f"skip {_rel(plugin_dir, root)}: no {PLUGIN_DOCS_DIRNAME}/ tree yet")
        return []
    errors: list[str] = []
    for docs_dir in docs_dirs:
        rel = _rel(docs_dir, root)
        files = _read_docs(docs_dir)
        try:
            validate_docs(files, first_party=True)
        except PluginDocsError as exc:
            errors.append(f"{plugin_dir.name}: {rel}: {exc}")
        else:
            print(f"ok   {rel} ({len(files)} file(s))")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "plugin_dir",
        nargs="*",
        type=Path,
        help="plugin root(s) to gate; default: every plugins/* workspace member",
    )
    args = parser.parse_args(argv)
    root = _repo_root()

    plugin_dirs = (
        [d.resolve() for d in args.plugin_dir]
        if args.plugin_dir
        else _plugin_dirs(root)
    )
    errors: list[str] = []
    for plugin_dir in plugin_dirs:
        errors.extend(_check_plugin(plugin_dir, root))

    if errors:
        print("\nplugin docs gate FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print("plugin docs gate: all present docs trees are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
