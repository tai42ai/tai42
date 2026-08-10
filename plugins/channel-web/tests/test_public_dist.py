"""Pin the packaged ``public/`` payload to what the page and asset doors require.

The doors read this manifest at request time: ``load_build`` refuses a build whose
entry or stylesheet is not integrity-listed, and the asset door serves ONLY names
the integrity map carries. These tests re-state those rules against the SHIPPED
bundle: the manifest is well-formed and matches the package version, every listed
file exists and still hashes to its declared SRI digest, no shipped file is missing
from the serve list, and the bundle reaches no foreign origin.

What this file does NOT gate is FRESHNESS. Re-hashing compares the committed bytes
to the committed manifest, and a source edit that was never rebuilt leaves both
untouched and consistent. CI's own check — rebuild ``public/`` and diff it against
the commit — is what catches that.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.resources
import json
import re
import tomllib
from pathlib import Path

_SHA384_RE = re.compile(r"^sha384-[A-Za-z0-9+/]{64}={0,2}$")
_EXPECTED_MANIFEST_KEYS = {"name", "version", "entry", "styles", "integrity"}
_MANIFEST_FILENAME = "public-manifest.json"
_PACKAGE_NAME = "tai42_channel_web"

# The page is standalone: it is served with no import map, so any one of these left
# as a bare specifier would be an unresolvable import and a blank page.
_MUST_BE_BUNDLED = (b'from"react"', b'from"react-dom"', b'from"@tai42/studio-sdk"')


def _public_root() -> Path:
    return Path(str(importlib.resources.files(_PACKAGE_NAME))) / "public"


def _manifest() -> dict:
    raw = (_public_root() / _MANIFEST_FILENAME).read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    return parsed


def _sha384(data: bytes) -> str:
    return "sha384-" + base64.b64encode(hashlib.sha384(data).digest()).decode("ascii")


def _pyproject_version() -> str:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    version = data["project"]["version"]
    assert isinstance(version, str)
    return version


def test_manifest_exists_and_has_exact_keys():
    assert set(_manifest()) == _EXPECTED_MANIFEST_KEYS


def test_manifest_name_is_the_package_name():
    assert _manifest()["name"] == _PACKAGE_NAME


def test_manifest_version_matches_pyproject():
    assert _manifest()["version"] == _pyproject_version()


def test_entry_is_an_integrity_key():
    manifest = _manifest()
    assert manifest["entry"] in manifest["integrity"]


def test_every_stylesheet_is_an_integrity_key():
    manifest = _manifest()
    assert manifest["styles"], "the page must link at least one stylesheet"
    for name in manifest["styles"]:
        assert name in manifest["integrity"]


def test_integrity_files_exist_and_hashes_match():
    # A file whose bytes and declared digest have drifted apart would be served with
    # SRI attributes the browser refuses — a blank page, not a degraded one.
    root = _public_root()
    integrity = _manifest()["integrity"]
    assert integrity, "integrity map must not be empty"
    for filename, declared in integrity.items():
        assert _SHA384_RE.match(declared), f"{filename} hash is not sha384-<base64>"
        asset = root / filename
        assert asset.is_file(), f"integrity lists {filename} but no such file exists"
        assert _sha384(asset.read_bytes()) == declared, f"{filename} sha384 mismatch"


def test_manifest_file_is_not_an_integrity_entry():
    assert _MANIFEST_FILENAME not in _manifest()["integrity"]


def test_shipped_assets_and_integrity_entries_match():
    # The integrity map is the asset door's serve list, so a shipped file missing
    # from it is a file the browser can never fetch. rglob so a file emitted into a
    # subdirectory cannot slip past the very check meant to catch it.
    root = _public_root()
    shipped = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    assert shipped - {_MANIFEST_FILENAME} == set(_manifest()["integrity"])


def test_bundle_is_self_contained():
    root = _public_root()
    js_files = [name for name in _manifest()["integrity"] if name.endswith(".js")]
    assert js_files, "the integrity map lists no JS files"
    for name in js_files:
        data = (root / name).read_bytes()
        for specifier in _MUST_BE_BUNDLED:
            assert specifier not in data, f"{name} imports {specifier!r} instead of bundling it"


def test_stylesheet_reaches_no_foreign_origin():
    # The page's CSP is default-src 'none' plus same-origin allowances, so an
    # off-origin stylesheet reference could only ever be a blocked request.
    root = _public_root()
    for name in _manifest()["styles"]:
        css = (root / name).read_text(encoding="utf-8")
        assert "url(//" not in css
        assert "url(http" not in css
        assert "@import url(" not in css
