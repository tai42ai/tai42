"""Fixtures for the opt-in marketplace test area.

Scoped to the gated directory so nothing marketplace-flavored is imported at
collection when ``TAI_E2E_MARKETPLACE`` is unset (``tests/conftest.py`` adds
``marketplace/*`` to ``collect_ignore_glob``, so this file never loads).

The area boots one registry + one skeleton stack per module: the forged fixture
artifacts are session-shared (forging is pure and expensive), but the package
index, registry, and stack reset per module so the staged ``0.1.0``→``0.2.0``
publish dance starts against a clean, empty catalog every time.

``pip install`` mutates the ONE shared venv, which no per-stack isolation can
scope, so :func:`_venv_state_guard` asserts the fixture distributions are absent
before the area runs AND after it finishes, raising loudly (never auto-cleaning)
on a leftover.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import subprocess
import sys
from collections.abc import Iterator

import pytest

from tai42_e2e.booting import boot_stack
from tai42_e2e.manifests import (
    build_marketplace_prefix_stack,
    build_marketplace_stack,
    build_router_merge_stack,
)
from tai42_e2e.marketplace import (
    ALPHA_PACKAGE,
    BETA_PACKAGE,
    DELTA_PACKAGE,
    EPSILON_PACKAGE,
    GAMMA_PACKAGE,
    ZETA_COMPAT_VERSION,
    ZETA_INCOMPAT_VERSION,
    ZETA_NARROW_CONTRACT_RANGE,
    ZETA_PACKAGE,
    ZETA_WIDE_CONTRACT_RANGE,
    FixtureArtifacts,
    MarketplaceService,
    assert_zeta_ranges_bracket_running_contract,
    forge_fixture_artifacts,
    forge_zeta_wheel,
    seed_fixture_catalog,
    seed_zeta_listing,
)
from tai42_e2e.pkgsource import BuiltWheel, FixturePackageIndex
from tai42_e2e.stack import Infra, TaiStack

# The fixture distributions a marketplace install pip-installs into the shared
# venv. The venv guard asserts none of them linger before or after the area.
_FIXTURE_DISTRIBUTIONS = (ALPHA_PACKAGE, BETA_PACKAGE, GAMMA_PACKAGE, DELTA_PACKAGE, EPSILON_PACKAGE, ZETA_PACKAGE)


def _assert_fixture_distributions_absent() -> None:
    """Raise if any fixture distribution resolves in the venv. Distribution
    lookup re-scans sys.path per call and the harness process never imports the
    fixture packages, so the check is accurate."""
    for name in _FIXTURE_DISTRIBUTIONS:
        try:
            importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        raise RuntimeError(
            f"fixture distribution {name} is installed in the venv (leftover from an aborted run?); "
            f"remove it with: uv pip uninstall {name}"
        )


@pytest.fixture(scope="session", autouse=True)
def _venv_state_guard() -> Iterator[None]:
    """Assert the fixture distributions are ABSENT before the first marketplace
    test and after the last — every lifecycle spec is responsible for its own
    uninstall, so a leftover here is a real leak, raised loudly, never
    auto-cleaned."""
    _assert_fixture_distributions_absent()
    yield
    _assert_fixture_distributions_absent()


@pytest.fixture(scope="session", autouse=True)
def _pip_preflight() -> None:
    """Assert ``{python} -m pip --version`` exits 0 up front.

    A uv-synced venv ships no pip of its own; it is present here only because
    tai42-skeleton declares ``pip>=25`` as a runtime dependency, which the editable
    sibling carries into the shared venv. A failure means that runtime dep was
    dropped — a regression in the installer's own contract, named loudly rather
    than papered over by adding pip to this repo's deps."""
    proc = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            "the venv has no importable 'pip' module (`python -m pip --version` failed); the marketplace "
            "installer shells out to `sys.executable -m pip`, which relies on tai42-skeleton's `pip>=25` runtime "
            f"dependency. Restore that dependency rather than adding pip to tai42-e2e.\n{proc.stdout}\n{proc.stderr}"
        )


@pytest.fixture(scope="session")
def fixture_artifacts(tmp_path_factory: pytest.TempPathFactory) -> FixtureArtifacts:
    """Forge the five wheels (alpha 0.1.0/0.2.0, beta 0.1.0, gamma 0.1.0, epsilon
    0.1.0) and the two delta source tarballs (0.1.0/0.2.0) once per session — the
    artifacts are immutable and shared across modules."""
    return forge_fixture_artifacts(tmp_path_factory.mktemp("artifacts"))


@pytest.fixture(scope="session")
def zeta_compat_wheels(tmp_path_factory: pytest.TempPathFactory) -> tuple[BuiltWheel, BuiltWheel]:
    """Forge zeta's two compat wheels once per session: ``0.1.0`` declaring the
    wide contract range (contains the running contract) and ``0.2.0`` declaring
    the narrow future range (excludes it), each in lockstep with its built
    ``Requires-Dist``. Immutable and shared across modules like the other forged
    artifacts."""
    out = tmp_path_factory.mktemp("zeta-artifacts")
    return (
        forge_zeta_wheel(ZETA_COMPAT_VERSION, out, contract_range=ZETA_WIDE_CONTRACT_RANGE),
        forge_zeta_wheel(ZETA_INCOMPAT_VERSION, out, contract_range=ZETA_NARROW_CONTRACT_RANGE),
    )


@pytest.fixture(scope="module")
def zeta_catalog(
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    zeta_compat_wheels: tuple[BuiltWheel, BuiltWheel],
) -> tuple[BuiltWheel, BuiltWheel]:
    """Publish the zeta compat listing (0.1.0 wide, 0.2.0 narrow) into this
    module's registry through the real admin-seed + ingest pipeline, and return
    the two wheels. Asserts the compat bracket against the installed
    tai42-contract first, so a spec never runs against a contract version the
    fixture ranges do not bracket. Requested only by the compat specs — the
    shared browse catalog stays alpha/beta/gamma."""
    assert_zeta_ranges_bracket_running_contract()
    asyncio.run(seed_zeta_listing(marketplace_service, package_index, zeta_compat_wheels))
    return zeta_compat_wheels


@pytest.fixture(scope="module")
def package_index() -> Iterator[FixturePackageIndex]:
    """A fresh, empty package index per module. Registration is staged by
    ``seed_fixture_catalog`` (and the webhook-ingest spec) against this module's
    own registry, so a per-module index keeps the staged publish order clean."""
    index = FixturePackageIndex()
    index.start()
    try:
        yield index
    finally:
        index.stop()


@pytest.fixture(scope="module")
def marketplace_service(
    infra: Infra,
    tmp_path_factory: pytest.TempPathFactory,
    package_index: FixturePackageIndex,
    fixture_artifacts: FixtureArtifacts,
) -> Iterator[MarketplaceService]:
    """Boot the registry (the API server on an isolated PG DB) and seed the
    pypi-sourced fixture catalog through the real admin-seed + ingest pipeline.
    Module-scoped like every stack fixture, so a ``stop()``/``start()`` outage
    inside one module never leaks into another."""
    root = tmp_path_factory.mktemp("marketplace-registry")
    mp = MarketplaceService(infra, root, index_url=package_index.url)
    try:
        mp.boot()
        asyncio.run(seed_fixture_catalog(mp, package_index, fixture_artifacts))
    except BaseException:
        # Reclaim resources on a boot/seed failure, but suppress teardown's own
        # secondary error: teardown aggregates-and-raises on any leak, which would
        # otherwise replace the original boot/seed failure in this double-fault
        # window. The original error must propagate.
        with contextlib.suppress(Exception):
            mp.teardown()
        raise
    try:
        yield mp
    finally:
        mp.teardown()


@pytest.fixture(scope="module")
def marketplace_stack(
    infra: Infra,
    tmp_path_factory: pytest.TempPathFactory,
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
) -> Iterator[TaiStack]:
    """A skeleton stack wired at the harness-run registry: the marketplace router,
    a short advisories poll, the attribution store on the stack's own PG clone,
    and the fixture package index the installer resolves wheels from."""
    resource_kwargs = {
        "marketplace_url": marketplace_service.base_url,
        "package_index_url": package_index.url,
    }
    yield from boot_stack(
        infra,
        tmp_path_factory.mktemp("marketplace"),
        build_marketplace_stack,
        resource_kwargs=resource_kwargs,
    )


@pytest.fixture(scope="module")
def marketplace_prefix_stack(
    infra: Infra,
    tmp_path_factory: pytest.TempPathFactory,
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
) -> Iterator[TaiStack]:
    """The marketplace stack with a persistent plugin prefix — the home of the
    restart-survival spec. Wired at the harness-run registry + fixture index exactly
    like ``marketplace_stack`` so its install door resolves and installs the fixture
    wheel, but with ``TAI_PLUGINS_PREFIX`` set so the install lands under the stack
    root (never the shared venv) and survives a serve restart."""
    resource_kwargs = {
        "marketplace_url": marketplace_service.base_url,
        "package_index_url": package_index.url,
    }
    yield from boot_stack(
        infra,
        tmp_path_factory.mktemp("marketplace-prefix"),
        build_marketplace_prefix_stack,
        resource_kwargs=resource_kwargs,
    )


@pytest.fixture(scope="module")
def router_merge_stack(
    infra: Infra,
    tmp_path_factory: pytest.TempPathFactory,
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
) -> Iterator[TaiStack]:
    """The marketplace stack with the SPA catch-all router mounted LAST — the home
    of the plugin-router/middleware auto-merge spec. Wired at the harness-run
    registry + fixture index exactly like ``marketplace_stack`` so its install door
    resolves and installs the epsilon fixture wheel."""
    resource_kwargs = {
        "marketplace_url": marketplace_service.base_url,
        "package_index_url": package_index.url,
    }
    yield from boot_stack(
        infra,
        tmp_path_factory.mktemp("router-merge"),
        build_router_merge_stack,
        resource_kwargs=resource_kwargs,
    )
