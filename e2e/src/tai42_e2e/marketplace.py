"""The tai42-marketplace registry as a first-class harness resource.

The registry is OUR service under test, so it boots the way the skeleton SUT
does — REAL OS processes spawned from the shared venv via
:class:`~tai42_e2e.procs.ProcessHandle`, from the out-of-band pinned install, with
log files and a leak-checked teardown — not a net-fixture thread. A
:class:`MarketplaceService` owns an isolated Postgres database (created empty and
schema-bootstrapped through the production ``tai42-marketplace db migrate`` path,
which replays the migration chain — its baseline runs ``CREATE EXTENSION
pg_trgm``), spawns the API server, and waits for ``GET /healthz`` == 200.

:func:`seed_fixture_catalog` is the shared seeding helper (both the pytest
fixtures and the studio runner call it): it stages the forged fixture wheels on
the package index and drives the registry's REAL admin-seed + ingest pipeline,
which publishes each version synchronously in the seed request.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import os
import secrets
import subprocess
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from tai42_e2e import ports
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.pkgsource import (
    BuiltTarball,
    BuiltWheel,
    FixturePackageIndex,
    build_fixture_source_tarball,
    build_fixture_wheel,
)
from tai42_e2e.procs import ProcessHandle
from tai42_e2e.waiting import wait_for, wait_for_async

if TYPE_CHECKING:
    from tai42_e2e.settings import HarnessSettings
    from tai42_e2e.stack import Infra

# The registry lives in its own private repo, kept out of the monorepo lock. The
# opt-in marketplace suite installs it out-of-band at boot from this pinned ref
# (the git insteadOf token config rewrites the URL — no token handling here).
_MARKETPLACE_GIT_URL = "https://github.com/tai42ai/tai-marketplace"
# Re-pin to the tai-marketplace commit SHA that moves the registry off its
# standalone ``db init`` onto the framework migration runner (``db migrate``) at
# the user-gated release. That commit is the ONLY ref whose CLI carries the
# ``db migrate`` path :meth:`MarketplaceService._apply_ddl` now drives; it does
# not exist until that release is cut, so this placeholder is deliberately not a
# resolvable ref — the marketplace suite fails loudly on out-of-band install
# until it is filled, never silently resolving an older ``db init``-only pin.
_MARKETPLACE_PIN = "b2a5b188ea904551cf026add33c1ad7f7a5a2010"


def _registry_venv_dir() -> Path:
    """The DEDICATED venv the pinned registry installs into — a per-checkout,
    per-pin directory under this e2e member (alongside its workspace ``.venv``),
    built once and reused across modules and sessions.

    Per-checkout, not host-global: a shared system-temp directory lets concurrent
    runs on separate checkouts race on ``uv venv --clear``, one wiping another's
    live registry. Keying off this package's own location isolates each checkout.

    The registry must NEVER share the SUT's workspace venv. Its pinned dependency
    caps (tai42-contract / tai42-kit) are point-in-time: in a release-PR window
    where the workspace has moved a first-party package past a registry cap, a
    shared-venv install would DOWNGRADE that workspace package from PyPI and the
    skeleton would then quarantine its own routers at boot. A separate venv keeps
    the registry's dependency resolution wholly apart from the SUT's."""
    return Path(__file__).resolve().parents[2] / f".tai42-e2e-marketplace-{_MARKETPLACE_PIN[:12]}"


def _registry_python() -> Path:
    return _registry_venv_dir() / "bin" / "python"


def _registry_bin() -> Path:
    return _registry_venv_dir() / "bin" / "tai42-marketplace"


_FIXTURES_ENV = "TAI_E2E_MARKETPLACE_FIXTURES"
# The in-repo fixture-plugin sources (outside ``src`` so uv never installs them,
# outside ``tests`` so pytest never collects them). ``TAI_E2E_MARKETPLACE_FIXTURES``
# overrides for an out-of-tree checkout.
_DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "marketplace_plugins"


def _resolve_fixtures_dir(fixtures_dir: Path | None) -> Path:
    """The marketplace fixture-plugin source tree the forge builds artifacts from.

    Resolves the explicit argument, then ``TAI_E2E_MARKETPLACE_FIXTURES``, then the
    in-repo default. A missing directory raises loudly — never a silent empty forge."""
    resolved = fixtures_dir if fixtures_dir is not None else _env_fixtures_dir()
    if not resolved.is_dir():
        raise RuntimeError(
            f"marketplace fixtures dir {resolved} does not exist; point "
            f"{_FIXTURES_ENV} (or the fixtures_dir argument) at the checked-out "
            "marketplace_plugins tree"
        )
    return resolved


def _env_fixtures_dir() -> Path:
    raw = os.environ.get(_FIXTURES_ENV)
    return Path(raw) if raw else _DEFAULT_FIXTURES_DIR


ALPHA_PACKAGE = "tai-e2e-market-alpha"
BETA_PACKAGE = "tai-e2e-market-beta"
GAMMA_PACKAGE = "tai-e2e-market-gamma"
DELTA_PACKAGE = "tai-e2e-market-delta"
EPSILON_PACKAGE = "tai-e2e-market-epsilon"
ZETA_PACKAGE = "tai-e2e-market-zeta"
ETA_PACKAGE = "tai-e2e-market-eta"
ALPHA_REF = "tai42/e2e-alpha"
BETA_REF = "tai42/e2e-beta"
GAMMA_REF = "tai42/e2e-gamma"
DELTA_REF = "tai42/e2e-delta"
EPSILON_REF = "tai42/e2e-epsilon"
ZETA_REF = "tai42/e2e-zeta"
ETA_REF = "tai42/e2e-eta"

# Eta is the mcp-server fixture: its one provided item is kind ``mcp-server`` whose
# ``mcp.command`` launches the fixture's own one-tool stdio server. An mcp-server
# package imports no tai42-contract, so its spec declares NO contract range and the
# forge stamps none (:func:`forge_fixture_artifacts`). The install writes a manifest
# ``mcp`` entry titled by the item name, under which the mounted tool binds.
ETA_MCP_TITLE = "e2e_eta_mcp"
ETA_MCP_TOOL = "e2e_eta_mcp_ping"

# Zeta is the plugin-compat fixture: two published versions whose DECLARED
# contract ranges differ. 0.1.0 declares the wide range (it contains the
# tai42-contract version the shared venv actually runs — asserted at seed time
# by ``assert_zeta_ranges_bracket_running_contract``); 0.2.0 declares the narrow
# future range, which excludes it. The wheels are forged per compat spec via
# :func:`forge_zeta_wheel`, never through ``forge_fixture_artifacts``.
ZETA_COMPAT_VERSION = "0.1.0"
ZETA_INCOMPAT_VERSION = "0.2.0"
ZETA_WIDE_CONTRACT_RANGE = ">=0.1,<9"
ZETA_NARROW_CONTRACT_RANGE = ">=9,<10"
# The module zeta's one tool item provides — the manifest config row an install
# persists ({"title": <module>, "module": <module>}) targets this module.
ZETA_TOOLS_MODULE = "tai_e2e_market_zeta.tools"

# Delta is the one github-sourced fixture: seeded repo-form against this URL (which
# its checked-in ``tai-plugin.yml`` declares), never via a wheel. The webhook-ingest
# spec matches its tag-push deliveries on this repository URL.
DELTA_REPOSITORY_URL = "https://github.com/tai42ai/tai-e2e-market-delta"


@dataclass(frozen=True)
class FixtureArtifacts:
    """The forged fixture artifacts a marketplace-area run needs: the pypi-sourced
    wheels (alpha 0.1.0/0.2.0, beta 0.1.0, gamma 0.1.0, epsilon 0.1.0, eta 0.1.0) and
    the github-sourced delta source tarballs (0.1.0/0.2.0). Immutable and shareable
    across modules — forging is pure and the built files never change."""

    alpha_v1: BuiltWheel
    alpha_v2: BuiltWheel
    beta_v1: BuiltWheel
    gamma_v1: BuiltWheel
    epsilon_v1: BuiltWheel
    eta_v1: BuiltWheel
    delta_v1: BuiltTarball
    delta_v2: BuiltTarball


def _workspace_contract_range() -> str:
    """The tai42-contract version specifier the running tai42-skeleton declares.

    The fixture plugins import ``tai42_contract`` and install into the skeleton's
    environment, so their declared contract range must CONTAIN the workspace
    contract at every release-version window — otherwise a release-PR bump moves
    the workspace contract past a fixture's static cap and the install can no
    longer resolve it against the environment's own contract. Mirroring the
    skeleton's declared range tracks the workspace automatically (the skeleton's
    cap is the authoritative compat band). Raises loudly if the skeleton declares
    no tai42-contract specifier, or declares more than one distinct specifier
    (base + extra-gated with different bands) — there is no single band to mirror."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    specifiers: set[str] = set()
    for raw in importlib.metadata.requires("tai42-skeleton") or []:
        req = Requirement(raw)
        if canonicalize_name(req.name) == "tai42-contract":
            if not req.specifier:
                raise RuntimeError("tai42-skeleton declares tai42-contract with no version specifier")
            specifiers.add(str(req.specifier))
    if not specifiers:
        raise RuntimeError("tai42-skeleton declares no tai42-contract dependency to mirror in the fixture plugins")
    if len(specifiers) > 1:
        raise RuntimeError(
            "tai42-skeleton declares conflicting tai42-contract specifiers "
            f"({', '.join(sorted(specifiers))}); no single band to mirror in the fixture plugins"
        )
    return specifiers.pop()


def contract_facet_probe_versions() -> tuple[str, str]:
    """A contract version INSIDE the workspace contract band and one BELOW its
    lower bound, both derived from the SAME band the forge stamps onto every
    fixture (:func:`_workspace_contract_range`).

    The forge stamps each fixture's declared contract range to the workspace band,
    so a registry ``contract=`` facet probe is window-dependent: the inside probe
    must land within whatever band the current release window declares (every
    fixture matches it) and the below probe must fall under its lower bound (no
    fixture matches). Deriving both from the band tracks the window automatically.
    Raises loudly if the band exposes no lower bound, or a derived probe falls the
    wrong side of it."""
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    band = _workspace_contract_range()
    spec = SpecifierSet(band)
    lowers = [Version(s.version) for s in spec if s.operator in (">=", ">", "~=", "==")]
    if not lowers:
        raise RuntimeError(f"workspace contract band {band!r} has no lower bound to derive facet probes from")
    lower = max(lowers)
    inside = f"{lower.major}.{lower.minor}.5"
    if lower.minor > 0:
        below = f"{lower.major}.{lower.minor - 1}.5"
    elif lower.major > 0:
        below = f"{lower.major - 1}.9.5"
    else:
        raise RuntimeError(f"workspace contract band {band!r} lower bound {lower} has nothing below it to probe")
    if not spec.contains(inside, prereleases=True):
        raise RuntimeError(f"derived inside probe {inside} is not within the workspace contract band {band!r}")
    if spec.contains(below, prereleases=True):
        raise RuntimeError(f"derived below-band probe {below} is within the workspace contract band {band!r}")
    return inside, below


def forge_fixture_artifacts(out_dir: Path, fixtures_dir: Path | None = None) -> FixtureArtifacts:
    """Forge every fixture artifact into ``out_dir``: the six wheels and the two
    delta source tarballs (delta is the github-sourced listing and gets no
    wheel). Reads the fixture-plugin sources from ``fixtures_dir`` (or
    ``TAI_E2E_MARKETPLACE_FIXTURES``); each build stamps its version into a copy of
    the source, never mutating the source tree.

    Every CONTRACT-BEARING fixture's declared contract range (and its built
    ``Requires-Dist`` specifier) is stamped to the workspace's own contract band
    (:func:`_workspace_contract_range`), so the install resolves against the
    environment's contract at every release-version window. The eta fixture is the
    lone exception: its one item is kind ``mcp-server``, so its spec declares no
    ``contract`` and its package depends on no tai42-contract — it is forged with NO
    contract stamp (passing a range would fail its all-mcp-server spec validation)."""
    src = _resolve_fixtures_dir(fixtures_dir)
    contract_range = _workspace_contract_range()
    return FixtureArtifacts(
        alpha_v1=build_fixture_wheel(src / "alpha", "0.1.0", out_dir, contract_range=contract_range),
        alpha_v2=build_fixture_wheel(src / "alpha", "0.2.0", out_dir, contract_range=contract_range),
        beta_v1=build_fixture_wheel(src / "beta", "0.1.0", out_dir, contract_range=contract_range),
        gamma_v1=build_fixture_wheel(src / "gamma", "0.1.0", out_dir, contract_range=contract_range),
        epsilon_v1=build_fixture_wheel(src / "epsilon", "0.1.0", out_dir, contract_range=contract_range),
        eta_v1=build_fixture_wheel(src / "eta", "0.1.0", out_dir),
        delta_v1=build_fixture_source_tarball(src / "delta", "0.1.0", out_dir, contract_range=contract_range),
        delta_v2=build_fixture_source_tarball(src / "delta", "0.2.0", out_dir, contract_range=contract_range),
    )


@dataclass
class _ProcSpec:
    """Everything needed to (re)spawn one registry process, kept so ``start`` can
    rebuild an identical handle after a controlled outage."""

    name: str
    argv: list[str]
    log_path: Path


def _marketplace_source_env(switch: HarnessSettings, index_url: str) -> dict[str, str]:
    """The registry's outbound ingest-source coordinates (the ``MP_*`` PyPI/GitHub
    knobs the validator/ingest fetches through).

    MOCK (default): both sources point at the fixture package index, so every fetch
    lands on the local PyPI JSON/wheel + github-shaped handlers. REAL: the named seam
    drops its fixture override so the registry resolves from the live vendor — the two
    seams toggle independently:
      * ``marketplace-pypi`` real → no ``MP_PYPI_BASE_URL`` (→ real pypi.org; public,
        so there is no operator credential and nothing to loud-fail on);
      * ``marketplace-github`` real → no ``MP_GITHUB_API_BASE`` (→ real api.github.com)
        and ``MP_GITHUB_TOKEN`` carried for the rate limit (the collection-time gate has
        already loud-failed a real selection whose token is absent).
    Every mock-side key is byte-for-byte today's fill."""
    env: dict[str, str] = {}
    if not switch.is_real("marketplace-pypi"):
        env["MP_PYPI_BASE_URL"] = index_url
    if switch.is_real("marketplace-github"):
        env["MP_GITHUB_TOKEN"] = os.environ["MP_GITHUB_TOKEN"]
    else:
        env["MP_GITHUB_API_BASE"] = f"{index_url}/gh-api"
    return env


class MarketplaceService:
    """A booted tai42-marketplace registry: the API server on an isolated
    Postgres database.

    Construct, then :meth:`boot`; use :meth:`stop` / :meth:`start` for a
    controlled outage and clean recovery; :meth:`teardown` reaps everything
    leak-free. The registry's ``{"data": …}`` envelope matches
    :class:`~tai42_e2e.httpapi.ApiClient`, so :attr:`api` is one; admin routes are
    addressed with :attr:`admin_headers`."""

    def __init__(
        self,
        infra: Infra,
        root: Path,
        *,
        index_url: str,
        port: int | None = None,
        admin_token: str | None = None,
    ) -> None:
        self._infra = infra
        self.root = root
        self.host = "127.0.0.1"
        self._index_url = index_url.rstrip("/")

        # Own empty Postgres DB — the registry applies its own schema, incl.
        # CREATE EXTENSION pg_trgm.
        self._db_name = f"tai42_e2e_mp_{uuid.uuid4().hex[:6]}"
        try:
            infra.pg.create_empty_db(self._db_name)
            if port is None:
                self.port = ports.allocate_port()
            else:
                ports.reserve_specific_port(port)
                self.port = port
        except BaseException:
            # A failure after creating the empty DB (including a taken pinned port)
            # leaves no instance for the caller to tear down, so reclaim the DB here
            # before re-raising. The drop tolerates a DB that was never created and
            # suppresses its own error so the original failure propagates.
            with contextlib.suppress(Exception):
                infra.pg.drop_stack_db(self._db_name)
            raise

        self.admin_token = admin_token if admin_token is not None else f"mp-admin-{secrets.token_urlsafe(24)}"
        self.webhook_secret = secrets.token_hex(16)
        self._logs_dir = root / "logs"

        self._procs: dict[str, ProcessHandle] = {}
        self._specs: dict[str, _ProcSpec] = {}
        self._torn_down = False

    # ---- coordinates -----------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def api(self) -> ApiClient:
        """An ``httpx``-based client for the registry (its ``{"data": …}``
        envelope matches the skeleton's, so :class:`ApiClient` unwraps it)."""
        return ApiClient(self.base_url)

    @property
    def admin_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.admin_token}"}

    def _database_url(self) -> str:
        s = self._infra.settings
        return f"postgresql://{s.pg_user}:{s.pg_password}@{s.pg_host}:{s.pg_port}/{self._db_name}"

    def _service_env(self) -> dict[str, str]:
        """A from-scratch child env: PATH/HOME plus the ``MP_*`` group. Never an
        ``os.environ`` passthrough. The outbound PyPI/GitHub bases default to the
        fixture package index (MOCK) so every fetch the validator/ingest makes lands
        on the local handlers; a real ``marketplace-pypi``/``marketplace-github`` seam
        drops its fixture override so that source resolves from the live vendor
        (:func:`_marketplace_source_env`)."""
        venv_bin = str(_registry_venv_dir() / "bin")
        return {
            "PATH": os.pathsep.join([venv_bin, "/usr/local/bin", "/usr/bin", "/bin"]),
            "HOME": os.environ.get("HOME", str(self.root)),
            "MP_DATABASE_URL": self._database_url(),
            "MP_ADMIN_TOKEN": self.admin_token,
            "MP_GITHUB_WEBHOOK_SECRET": self.webhook_secret,
            "MP_BASE_URL": self.base_url,
            **_marketplace_source_env(self._infra.settings, self._index_url),
        }

    # ---- lifecycle -------------------------------------------------------

    def _bin(self) -> str:
        """The ``tai42-marketplace`` console script from the registry's DEDICATED
        venv (never the SUT's workspace venv — see :func:`_registry_venv_dir`).

        The registry is absent from the monorepo lock, so its venv carries no
        console script until installed: install the pinned registry into that
        venv on first use. Idempotent — reuse only when the console script
        exists AND the venv's base interpreter symlink is still alive (an
        upgraded/removed base leaves a dead shebang that fails at spawn); else
        reinstall."""
        candidate = _registry_bin()
        if not candidate.exists() or not _registry_python().resolve().exists():
            self._install_marketplace()
        return str(candidate)

    @staticmethod
    def _install_marketplace() -> None:
        """Create the dedicated registry venv and ``uv pip install`` the pinned
        tai42-marketplace into it — apart from the SUT's workspace venv, so the
        registry's own dependency caps never mutate the workspace's first-party
        packages (the git insteadOf token config rewrites the URL — no token
        handling here). Raises loudly on a non-zero exit with the captured output."""
        venv = _registry_venv_dir()
        mk = subprocess.run(
            ["uv", "venv", "--clear", "--python", sys.executable, str(venv)],
            capture_output=True,
            text=True,
            check=False,
        )
        if mk.returncode != 0:
            raise RuntimeError(
                f"creating the registry venv at {venv} failed (exit {mk.returncode}):\n{mk.stdout}\n{mk.stderr}"
            )
        spec = f"tai42-marketplace @ git+{_MARKETPLACE_GIT_URL}@{_MARKETPLACE_PIN}"
        proc = subprocess.run(
            ["uv", "pip", "install", "--python", str(_registry_python()), spec],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"installing {spec} into the registry venv {venv} failed "
                f"(exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
            )

    def boot(self) -> None:
        """Apply the schema through the production DDL path, then spawn the API
        server and wait for ``/healthz``."""
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        self._apply_ddl()
        tai42_mp = self._bin()
        self._specs = {
            "mp-api": _ProcSpec(
                name="mp-api",
                argv=[tai42_mp, "serve", "--host", self.host, "--port", str(self.port)],
                log_path=self._logs_dir / "mp-api.log",
            ),
        }
        self._spawn_all()
        self._wait_ready()

    def _apply_ddl(self) -> None:
        """Run ``tai42-marketplace db migrate`` to completion against the empty DB.

        The production bootstrap path: it replays the registry's migration chain,
        whose baseline runs ``CREATE EXTENSION pg_trgm``, so the DB must be owned
        (created by ``create_empty_db``, not a template clone). A non-zero exit
        raises loudly with the captured output."""
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [self._bin(), "db", "migrate"],
            cwd=str(self.root),
            env=self._service_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        (self._logs_dir / "mp-db-migrate.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        if proc.returncode != 0:
            raise RuntimeError(
                f"tai42-marketplace db migrate failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
            )

    def _spawn_all(self) -> None:
        env = self._service_env()
        for spec in self._specs.values():
            handle = ProcessHandle(name=spec.name, argv=spec.argv, cwd=self.root, env=env, log_path=spec.log_path)
            self._procs[spec.name] = handle
            handle.start()

    def _wait_ready(self) -> None:
        deadline = self._infra.settings.boot_timeout
        url = f"{self.base_url}/healthz"

        def probe() -> bool:
            early = self._early_exit_detail()
            if early is not None:
                raise RuntimeError(f"marketplace readiness: {early}")
            try:
                return httpx.get(url, timeout=2.0).status_code == 200
            except httpx.HTTPError:
                return False

        wait_for(probe, deadline=deadline, message=f"marketplace registry never became ready at {url}")

    def _early_exit_detail(self) -> str | None:
        for handle in self._procs.values():
            if not handle.is_running():
                return f"process {handle.name!r} exited early (code {handle.poll()}):\n{handle.log_tail()}"
        return None

    def stop(self) -> None:
        """Terminate the API server for a controlled outage and wait the API port
        frees, keeping the DB and specs so :meth:`start` can recover."""
        errors: list[str] = []
        for handle in list(self._procs.values()):
            try:
                handle.terminate()
            except Exception as exc:
                errors.append(f"terminate {handle.name}: {exc!r}")
            if handle.is_running():
                errors.append(f"process {handle.name} still running after SIGKILL (leak)")
        self._procs.clear()
        # Wait for the API port to free before a later start() rebinds it.
        try:
            wait_for(lambda: ports.is_free(self.port), deadline=5.0, message=f"port {self.port} never freed")
        except TimeoutError:
            errors.append(f"port {self.port} still bound after stop (leak)")
        if errors:
            raise RuntimeError("marketplace stop found leaks:\n  " + "\n  ".join(errors))

    def start(self) -> None:
        """Respawn the API server from the saved specs and wait for readiness
        (the DDL already landed on the persistent DB)."""
        if self._procs:
            raise RuntimeError("marketplace service is already running; call stop() before start()")
        self._spawn_all()
        self._wait_ready()

    def teardown(self) -> None:
        """Terminate the API server, assert reaped, wait the port frees, drop the
        DB, and remove ``root`` per ``keep_stacks`` — collecting every error and
        raising a combined failure (nothing swallowed)."""
        if self._torn_down:
            return
        self._torn_down = True
        errors: list[str] = []
        for handle in list(self._procs.values()):
            try:
                handle.terminate()
            except Exception as exc:
                errors.append(f"terminate {handle.name}: {exc!r}")
            if handle.is_running():
                errors.append(f"process {handle.name} still running after SIGKILL (leak)")
        self._procs.clear()
        try:
            wait_for(lambda: ports.is_free(self.port), deadline=5.0, message=f"port {self.port} never freed")
        except TimeoutError:
            errors.append(f"port {self.port} still bound after teardown (leak)")
        ports.release_port(self.port)
        try:
            self._infra.pg.drop_stack_db(self._db_name)
        except Exception as exc:
            errors.append(f"drop database {self._db_name}: {exc!r}")
        if not self._infra.settings.keep_stacks:
            import shutil

            shutil.rmtree(self.root, ignore_errors=True)
        if errors:
            raise RuntimeError("marketplace teardown found leaks:\n  " + "\n  ".join(errors))


# ---- seeding ------------------------------------------------------------


async def _wait_published(mp: MarketplaceService, ref: str, version: str, *, deadline: float = 30.0) -> None:
    """Confirm ``ref`` reports ``version`` at status ``published``.

    The admin seed publishes each version synchronously in its own request and
    :func:`_admin_seed` already raises on a failed row, so this is a read-back
    confirmation of the just-published version, not a wait on a background
    pipeline."""

    async def probe() -> bool:
        payload = await mp.api.get(f"/api/v1/plugins/{ref}/versions")
        statuses = {row["version"]: row["status"] for row in payload["versions"]}
        return statuses.get(version) == "published"

    await wait_for_async(
        probe, deadline=deadline, message=f"listing {ref} version {version} never reached status 'published'"
    )


async def _admin_seed(mp: MarketplaceService, entries: Sequence[tuple[str, str]]) -> None:
    """Drive the registry's admin-seed route for ``(package, plugin_yml)`` pairs
    and fail loudly on any failed result row.

    Each entry carries the inline stamped ``tai-plugin.yml`` the route requires
    alongside its pip distribution name. The route confines a per-entry failure to
    its result row (``{"status": "failed", "error": ...}``) rather than raising, so
    an entry that never registered would otherwise surface only as a later publish
    timeout with its root cause lost. Raise on any failed row instead."""
    repos = [{"package": package, "plugin_yml": plugin_yml} for package, plugin_yml in entries]
    seed_result = await mp.api.post("/api/v1/admin/seed", json={"repos": repos}, headers=mp.admin_headers)
    failed_rows = [row for row in seed_result["results"] if row.get("status") == "failed"]
    if seed_result.get("failed") or failed_rows:
        detail = ", ".join(f"{row.get('package') or row.get('repo') or '?'}: {row.get('error')}" for row in failed_rows)
        raise RuntimeError(f"admin seed failed to register {len(failed_rows)} listing(s): {detail}")


async def seed_fixture_catalog(mp: MarketplaceService, index: FixturePackageIndex, artifacts: FixtureArtifacts) -> None:
    """Stage the pypi-sourced browse fixtures on the package index and drive the
    real admin-seed + ingest pipeline until they publish.

    Seeds exactly the alpha/beta/gamma browse catalog. Registration is STAGED:
    only the ``0.1.0`` wheels are published before the seed (the index is empty
    when this runs — module-scoped and paired with a fresh registry), so the
    install spec can pin ``0.1.0`` before ``0.2.0`` exists. Alpha ``0.2.0`` is then
    registered on the index and published by an explicit re-seed of alpha with its
    stamped ``0.2.0`` spec, since the seed publishes synchronously and no
    background poller detects the newer release.

    Delta and epsilon are deliberately untouched here so they never enter the
    shared browse catalog: delta has no wheel and the webhook-ingest spec stages
    it on the github surfaces itself; epsilon is the router/middleware fixture the
    auto-merge spec publishes into its own registry via :func:`seed_epsilon_listing`."""
    index.register(artifacts.alpha_v1)
    index.register(artifacts.beta_v1)
    index.register(artifacts.gamma_v1)

    await _admin_seed(
        mp,
        (
            (ALPHA_PACKAGE, artifacts.alpha_v1.plugin_yml),
            (BETA_PACKAGE, artifacts.beta_v1.plugin_yml),
            (GAMMA_PACKAGE, artifacts.gamma_v1.plugin_yml),
        ),
    )

    await _wait_published(mp, ALPHA_REF, "0.1.0")
    await _wait_published(mp, BETA_REF, "0.1.0")
    await _wait_published(mp, GAMMA_REF, "0.1.0")

    # Publish alpha 0.2.0 by registering its wheel and re-seeding alpha with the
    # stamped 0.2.0 spec (the synchronous seed publishes it in the request).
    index.register(artifacts.alpha_v2)
    await _admin_seed(mp, ((ALPHA_PACKAGE, artifacts.alpha_v2.plugin_yml),))
    await _wait_published(mp, ALPHA_REF, "0.2.0")


def forge_zeta_wheel(
    version: str,
    out_dir: Path,
    *,
    contract_range: str,
    requires_dist_range: str | None = None,
    fixtures_dir: Path | None = None,
) -> BuiltWheel:
    """Forge one zeta wheel with a stamped declared contract range.

    Reads the zeta source from ``fixtures_dir`` (or ``TAI_E2E_MARKETPLACE_FIXTURES``).
    ``requires_dist_range`` defaults to ``contract_range`` (a lockstep wheel:
    the packaged ``tai-plugin.yml`` contract range equals the built
    ``Requires-Dist`` tai42-contract specifier); passing a different value
    forges the deliberately mismatched wheel the ingest lockstep gate must
    reject."""
    src = _resolve_fixtures_dir(fixtures_dir)
    return build_fixture_wheel(
        src / "zeta", version, out_dir, contract_range=contract_range, requires_dist_range=requires_dist_range
    )


def assert_zeta_ranges_bracket_running_contract() -> None:
    """Assert the zeta compat ranges really bracket the installed
    ``tai42-contract`` version: the wide range contains it, the narrow future
    range excludes it. Raises loudly otherwise — a compat spec running against a
    contract version outside the bracket would assert the wrong compatibility
    verdicts. ``prereleases=True`` on both sides, matching how the compat checks
    treat a dev-versioned editable contract checkout."""
    from packaging.specifiers import SpecifierSet

    running = importlib.metadata.version("tai42-contract")
    if not SpecifierSet(ZETA_WIDE_CONTRACT_RANGE).contains(running, prereleases=True):
        raise RuntimeError(
            f"zeta's wide contract range {ZETA_WIDE_CONTRACT_RANGE!r} does not contain the installed "
            f"tai42-contract {running}; the compat fixture bracket is broken"
        )
    if SpecifierSet(ZETA_NARROW_CONTRACT_RANGE).contains(running, prereleases=True):
        raise RuntimeError(
            f"zeta's narrow contract range {ZETA_NARROW_CONTRACT_RANGE!r} contains the installed "
            f"tai42-contract {running}; the compat fixture bracket is broken"
        )


async def seed_zeta_listing(mp: MarketplaceService, index: FixturePackageIndex, wheels: Sequence[BuiltWheel]) -> None:
    """Stage the given zeta wheels and publish each version through the real
    admin-seed + ingest pipeline, in the given order.

    Zeta is the plugin-compat fixture the core-aware resolve / boot-quarantine /
    upgrade-all specs select against: which versions publish is the spec's whole
    scenario (both compat wheels for a listing whose newest published version is
    contract-incompatible while an older compatible one exists; the narrow wheel
    alone for a listing with NO compatible published version), so the caller
    passes exactly the wheels its registry must carry. Kept OUT of
    :func:`seed_fixture_catalog` like delta and epsilon, so the shared browse
    catalog stays alpha/beta/gamma."""
    for wheel in wheels:
        index.register(wheel)
        await _admin_seed(mp, ((ZETA_PACKAGE, wheel.plugin_yml),))
        await _wait_published(mp, ZETA_REF, wheel.version)


async def seed_epsilon_listing(mp: MarketplaceService, index: FixturePackageIndex, artifacts: FixtureArtifacts) -> None:
    """Stage epsilon's ``0.1.0`` wheel and drive the real admin-seed + ingest
    pipeline until it publishes.

    Epsilon is the router/middleware fixture the auto-merge spec installs. It is
    kept OUT of :func:`seed_fixture_catalog` so it never pollutes the shared browse
    catalog — only the router-merge spec's own registry carries it, exactly as
    delta is seeded by the webhook-ingest spec rather than the shared seed."""
    index.register(artifacts.epsilon_v1)
    await _admin_seed(mp, ((EPSILON_PACKAGE, artifacts.epsilon_v1.plugin_yml),))
    await _wait_published(mp, EPSILON_REF, "0.1.0")


async def seed_eta_listing(mp: MarketplaceService, index: FixturePackageIndex, artifacts: FixtureArtifacts) -> None:
    """Stage eta's ``0.1.0`` wheel and drive the real admin-seed + ingest pipeline
    until it publishes.

    Eta is the mcp-server fixture the install-mcp-server spec installs. Kept OUT of
    :func:`seed_fixture_catalog` so it never pollutes the shared browse catalog —
    only the mcp-server spec's own registry carries it, exactly as delta/epsilon are
    seeded by their own specs rather than the shared seed. Its spec carries no
    ``contract`` (an all-mcp-server package), so the registry ingests it under the
    contract-less mcp-server kind branch."""
    index.register(artifacts.eta_v1)
    await _admin_seed(mp, ((ETA_PACKAGE, artifacts.eta_v1.plugin_yml),))
    await _wait_published(mp, ETA_REF, "0.1.0")
