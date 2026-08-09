"""Forge real plugin artifacts from the checked-in fixture sources and serve
them over a thread-hosted package index.

Three pieces power the marketplace area's honest install path:

* :func:`build_fixture_wheel` copies a fixture source tree, stamps a version
  into its ``pyproject.toml`` and both ``tai-plugin.yml`` copies, builds a real
  wheel with ``uv build``, and carries the stamped root ``tai-plugin.yml`` on the
  built artifact — the inline spec the registry's admin-seed route requires.
* :func:`build_fixture_source_tarball` builds the github-sourced counterpart: a
  gzipped tar of the version-stamped source tree, shaped like a GitHub tag
  tarball (a top-level prefix directory carrying the whole source, both
  ``tai-plugin.yml`` copies present and byte-identical). It, too, carries the
  stamped root spec: the per-tag ``tai-plugin.yml`` the github contents surface
  serves.
* :class:`FixturePackageIndex` serves those artifacts: the PEP 503 ``/simple/``
  surface pip resolves wheels from, the PyPI JSON subset the registry's validator
  reads (its ``description`` carries a table-bearing README the detail page
  renders), and a github-shaped subset (a repo-existence probe, the contents API
  serving each tag's stamped spec, and the README endpoint) the registry's
  seed and webhook-ingest legs read.

The index is thread-hosted in the pytest/runner process like the other net
fixtures; it never mocks the system under test — it serves genuine artifact
bytes the real ingest pipeline downloads and validates.
"""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from packaging.version import Version

from tai42_e2e._threaded import ThreadedServer
from tai42_e2e.ports import allocate_port

_PLUGIN_SPEC_FILENAME = "tai-plugin.yml"

# The README markdown every pypi-sourced fixture serves as its PyPI ``description``.
# It carries a GFM table (and a strikethrough) so the marketplace detail page's
# Readme tab renders a real ``<table>`` — the render the browser leg asserts on.
README_MARKDOWN = (
    "# TAI42 Toolbox\n"
    "\n"
    "| Option | Type | Default |\n"
    "| --- | --- | --- |\n"
    "| `retries` | int | 3 |\n"
    "| `timeout` | float | 1.5 |\n"
    "\n"
    "~~Deprecated~~ superseded by `retries`.\n"
)


@dataclass(frozen=True)
class BuiltWheel:
    """A forged wheel: its pip distribution name, stamped version, on-disk path,
    the sha256 of the wheel bytes, and the stamped root ``tai-plugin.yml`` text
    (the inline spec the admin-seed route registers this version from)."""

    project: str
    version: str
    path: Path
    sha256: str
    plugin_yml: str


@dataclass(frozen=True)
class BuiltTarball:
    """A forged source tarball: its pip distribution name, stamped version,
    on-disk path, the sha256 of the tarball bytes, and the stamped root
    ``tai-plugin.yml`` text (the per-tag spec the github contents surface serves
    for this version)."""

    project: str
    version: str
    path: Path
    sha256: str
    plugin_yml: str


def _normalize_project(name: str) -> str:
    """PEP 503-normalize a project name (lowercase, runs of ``-_.`` to one ``-``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _project_name(pyproject: Path) -> str:
    """Read the ``[project].name`` from a ``pyproject.toml``."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    name = data.get("project", {}).get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeError(f"{pyproject} has no [project].name")
    return name


def _stamp(path: Path, pattern: str, replacement: str) -> None:
    """Rewrite the first line of ``path`` matching ``pattern`` to ``replacement``.

    A pattern that matches nothing raises — a silent no-op stamp would ship the
    checked-in ``0.0.0`` and fail ingest's wheel-version-vs-listing crosscheck
    confusingly later."""
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(pattern, lambda _m: replacement, text, count=1, flags=re.MULTILINE)
    if count == 0:
        raise RuntimeError(f"no version stamp target ({pattern!r}) found in {path}")
    path.write_text(new_text, encoding="utf-8")


def _prepare_stamped_tree(
    source_dir: Path,
    version: str,
    out_dir: Path,
    *,
    contract_range: str | None = None,
    requires_dist_range: str | None = None,
) -> tuple[Path, str]:
    """Copy ``source_dir`` into a fresh temp dir under ``out_dir`` and stamp
    ``version`` into the copy's ``pyproject.toml`` and every ``tai-plugin.yml``.

    Never mutates the checked-in source. Returns the copied tree root and the
    stamped root ``tai-plugin.yml`` text — the inline spec every seed entry
    carries and the per-tag spec the github contents surface serves. Every
    checked-in fixture yml declares ``version: 0.0.0``; the stamped text is the
    only copy carrying the real version, so an unstamped spec would fail the
    registry's version-vs-listing crosscheck.

    ``contract_range`` additionally stamps the declared ``contract:`` range into
    every ``tai-plugin.yml`` copy, and the ``tai42-contract`` dependency
    specifier in ``pyproject.toml`` is stamped to ``requires_dist_range`` (which
    defaults to ``contract_range``, keeping the artifact's spec and its built
    ``Requires-Dist`` metadata in lockstep). Passing a DIFFERENT
    ``requires_dist_range`` forges a deliberately mismatched artifact — the
    shape the registry's ingest lockstep gate must reject."""
    work = Path(tempfile.mkdtemp(dir=out_dir))
    tree = work / source_dir.name
    shutil.copytree(source_dir, tree)
    _stamp(tree / "pyproject.toml", r'^version = "[^"]*"', f'version = "{version}"')
    specs = sorted(tree.rglob(_PLUGIN_SPEC_FILENAME))
    if not specs:
        raise RuntimeError(f"no {_PLUGIN_SPEC_FILENAME} to stamp under {tree}")
    for spec in specs:
        _stamp(spec, r"^version: .*$", f"version: {version}")
    if contract_range is not None:
        for spec in specs:
            _stamp(spec, r"^contract: .*$", f'contract: "{contract_range}"')
    dependency_range = requires_dist_range if requires_dist_range is not None else contract_range
    if dependency_range is not None:
        _stamp(tree / "pyproject.toml", r'^\s*"tai42-contract[^"]*",$', f'    "tai42-contract{dependency_range}",')
    root_spec = tree / _PLUGIN_SPEC_FILENAME
    if not root_spec.exists():
        raise RuntimeError(f"no root {_PLUGIN_SPEC_FILENAME} under {tree}")
    return tree, root_spec.read_text(encoding="utf-8")


def build_fixture_wheel(
    source_dir: Path,
    version: str,
    out_dir: Path,
    *,
    contract_range: str | None = None,
    requires_dist_range: str | None = None,
) -> BuiltWheel:
    """Forge a real wheel from a fixture source tree at ``version``.

    Copies the tree, stamps the version into ``pyproject.toml`` and both
    ``tai-plugin.yml`` copies, then builds with ``uv build --wheel``. A build
    failure raises with the captured stderr; a missing ``uv`` raises loudly with
    that hint (``uv`` is the ecosystem toolchain, present locally and in CI).
    ``contract_range`` / ``requires_dist_range`` stamp the declared contract
    range and the built ``Requires-Dist`` specifier (see
    :func:`_prepare_stamped_tree`)."""
    tree, plugin_yml = _prepare_stamped_tree(
        source_dir, version, out_dir, contract_range=contract_range, requires_dist_range=requires_dist_range
    )
    try:
        proc = subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(out_dir), str(tree)],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("uv is required to build fixture wheels but was not found on PATH") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"uv build --wheel failed for {source_dir.name} {version}:\n{proc.stderr}")
    project = _project_name(tree / "pyproject.toml")
    dist = _normalize_project(project).replace("-", "_")
    matches = sorted(out_dir.glob(f"{dist}-{version}-*.whl"))
    if not matches:
        raise RuntimeError(f"uv build produced no wheel for {dist} {version} in {out_dir}")
    wheel_path = matches[-1]
    sha256 = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    return BuiltWheel(project=project, version=version, path=wheel_path, sha256=sha256, plugin_yml=plugin_yml)


def build_fixture_source_tarball(
    source_dir: Path,
    version: str,
    out_dir: Path,
    *,
    contract_range: str | None = None,
    requires_dist_range: str | None = None,
) -> BuiltTarball:
    """Forge a github-tag-shaped source tarball from a fixture source tree.

    Copies and version-stamps the tree exactly as the wheel forge does, then
    writes a gzipped tar whose members sit under a single top-level prefix
    directory carrying the whole source — the root ``tai-plugin.yml`` and the
    ``src/<pkg>/`` copy both present and byte-identical. The tarball is a
    realistic github-source artifact for the delta fixture; the version the
    github surface actually serves is the stamped root spec carried on the
    returned :class:`BuiltTarball`. ``contract_range`` / ``requires_dist_range``
    stamp the declared contract range and the ``tai42-contract`` dependency
    specifier (see :func:`_prepare_stamped_tree`)."""
    tree, plugin_yml = _prepare_stamped_tree(
        source_dir, version, out_dir, contract_range=contract_range, requires_dist_range=requires_dist_range
    )
    project = _project_name(tree / "pyproject.toml")
    prefix = f"{_normalize_project(project)}-{version}"
    tar_path = out_dir / f"{prefix}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for member in sorted(p for p in tree.rglob("*") if p.is_file()):
            arcname = f"{prefix}/{member.relative_to(tree).as_posix()}"
            tar.add(member, arcname=arcname)
    sha256 = hashlib.sha256(tar_path.read_bytes()).hexdigest()
    return BuiltTarball(project=project, version=version, path=tar_path, sha256=sha256, plugin_yml=plugin_yml)


@dataclass
class _GithubRelease:
    """One staged github release: its tag and the root ``tai-plugin.yml`` payload
    the contents API serves for that tag."""

    tag: str
    plugin_yaml: bytes


# The git object-tree mode for a subdirectory and for a regular (non-executable)
# file blob. The docs-fetch mode allowlist is 100644/100755 (see W2_CONTRACTS §2);
# the mock's docs files are regular blobs, so they carry 100644.
_GIT_TREE_MODE = "040000"
_GIT_BLOB_MODE = "100644"
# The subdirectory a plugin's docs tree lives under at the repo root — the segment
# the docs-fetch walks from the tag's root tree to the docs subtree SHA.
_DOCS_DIRNAME = "docs"


def _git_blob_sha(content: bytes) -> str:
    """The git blob object id for ``content`` (``sha1("blob <len>\\0<bytes>")``) —
    a stable, content-addressed id so the tree listing and the ``/git/blobs`` fetch
    agree on the same sha, exactly as a real repository's do."""
    header = f"blob {len(content)}\0".encode()
    return hashlib.sha1(header + content).hexdigest()


class FixturePackageIndex:
    """A thread-hosted package index serving forged fixture artifacts.

    The PyPI surfaces (``/simple/``, ``/wheels/``, ``/pypi/{project}/json``)
    serve wheels registered via :meth:`register`; the github-shaped surfaces
    (``/gh-api/repos/{owner}/{repo}`` existence probe,
    ``/gh-api/repos/{owner}/{repo}/contents/...`` per-tag spec, and the
    ``/gh-api/repos/{owner}/{repo}/git/trees|blobs/...`` docs-fetch tree the
    registry's github docs ingest reads) serve releases staged via
    :meth:`register_github_release` / :meth:`register_github_docs_tree`. Every
    request path is recorded so a spec can assert the registry actually fetched
    (and that nothing was fetched after teardown)."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        self.port = allocate_port()
        self.requests: list[str] = []
        # Registered wheels keyed by PEP 503-normalized project name.
        self._wheels: dict[str, list[BuiltWheel]] = {}
        # Wheel bytes keyed by filename, for the /wheels/ handler.
        self._wheel_by_filename: dict[str, BuiltWheel] = {}
        # Staged github releases keyed by tag; each serves its own stamped spec.
        self._gh_releases: dict[str, _GithubRelease] = {}
        # Staged git-data trees keyed by sha (and, for a tag's root, by the tag
        # itself so the docs fetch can resolve the tag's root tree in one hop);
        # docs blobs keyed by their git blob sha.
        self._gh_trees: dict[str, dict[str, object]] = {}
        self._gh_blobs: dict[str, bytes] = {}
        self._server = ThreadedServer(self._build_app(), host, self.port)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def simple_url(self) -> str:
        """The PEP 503 root pip resolves against (the ``PIP_INDEX_URL`` value)."""
        return f"{self.url}/simple/"

    @property
    def github_api_base(self) -> str:
        """The base ``MP_GITHUB_API_BASE`` points at."""
        return f"{self.url}/gh-api"

    def start(self) -> None:
        self._server.start()

    def stop(self) -> None:
        self._server.stop()

    def register(self, wheel: BuiltWheel) -> None:
        """Publish a wheel on the PyPI/PEP 503 surfaces."""
        key = _normalize_project(wheel.project)
        self._wheels.setdefault(key, []).append(wheel)
        self._wheel_by_filename[wheel.path.name] = wheel

    def register_github_release(self, tag: str, plugin_yml: str) -> None:
        """Stage a github release: its tag and the stamped root ``tai-plugin.yml``
        the contents API serves for that tag (honouring ``?ref=<tag>``)."""
        self._gh_releases[tag] = _GithubRelease(tag=tag, plugin_yaml=plugin_yml.encode("utf-8"))

    def register_github_docs_tree(self, tag: str, docs_files: dict[str, str]) -> None:
        """Stage a tag's ``docs/`` tree on the git-data surfaces (``/git/trees`` +
        ``/git/blobs``), the shape the registry's github docs ingest fetches.

        ``docs_files`` maps docs-relative posix paths (e.g. ``"index.mdx"``) to
        their text. Mirrors the pinned PLAN_3 fetch shape (W2_CONTRACTS §2): the
        tag's root tree carries a single ``docs`` subtree entry; the docs subtree
        lists each file as a ``100644`` blob; each blob is served base64-encoded by
        its git object id. The root tree is addressable by BOTH the tag and its own
        sha, so a fetch resolving the tag's root tree in one hop and one walking a
        ref → root-sha first both land."""
        blobs: dict[str, bytes] = {name: text.encode("utf-8") for name, text in docs_files.items()}
        docs_entries = [
            {
                "path": name,
                "mode": _GIT_BLOB_MODE,
                "type": "blob",
                "sha": _git_blob_sha(content),
                "size": len(content),
            }
            for name, content in sorted(blobs.items())
        ]
        docs_sha = hashlib.sha1(
            ("docs-tree:" + tag + ":" + ",".join(f"{e['path']}:{e['sha']}" for e in docs_entries)).encode()
        ).hexdigest()
        root_entries = [{"path": _DOCS_DIRNAME, "mode": _GIT_TREE_MODE, "type": "tree", "sha": docs_sha}]
        root_sha = hashlib.sha1(("root-tree:" + tag + ":" + docs_sha).encode()).hexdigest()
        root_tree = {"sha": root_sha, "truncated": False, "tree": root_entries}
        self._gh_trees[tag] = root_tree
        self._gh_trees[root_sha] = root_tree
        self._gh_trees[docs_sha] = {"sha": docs_sha, "truncated": False, "tree": docs_entries}
        for entry in docs_entries:
            self._gh_blobs[str(entry["sha"])] = blobs[str(entry["path"])]

    def _pypi_json(self, project: str) -> dict[str, object]:
        wheels = self._wheels[_normalize_project(project)]
        releases: dict[str, list[dict[str, object]]] = {}
        for wheel in wheels:
            releases.setdefault(wheel.version, []).append(
                {
                    "url": f"{self.url}/wheels/{wheel.path.name}",
                    "digests": {"sha256": wheel.sha256},
                    "filename": wheel.path.name,
                    "packagetype": "bdist_wheel",
                }
            )
        latest = max(wheels, key=lambda w: Version(w.version)).version
        return {
            # ``description`` is the long description of the built distribution —
            # the markdown the registry stores as a pypi-source listing's README.
            # It carries a GFM table so the detail page renders a real ``<table>``.
            "info": {"name": wheels[0].project, "version": latest, "description": README_MARKDOWN},
            "releases": releases,
        }

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/simple/")
        async def simple_root() -> HTMLResponse:
            self.requests.append("/simple/")
            anchors = "".join(f'<a href="{name}/">{name}</a>\n' for name in sorted(self._wheels))
            return HTMLResponse(f"<!DOCTYPE html><html><body>\n{anchors}</body></html>\n")

        @app.get("/simple/{project}/")
        async def simple_project(project: str) -> HTMLResponse:
            self.requests.append(f"/simple/{project}/")
            wheels = self._wheels.get(_normalize_project(project))
            if wheels is None:
                return HTMLResponse("unknown project", status_code=404)
            anchors = "".join(
                f'<a href="{self.url}/wheels/{w.path.name}#sha256={w.sha256}">{w.path.name}</a>\n' for w in wheels
            )
            return HTMLResponse(f"<!DOCTYPE html><html><body>\n{anchors}</body></html>\n")

        @app.get("/wheels/{filename}")
        async def wheel_file(filename: str) -> Response:
            self.requests.append(f"/wheels/{filename}")
            wheel = self._wheel_by_filename.get(filename)
            if wheel is None:
                return JSONResponse({"error": f"unknown wheel {filename}"}, status_code=404)
            return FileResponse(wheel.path, media_type="application/octet-stream", filename=filename)

        @app.get("/pypi/{project}/json")
        async def pypi_json(project: str) -> JSONResponse:
            self.requests.append(f"/pypi/{project}/json")
            if _normalize_project(project) not in self._wheels:
                return JSONResponse({"error": f"unknown project {project}"}, status_code=404)
            return JSONResponse(self._pypi_json(project))

        @app.get("/gh-api/repos/{owner}/{repo}")
        async def gh_repo(owner: str, repo: str) -> JSONResponse:
            # The lightweight existence probe a repo-form seed makes before ingest
            # (``github.repo_exists``). The fixture index serves only the harness's
            # own controlled repos, so existence is unconditional here.
            self.requests.append(f"/gh-api/repos/{owner}/{repo}")
            return JSONResponse({"full_name": f"{owner}/{repo}"})

        @app.get("/gh-api/repos/{owner}/{repo}/contents/{path:path}")
        async def gh_contents(owner: str, repo: str, path: str, ref: str | None = None) -> JSONResponse:
            # Serve the stamped ``tai-plugin.yml`` for the requested tag. The tag
            # webhook fetches this at ``?ref=<tag>`` and rejects a spec whose
            # declared version does not normalize from that tag, so each tag must
            # serve its own stamped spec.
            self.requests.append(f"/gh-api/repos/{owner}/{repo}/contents/{path}")
            if path.rsplit("/", 1)[-1] == _PLUGIN_SPEC_FILENAME and ref is not None:
                release = self._gh_releases.get(ref)
                if release is not None:
                    content = base64.b64encode(release.plugin_yaml).decode("ascii")
                    return JSONResponse({"encoding": "base64", "content": content, "path": path})
            return JSONResponse({"error": f"not found: {path} at ref {ref!r}"}, status_code=404)

        @app.get("/gh-api/repos/{owner}/{repo}/git/trees/{tree_sha}")
        async def gh_tree(owner: str, repo: str, tree_sha: str, recursive: str | None = None) -> JSONResponse:
            # The docs-fetch resolves the tag's root tree (addressed by the tag or
            # its root sha), walks the ``docs`` segment to the docs subtree sha, then
            # makes ONE recursive call on that small subtree. The staged trees are
            # shallow, so ``?recursive=1`` returns the same stored entry list — the
            # mock never serves a whole-repo recursive listing (W2_CONTRACTS §2).
            self.requests.append(f"/gh-api/repos/{owner}/{repo}/git/trees/{tree_sha}")
            tree = self._gh_trees.get(tree_sha)
            if tree is None:
                return JSONResponse({"error": f"not found: tree {tree_sha}"}, status_code=404)
            return JSONResponse(tree)

        @app.get("/gh-api/repos/{owner}/{repo}/git/blobs/{file_sha}")
        async def gh_blob(owner: str, repo: str, file_sha: str) -> JSONResponse:
            # Serve a docs blob base64-encoded by its git object id, the last hop of
            # the tree-walk docs fetch. Bounded reads are the fetcher's concern; the
            # mock serves only the harness's own small docs blobs.
            self.requests.append(f"/gh-api/repos/{owner}/{repo}/git/blobs/{file_sha}")
            content = self._gh_blobs.get(file_sha)
            if content is None:
                return JSONResponse({"error": f"not found: blob {file_sha}"}, status_code=404)
            encoded = base64.b64encode(content).decode("ascii")
            return JSONResponse({"sha": file_sha, "encoding": "base64", "content": encoded, "size": len(content)})

        @app.get("/gh-api/repos/{owner}/{repo}/readme")
        async def gh_readme(owner: str, repo: str) -> JSONResponse:
            self.requests.append(f"/gh-api/repos/{owner}/{repo}/readme")
            return JSONResponse({"error": "no readme"}, status_code=404)

        @app.api_route("/{path:path}", methods=["GET"])
        async def unknown(path: str) -> JSONResponse:
            self.requests.append(f"/{path}")
            return JSONResponse({"error": f"not found: /{path}"}, status_code=404)

        return app
