"""The tai42-marketplace registry as a first-class harness resource.

The registry is OUR service under test, so it boots the way the skeleton SUT
does — REAL OS processes spawned from the shared venv via
:class:`~tai42_e2e.procs.ProcessHandle`, from the out-of-band pinned install, with
log files and a leak-checked teardown — not a net-fixture thread. A
:class:`MarketplaceService` owns an isolated Postgres database (created empty and
schema-bootstrapped through the production ``tai42-marketplace db migrate`` path,
which replays the migration chain — its baseline runs ``CREATE EXTENSION
pg_trgm``), spawns the API server, and waits for ``GET /healthz`` == 200.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from tai42_e2e import ports
from tai42_e2e.httpapi import ApiClient
from tai42_e2e.procs import ProcessHandle
from tai42_e2e.waiting import wait_for

if TYPE_CHECKING:
    from tai42_e2e.settings import HarnessSettings
    from tai42_e2e.topology import Infra


# The registry lives in its own private repo, kept out of the monorepo lock;
# the ``tai42-marketplace`` package sits in that repo's ``api/`` subdirectory, so
# the install spec targets it with ``#subdirectory=api``. The opt-in marketplace
# suite installs it out-of-band at boot from this pinned ref (the git insteadOf
# token config rewrites the URL — no token handling here).
_MARKETPLACE_GIT_URL = "https://github.com/tai42ai/tai-marketplace"
# The pinned registry commit MUST carry the framework migration runner
# (``db migrate``/``db status``) — :meth:`MarketplaceService._apply_ddl` drives
# that CLI path and nothing older. An unresolvable pin fails the out-of-band
# install loudly rather than silently resolving an older ``db init``-only ref.
#
# This pin runs the contract-4.x-era tai-marketplace: it carries the whole ingest
# surface the marketplace suite drives — the descriptor-only (``source='spec'``)
# listings branch (package-optional PluginSpec + connector kind), the mcp-server
# ingest branch (contract-less ``mcp-server``-kind items), and the github
# docs-ingest over the git-data ``/git/trees`` + ``/git/blobs`` surfaces — plus the
# declared-plugin-routes surface the pre-4 pin already had. The api pins
# tai42-contract >=4.1,<5 + tai42-kit[postgres] >=3.5,<4, resolving contract 4.2.0
# against the v4 fleet, so the registry accepts route-carrying fixture specs at
# seed time and the route-marketplace legs RUN (they gate on
# :func:`registry_supports_declared_routes`, satisfied here).
_MARKETPLACE_PIN = "2c22ee1ee2fc65696608fc5f1888dce2497145f3"

# The first tai42-contract major whose PluginSpec accepts a declared ``routes``
# field (contract 2.0). A registry venv below it rejects route-carrying specs.
_ROUTES_CAPABLE_CONTRACT_MAJOR = 2

# The fleet-e2e workflow DISPATCHES the source sha under test here when a
# tai-marketplace push drives the run, so the harness boots the registry at the
# exact commit being validated instead of the checked-in pin. Unset (the normal
# monorepo run), the pin governs.
_MARKETPLACE_REF_ENV = "TAI_E2E_MARKETPLACE_REF"
# A dispatched ref is a full 40-hex git commit sha; the workflow already applies
# this same shape guard before exporting it, and this resolver re-checks so a
# malformed value fails loudly here rather than surfacing as an opaque
# ``git+…@<garbage>`` install error deep in ``_install_marketplace``.
_MARKETPLACE_SHA_RE = r"[0-9a-f]{40}"


def _marketplace_ref() -> str:
    """The registry commit the out-of-band install resolves and the dedicated venv
    is keyed by: the dispatched ``TAI_E2E_MARKETPLACE_REF`` when set (the fleet
    workflow exports the source sha under test), else the checked-in
    ``_MARKETPLACE_PIN``.

    A dispatched value MUST be a full 40-char lowercase-hex commit sha
    (:data:`_MARKETPLACE_SHA_RE`); anything else raises loudly, naming the env var
    and the expected shape, so a mangled dispatch fails here rather than as an
    opaque git-resolve error at install time."""
    override = os.environ.get(_MARKETPLACE_REF_ENV)
    if override is None:
        return _MARKETPLACE_PIN
    if re.fullmatch(_MARKETPLACE_SHA_RE, override) is None:
        raise RuntimeError(
            f"{_MARKETPLACE_REF_ENV}={override!r} is not a full 40-char lowercase-hex commit sha "
            f"(expected /{_MARKETPLACE_SHA_RE}/); the fleet workflow exports the dispatched source sha"
        )
    return override


def _registry_venv_dir() -> Path:
    """The DEDICATED venv the pinned registry installs into — a per-checkout,
    per-ref directory under this e2e member (alongside its workspace ``.venv``),
    built once and reused across modules and sessions.

    Keyed by the RESOLVED registry ref (:func:`_marketplace_ref`, the dispatched
    ``TAI_E2E_MARKETPLACE_REF`` or the checked-in pin), first 12 chars: a dispatched
    ref boots into its own venv rather than reusing a stale pin-keyed one, and a
    later run at a different ref never collides with it.

    Per-checkout, not host-global: a shared system-temp directory lets concurrent
    runs on separate checkouts race on ``uv venv --clear``, one wiping another's
    live registry. Keying off this package's own location isolates each checkout.

    The registry must NEVER share the SUT's workspace venv. Its pinned dependency
    caps (tai42-contract / tai42-kit) are point-in-time: in a release-PR window
    where the workspace has moved a first-party package past a registry cap, a
    shared-venv install would DOWNGRADE that workspace package from PyPI and the
    skeleton would then abort boot on its own routers. A separate venv keeps
    the registry's dependency resolution wholly apart from the SUT's."""
    return Path(__file__).resolve().parents[2] / f".tai42-e2e-marketplace-{_marketplace_ref()[:12]}"


def _registry_python() -> Path:
    return _registry_venv_dir() / "bin" / "python"


def _registry_bin() -> Path:
    return _registry_venv_dir() / "bin" / "tai42-marketplace"


def registry_supports_declared_routes() -> bool:
    """Whether the pinned registry can accept a PluginSpec that declares ``routes``
    — i.e. its DEDICATED venv runs tai42-contract >= 2 (contract 2.0's PluginSpec
    is the first with a ``routes`` field; older contracts reject it as an extra
    input at seed time).

    Gates every e2e leg that admin-seeds a route-carrying fixture (epsilon /
    epsilon_v2 / theta) into the registry. The resolved ref (:func:`_marketplace_ref`)
    runs a routes-capable tai-marketplace today (``_MARKETPLACE_PIN`` 2c22ee1 resolves
    tai42-contract 4.x, whose PluginSpec carries ``routes``), so this is TRUE in a
    normal run and those legs RUN. The gate remains as a guard, not a release-window
    skip: it degrades to skip only when the registry venv is not yet built (lazy
    install), and — when a ref is DISPATCHED via ``TAI_E2E_MARKETPLACE_REF`` — a False
    verdict is escalated to a hard failure by the skip helper / browser guard (see
    :func:`declared_routes_dispatch_failure`), because a dispatched ref that cannot
    seed routes is a regression, not an expected window. A plain importable predicate —
    pair it with ``skip_unless_registry_supports_declared_routes`` for the pytest skip,
    and reuse it from any future route-declaring spec (e.g. a reload-probe leg).

    Reads the tai42-contract version installed in the REGISTRY venv
    (:func:`_registry_python`), never the SUT's workspace contract — the registry's
    contract is point-in-time and independent of the SUT's. The registry install is
    lazy, so when that venv is not yet built this returns ``False`` (degrade to
    skip, never crash collection); the registry-booting fixtures build the venv
    before any gated leg runs, so in CI this reads the real installed version.
    Raises loudly only if the venv exists but its contract version cannot be read or
    parsed — never a silent swallow."""
    from packaging.version import InvalidVersion, Version

    py = _registry_python()
    if not py.exists():
        return False
    proc = subprocess.run(
        [str(py), "-c", "import importlib.metadata as m; print(m.version('tai42-contract'))"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"reading tai42-contract from the registry venv {py} failed "
            f"(exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )
    raw = proc.stdout.strip()
    try:
        version = Version(raw)
    except InvalidVersion as exc:
        raise RuntimeError(f"registry venv reported an unparseable tai42-contract version {raw!r}") from exc
    return version.major >= _ROUTES_CAPABLE_CONTRACT_MAJOR


def declared_routes_dispatch_failure() -> str | None:
    """The skip-vs-FAIL decision for a route-carrying leg when
    :func:`registry_supports_declared_routes` gated ``False``.

    Returns ``None`` when NO registry ref was dispatched (``TAI_E2E_MARKETPLACE_REF``
    unset) — the normal monorepo run, where a False gate is a legitimate
    release-window skip (the registry venv may not be built yet, or the checked-in
    pin genuinely predates the route surface). The caller SKIPS.

    Returns a LOUD dual-cause message when a ref WAS dispatched — the caller must
    FAIL, never skip, because the fleet dispatched a specific tai-marketplace sha to
    validate and a False gate means one of two real defects, which the message
    distinguishes:

    * the registry venv is ABSENT — a harness ordering bug: the gated leg ran before
      the registry booted, so the gate read a not-yet-installed venv rather than a
      real capability verdict; vs
    * the venv is built but runs tai42-contract major below the routes floor — the
      dispatched marketplace ref REGRESSED its contract floor, dropping the ``routes``
      surface every declared-routes leg requires."""
    ref = os.environ.get(_MARKETPLACE_REF_ENV)
    if ref is None:
        return None
    if not _registry_python().exists():
        return (
            f"registry venv {_registry_venv_dir()} is absent while marketplace ref {ref} was "
            "dispatched via TAI_E2E_MARKETPLACE_REF: harness ordering bug — the gated leg ran "
            "before the registry booted, so the declared-routes gate read a not-yet-built venv"
        )
    return (
        f"the dispatched marketplace ref {ref} regressed its contract floor: its registry venv "
        f"runs tai42-contract major < {_ROUTES_CAPABLE_CONTRACT_MAJOR}, so the PluginSpec ``routes`` "
        "surface every declared-routes leg needs is gone"
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
        """Create the dedicated registry venv and ``uv pip install`` tai42-marketplace
        at the resolved ref (:func:`_marketplace_ref` — the dispatched
        ``TAI_E2E_MARKETPLACE_REF`` or the checked-in pin) into it — apart from the
        SUT's workspace venv, so the registry's own dependency caps never mutate the
        workspace's first-party packages (the git insteadOf token config rewrites the
        URL — no token handling here). Raises loudly on a non-zero exit with the
        captured output."""
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
        spec = f"tai42-marketplace @ git+{_MARKETPLACE_GIT_URL}@{_marketplace_ref()}#subdirectory=api"
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
