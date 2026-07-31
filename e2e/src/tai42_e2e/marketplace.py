"""The tai42-marketplace registry as a first-class harness resource.

The registry is OUR service under test, so it boots the way the skeleton SUT
does — REAL OS processes spawned from the shared venv via
:class:`~tai42_e2e.procs.ProcessHandle`, from the editable sibling checkout, with
log files and a leak-checked teardown — not a net-fixture thread. A
:class:`MarketplaceService` owns an isolated Postgres database (created empty and
schema-bootstrapped through the production ``tai42-marketplace db init`` path,
which runs ``CREATE EXTENSION pg_trgm``), spawns the API server, and waits for
``GET /healthz`` == 200.

:func:`seed_fixture_catalog` is the shared seeding helper (both the pytest
fixtures and the studio runner call it): it stages the forged fixture wheels on
the package index and drives the registry's REAL admin-seed + ingest pipeline,
which publishes each version synchronously in the seed request.
"""

from __future__ import annotations

import contextlib
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
    from tai42_e2e.stack import Infra

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
ALPHA_REF = "tai42/e2e-alpha"
BETA_REF = "tai42/e2e-beta"
GAMMA_REF = "tai42/e2e-gamma"
DELTA_REF = "tai42/e2e-delta"
EPSILON_REF = "tai42/e2e-epsilon"
ZETA_REF = "tai42/e2e-zeta"

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
    wheels (alpha 0.1.0/0.2.0, beta 0.1.0, gamma 0.1.0, epsilon 0.1.0) and the
    github-sourced delta source tarballs (0.1.0/0.2.0). Immutable and shareable
    across modules — forging is pure and the built files never change."""

    alpha_v1: BuiltWheel
    alpha_v2: BuiltWheel
    beta_v1: BuiltWheel
    gamma_v1: BuiltWheel
    epsilon_v1: BuiltWheel
    delta_v1: BuiltTarball
    delta_v2: BuiltTarball


def forge_fixture_artifacts(out_dir: Path, fixtures_dir: Path | None = None) -> FixtureArtifacts:
    """Forge every fixture artifact into ``out_dir``: the five wheels and the two
    delta source tarballs (delta is the github-sourced listing and gets no
    wheel). Reads the fixture-plugin sources from ``fixtures_dir`` (or
    ``TAI_E2E_MARKETPLACE_FIXTURES``); each build stamps its version into a copy of
    the source, never mutating the source tree."""
    src = _resolve_fixtures_dir(fixtures_dir)
    return FixtureArtifacts(
        alpha_v1=build_fixture_wheel(src / "alpha", "0.1.0", out_dir),
        alpha_v2=build_fixture_wheel(src / "alpha", "0.2.0", out_dir),
        beta_v1=build_fixture_wheel(src / "beta", "0.1.0", out_dir),
        gamma_v1=build_fixture_wheel(src / "gamma", "0.1.0", out_dir),
        epsilon_v1=build_fixture_wheel(src / "epsilon", "0.1.0", out_dir),
        delta_v1=build_fixture_source_tarball(src / "delta", "0.1.0", out_dir),
        delta_v2=build_fixture_source_tarball(src / "delta", "0.2.0", out_dir),
    )


@dataclass
class _ProcSpec:
    """Everything needed to (re)spawn one registry process, kept so ``start`` can
    rebuild an identical handle after a controlled outage."""

    name: str
    argv: list[str]
    log_path: Path


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
        ``os.environ`` passthrough. The outbound PyPI/GitHub bases point at the
        fixture package index so every fetch the validator/ingest makes lands on
        the local handlers."""
        import os

        venv_bin = str(Path(sys.executable).parent)
        return {
            "PATH": os.pathsep.join([venv_bin, "/usr/local/bin", "/usr/bin", "/bin"]),
            "HOME": os.environ.get("HOME", str(self.root)),
            "MP_DATABASE_URL": self._database_url(),
            "MP_ADMIN_TOKEN": self.admin_token,
            "MP_GITHUB_WEBHOOK_SECRET": self.webhook_secret,
            "MP_BASE_URL": self.base_url,
            # Every outbound fetch lands on the fixture index (the PyPI JSON +
            # wheel handlers and the github-shaped repo/contents/readme handlers).
            "MP_PYPI_BASE_URL": self._index_url,
            "MP_GITHUB_API_BASE": f"{self._index_url}/gh-api",
        }

    # ---- lifecycle -------------------------------------------------------

    def _bin(self) -> str:
        """The ``tai42-marketplace`` console script from the active venv (the
        editable sibling checkout installs it next to the interpreter)."""
        candidate = Path(sys.executable).parent / "tai42-marketplace"
        if not candidate.exists():
            raise RuntimeError(
                f"tai42-marketplace console script not found next to interpreter at {candidate}; "
                "the ../tai-marketplace sibling must be installed in the venv (uv sync)"
            )
        return str(candidate)

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
        """Run ``tai42-marketplace db init`` to completion against the empty DB.

        The production bootstrap path; it runs ``CREATE EXTENSION pg_trgm``, so
        the DB must be owned (created by ``create_empty_db``, not a template
        clone). A non-zero exit raises loudly with the captured output."""
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [self._bin(), "db", "init"],
            cwd=str(self.root),
            env=self._service_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        (self._logs_dir / "mp-db-init.log").write_text(proc.stdout + proc.stderr, encoding="utf-8")
        if proc.returncode != 0:
            raise RuntimeError(
                f"tai42-marketplace db init failed (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
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
    import importlib.metadata

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
