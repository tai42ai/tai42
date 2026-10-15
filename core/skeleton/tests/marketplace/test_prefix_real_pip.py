"""A real-pip proof that the ``--constraint`` freeze is what keeps a prefix install
from mutating the environment.

Unlike the unit tests (which assert the argv and the freeze file), this test runs a
REAL ``pip install --prefix`` against a throwaway venv, offline, from a forged
wheelhouse — so the never-silent-error claim is proven end to end on the tool the
installer actually shells:

  * WITHOUT the constraint, pip resolves the plugin's ``dep<2`` by UNINSTALLING the
    venv's ``dep==2.0`` and landing ``dep==1.0`` in the prefix — silently breaking
    the environment for any process that does not have the prefix on ``sys.path``.
  * WITH the constraint (the venv's installed versions, exactly what
    ``environment_constraints`` emits), pip REFUSES the conflicting closure loudly
    before writing anything: a non-zero exit naming the ``dep==2.0`` constraint, the
    venv keeps ``dep==2.0``, and the prefix stays empty.

POSIX-only, matching the ecosystem's darwin/linux target (the venv's ``bin/python``).
"""

from __future__ import annotations

import base64
import hashlib
import re
import subprocess
import sys
import zipfile
from pathlib import Path

from packaging.utils import canonicalize_name


def _record_hash(data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
    return f"sha256={digest}"


def _make_wheel(wheelhouse: Path, name: str, version: str, *, requires: list[str] | None = None) -> None:
    """Forge a minimal, installable pure-python wheel into ``wheelhouse``."""
    dist_info = f"{name}-{version}.dist-info"
    metadata = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    metadata += [f"Requires-Dist: {req}" for req in requires or []]
    files: dict[str, bytes] = {
        f"{name}/__init__.py": b"VALUE = 1\n",
        f"{dist_info}/METADATA": ("\n".join(metadata) + "\n").encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = [f"{path},{_record_hash(data)},{len(data)}" for path, data in files.items()]
    record.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/RECORD"] = ("\n".join(record) + "\n").encode()
    with zipfile.ZipFile(wheelhouse / f"{name}-{version}-py3-none-any.whl", "w") as zf:
        for path, data in files.items():
            zf.writestr(path, data)


def _make_env_venv(venv_dir: Path, wheelhouse: Path) -> Path:
    """Create a venv and install ``dep==2.0`` into it; return its python executable."""
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True, capture_output=True)
    venvpy = venv_dir / "bin" / "python"
    subprocess.run(
        [str(venvpy), "-m", "pip", "install", "--no-input", "--no-index", "--find-links", str(wheelhouse), "dep==2.0"],
        check=True,
        capture_output=True,
    )
    return venvpy


def _purelib(venvpy: Path, base: Path | None = None) -> Path:
    """The venv's own ``purelib`` (``base=None``), or the ``--prefix`` tree's purelib,
    as the venv's interpreter computes it — never this runner's sysconfig scheme."""
    if base is None:
        code = "import sysconfig; print(sysconfig.get_path('purelib'))"
    else:
        code = (
            f"import sysconfig; v={{'base': {str(base)!r}, 'platbase': {str(base)!r}}}; "
            "print(sysconfig.get_paths(vars=v)['purelib'])"
        )
    out = subprocess.run([str(venvpy), "-c", code], check=True, capture_output=True, text=True).stdout.strip()
    return Path(out)


def _version_at(site: Path, name: str) -> str | None:
    """The installed version of ``name`` under ``site`` on disk, or ``None``."""
    import importlib.metadata as im

    target = canonicalize_name(name)
    for dist in im.distributions(path=[str(site)]):
        if dist.name and canonicalize_name(dist.name) == target:
            return dist.version
    return None


def _prefix_install(
    venvpy: Path, prefix: Path, wheelhouse: Path, *, constraint: Path | None
) -> subprocess.CompletedProcess[str]:
    argv = [
        str(venvpy),
        "-m",
        "pip",
        "install",
        "--no-input",
        "--disable-pip-version-check",
        "--prefix",
        str(prefix),
        "--no-index",
        "--find-links",
        str(wheelhouse),
    ]
    if constraint is not None:
        argv += ["--constraint", str(constraint)]
    argv.append("plugin==1.0")
    return subprocess.run(argv, capture_output=True, text=True)


def test_prefix_install_without_constraint_silently_mutates_the_env(tmp_path: Path) -> None:
    # RED-FIRST: today's behaviour (no constraint) — pip resolves plugin's dep<2 by
    # uninstalling the venv's dep==2.0 and landing dep==1.0 in the prefix. The env is
    # silently broken for any process without the prefix on sys.path.
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _make_wheel(wheelhouse, "dep", "2.0")
    _make_wheel(wheelhouse, "dep", "1.0")
    _make_wheel(wheelhouse, "plugin", "1.0", requires=["dep<2"])

    venvpy = _make_env_venv(tmp_path / "venv", wheelhouse)
    assert _version_at(_purelib(venvpy), "dep") == "2.0"

    prefix = tmp_path / "prefix"
    result = _prefix_install(venvpy, prefix, wheelhouse, constraint=None)

    assert result.returncode == 0, result.stdout + result.stderr
    # The env lost dep 2.0; dep 1.0 and plugin landed in the prefix.
    assert _version_at(_purelib(venvpy), "dep") != "2.0"
    assert _version_at(_purelib(venvpy, base=prefix), "dep") == "1.0"
    assert _version_at(_purelib(venvpy, base=prefix), "plugin") == "1.0"


def test_prefix_install_with_constraint_refuses_loudly_and_keeps_the_env(tmp_path: Path) -> None:
    # The fix: the venv's installed versions as a --constraint file (exactly what
    # environment_constraints emits). pip refuses the conflicting closure loudly
    # BEFORE writing — the venv keeps dep 2.0 and the prefix stays empty.
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _make_wheel(wheelhouse, "dep", "2.0")
    _make_wheel(wheelhouse, "dep", "1.0")
    _make_wheel(wheelhouse, "plugin", "1.0", requires=["dep<2"])

    venvpy = _make_env_venv(tmp_path / "venv", wheelhouse)
    venv_site = _purelib(venvpy)
    assert _version_at(venv_site, "dep") == "2.0"

    # The freeze: every env distribution as name==version — the dep==2.0 line is the
    # one that collides with the plugin's dep<2.
    import importlib.metadata as im

    freeze = sorted(f"{d.name}=={d.version}" for d in im.distributions(path=[str(venv_site)]) if d.name and d.version)
    constraint = tmp_path / "environment-constraints.txt"
    constraint.write_text("".join(f"{line}\n" for line in freeze), encoding="utf-8")

    prefix = tmp_path / "prefix"
    result = _prefix_install(venvpy, prefix, wheelhouse, constraint=constraint)

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    # pip names the constraint that made the closure impossible (phrasing varies by
    # pip version — both the "(constraint)" and "(from constraint)" forms appear).
    assert re.search(r"constraint.*dep==2\.0|dep==2\.0.*constraint", output, re.IGNORECASE | re.DOTALL), output
    # The env keeps dep 2.0; nothing landed in the prefix.
    assert _version_at(venv_site, "dep") == "2.0"
    prefix_site = _purelib(venvpy, base=prefix)
    assert _version_at(prefix_site, "dep") is None
    assert _version_at(prefix_site, "plugin") is None
