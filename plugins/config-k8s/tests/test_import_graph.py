"""Import-graph guard for the shipped package.

Two walks share one allowlist and assert every import root reachable from
``tai42_config_k8s`` is allowlisted. The runtime walk imports the package and
every submodule in a fresh subprocess, then inspects ``sys.modules`` (subprocess
isolation keeps sibling-test/conftest imports out of the observed closure). The
static walk parses every source file for import roots at ANY nesting depth,
catching function/class/``TYPE_CHECKING`` imports the runtime walk never executes.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

# The shipped package and the public first-party packages it may import.
PACKAGE = "tai42_config_k8s"
ALLOWED_FIRST_PARTY = frozenset({PACKAGE, "tai42_contract", "tai42_kit"})

# Third-party roots of the shipped graph: declared runtime deps plus their closure.
# Only widen for a genuine new dependency, never to make the test pass.
ALLOWED_THIRD_PARTY = frozenset(
    {
        "annotated_types",
        "attr",
        "attrs",
        "dotenv",
        "idna",
        "jsonschema",
        "jsonschema_specifications",
        "kubernetes",
        "pyaml_env",
        "pydantic",
        "pydantic_core",
        "pydantic_settings",
        "referencing",
        "rpds",
        "ruamel",
        "typing_extensions",
        "typing_inspection",
        "yaml",
    }
)

# Interpreter/compiler/virtual-env roots that land in ``sys.modules`` as ambient
# side effects. Not dependencies; build/platform-specific names are matched by shape.
_ARTIFACT_ROOTS = frozenset({"__main__", "__mp_main__", "cython_runtime", "_virtualenv"})


def _is_runtime_artifact(root: str) -> bool:
    return root in _ARTIFACT_ROOTS or root.endswith("__mypyc") or root.startswith(("_cython_", "_sysconfigdata"))


def _allowed(root: str) -> bool:
    return (
        root in sys.stdlib_module_names
        or root in ALLOWED_FIRST_PARTY
        or root in ALLOWED_THIRD_PARTY
        or _is_runtime_artifact(root)
    )


# Subprocess program: bind a stub app to ``tai42_app`` (plugin modules register
# through it at import time), import the package and every submodule, then print
# each imported root not on the allowlist. A failed import exits non-zero.
_CHILD_PROGRAM = f"""
import importlib
import pkgutil
import sys

from tai42_contract.app import tai42_app

PACKAGE = {PACKAGE!r}
ALLOWED_FIRST_PARTY = {set(ALLOWED_FIRST_PARTY)!r}
ALLOWED_THIRD_PARTY = {set(ALLOWED_THIRD_PARTY)!r}
_ARTIFACT_ROOTS = {set(_ARTIFACT_ROOTS)!r}


def _is_runtime_artifact(root):
    return (
        root in _ARTIFACT_ROOTS
        or root.endswith("__mypyc")
        or root.startswith(("_cython_", "_sysconfigdata"))
    )


def _allowed(root):
    return (
        root in sys.stdlib_module_names
        or root in ALLOWED_FIRST_PARTY
        or root in ALLOWED_THIRD_PARTY
        or _is_runtime_artifact(root)
    )


class _StubApp:
    # Accepts every registration seam: attribute access yields another stub, a
    # single-callable call acts as a bare decorator, any other call as a factory.
    def __getattr__(self, name):
        return _StubApp()

    def __call__(self, *args, **kwargs):
        if len(args) == 1 and not kwargs and callable(args[0]):
            return args[0]
        return _StubApp()


tai42_app.bind(_StubApp())

package = importlib.import_module(PACKAGE)
for module_info in pkgutil.walk_packages(package.__path__, prefix=package.__name__ + "."):
    importlib.import_module(module_info.name)

offenders = sorted(
    root for root in {{name.partition(".")[0] for name in sys.modules}} if not _allowed(root)
)
for name in offenders:
    print(name)
"""


def _source_root() -> Path:
    root = Path(__file__).resolve().parents[1].joinpath("src", *PACKAGE.split("."))
    assert root.is_dir(), f"shipped package source not found at {root}"
    return root


def _static_import_roots() -> dict[str, set[str]]:
    """Map each import root in the shipped sources to the files that import it.

    Walks the full AST, so nested imports are collected like module-level ones.
    Relative imports address the package itself and carry no root to check.
    """
    roots: dict[str, set[str]] = {}
    source_root = _source_root()
    paths = sorted(source_root.rglob("*.py"))
    assert paths, f"no source files found under {source_root}"
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.level == 0 and node.module else []
            else:
                continue
            for name in names:
                roots.setdefault(name.partition(".")[0], set()).add(str(path.relative_to(source_root)))
    return roots


def test_shipped_package_imports_only_allowlisted_roots() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_PROGRAM],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"importing the shipped tai42_config_k8s graph failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    offenders = [line for line in result.stdout.splitlines() if line]
    assert offenders == [], f"non-allowlisted roots in the tai42_config_k8s module graph: {offenders}"


def test_shipped_sources_name_only_allowlisted_roots() -> None:
    offenders = {root: sorted(files) for root, files in _static_import_roots().items() if not _allowed(root)}
    assert offenders == {}, f"non-allowlisted import roots in the tai42_config_k8s sources: {offenders}"
