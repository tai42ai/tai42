"""The registry seeding pipeline: stage forged fixture artifacts on the package
index and drive the marketplace's real admin-seed + ingest until each version
publishes — the shared browse catalog plus the per-scenario listings (zeta compat,
epsilon/theta routers, eta mcp-server, iota/kappa descriptors)."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Sequence
from pathlib import Path

from tai42_e2e.fixture_catalog import (
    ALPHA_PACKAGE,
    ALPHA_REF,
    BETA_PACKAGE,
    BETA_REF,
    EPSILON_PACKAGE,
    EPSILON_REF,
    EPSILON_V2_VERSION,
    ETA_PACKAGE,
    ETA_REF,
    GAMMA_PACKAGE,
    GAMMA_REF,
    IOTA_MONOREPO_NAME,
    IOTA_MONOREPO_REF,
    IOTA_MONOREPO_REPOSITORY_URL,
    IOTA_MONOREPO_SEED_TAG,
    IOTA_MONOREPO_SEED_VERSION,
    IOTA_MONOREPO_SUBPATH,
    IOTA_REF,
    IOTA_REPOSITORY_URL,
    IOTA_TAG_V1,
    IOTA_TAG_V2,
    IOTA_VERSION_V1,
    IOTA_VERSION_V2,
    KAPPA_REF,
    KAPPA_REPOSITORY_URL,
    KAPPA_TAG,
    KAPPA_VERSION,
    THETA_PACKAGE,
    THETA_REF,
    ZETA_NARROW_CONTRACT_RANGE,
    ZETA_PACKAGE,
    ZETA_REF,
    ZETA_WIDE_CONTRACT_RANGE,
    FixtureArtifacts,
    _descriptor_docs,
    _resolve_fixtures_dir,
    render_iota_descriptor,
    render_kappa_descriptor,
)
from tai42_e2e.marketplace import MarketplaceService
from tai42_e2e.pkgsource import BuiltWheel, FixturePackageIndex, build_fixture_wheel
from tai42_e2e.waiting import wait_for_async

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


async def seed_epsilon_v2_listing(
    mp: MarketplaceService, index: FixturePackageIndex, artifacts: FixtureArtifacts
) -> None:
    """Stage epsilon's bumped ``0.2.0`` wheel and publish it through the real
    admin-seed + ingest pipeline, so the same listing carries two versions.

    Publishes ON TOP of :func:`seed_epsilon_listing`'s ``0.1.0`` (the synchronous
    seed publishes each version in its own request, no background poller), so the
    route-mounting update spec can install ``0.1.0`` and then move onto the ``0.2.0``
    whose spec declares a route the older version did not."""
    index.register(artifacts.epsilon_v2)
    await _admin_seed(mp, ((EPSILON_PACKAGE, artifacts.epsilon_v2.plugin_yml),))
    await _wait_published(mp, EPSILON_REF, EPSILON_V2_VERSION)


async def seed_theta_listing(mp: MarketplaceService, index: FixturePackageIndex, artifacts: FixtureArtifacts) -> None:
    """Stage theta's ``0.1.0`` wheel and drive the real admin-seed + ingest pipeline
    until it publishes.

    Theta is the route-collision fixture the route-mounting spec installs after
    epsilon: its declared ``GET /{slug}`` template overlaps epsilon's concrete
    ``/ping`` / ``/open`` GET routes at the shared default base, so the second install
    is a collision until the operator remaps theta's base. Kept OUT of
    :func:`seed_fixture_catalog` so it never pollutes the shared browse catalog —
    only the route-mounting spec's own registry carries it, exactly as
    delta/epsilon/eta are seeded by their own specs rather than the shared seed."""
    index.register(artifacts.theta_v1)
    await _admin_seed(mp, ((THETA_PACKAGE, artifacts.theta_v1.plugin_yml),))
    await _wait_published(mp, THETA_REF, "0.1.0")


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


# ---- descriptor-only (source='spec') seeding ----------------------------


async def _seed_descriptor_version(
    mp: MarketplaceService,
    index: FixturePackageIndex,
    *,
    ref: str,
    repository_url: str,
    tag: str,
    plugin_yml: str,
    version: str,
    docs: dict[str, str],
    docs_subpath: str = "",
    default_branch: bool = False,
) -> None:
    """Stage one descriptor version on the github surfaces and publish it repo-form
    through the real admin-seed + ingest pipeline.

    A descriptor-only seed carries NO package: the entry is a REPO entry
    (``{"repo", "plugin_yml", "tag"}``), the ingest sees ``spec.package is None`` and
    classifies it ``source='spec'`` — the raw yml at the tag is the artifact, its
    sha256 the digest of the served bytes; no PyPI lookup, no wheel gate. The rendered
    yml is served at the tag (``default_branch`` also serves it ref-less for a subdir
    listing's seed-time existence probe) and its docs tree over the tree/blob
    surfaces (a published version must carry its docs index). Raises on a failed row."""
    index.register_github_release(tag, plugin_yml, default_branch=default_branch)
    index.register_github_docs_tree(tag, docs, subpath=docs_subpath)
    seeded = await mp.api.post(
        "/api/v1/admin/seed",
        json={"repos": [{"repo": repository_url, "plugin_yml": plugin_yml, "tag": tag}]},
        headers=mp.admin_headers,
    )
    row = seeded["results"][0]
    if row.get("status") != "published":
        raise RuntimeError(f"descriptor seed for {ref}@{version} failed: {row}")
    await _wait_published(mp, ref, version)


async def seed_iota_listing(
    mp: MarketplaceService,
    index: FixturePackageIndex,
    idp_base_url: str,
    *,
    versions: Sequence[str] = (IOTA_VERSION_V1, IOTA_VERSION_V2),
) -> None:
    """Publish the standalone iota OAuth-connector descriptor for each of ``versions``
    (in order) through the real admin-seed + ingest pipeline, rendered against
    ``idp_base_url`` (the stack's OAuth/MCP stub). ``0.2.0`` adds a scope over ``0.1.0``.

    Kept OUT of :func:`seed_fixture_catalog` like every other non-browse fixture, so the
    shared alpha/beta/gamma browse catalog is unpolluted; the descriptor specs select
    this seed themselves. Pass ``versions=(IOTA_VERSION_V1,)`` to stage only 0.1.0 before
    an update-to-0.2.0 leg (the synchronous seed publishes each version in its request,
    with no background poller detecting a newer release)."""
    docs = _descriptor_docs("iota")
    tags = {IOTA_VERSION_V1: IOTA_TAG_V1, IOTA_VERSION_V2: IOTA_TAG_V2}
    for version in versions:
        yml = render_iota_descriptor(idp_base_url, version)
        await _seed_descriptor_version(
            mp,
            index,
            ref=IOTA_REF,
            repository_url=IOTA_REPOSITORY_URL,
            tag=tags[version],
            plugin_yml=yml,
            version=version,
            docs=docs,
        )


async def seed_iota_monorepo_listing(mp: MarketplaceService, index: FixturePackageIndex, idp_base_url: str) -> None:
    """Publish the MONOREPO-style iota listing (name ``connector-iota``) at its 0.1.0
    seed tag, registering it under the ``/tree/<branch>/<path>`` URL so a later
    ``tai42-connector-iota-v<version>`` tag push routes to it by its component.

    The subdir listing's seed confirms the ``tai-plugin.yml`` exists under the repo path
    (a ref-less contents fetch), so the rendered yml is staged as the default-branch
    content too. Its provider id stays ``iota`` — only the top-level plugin name differs
    (the routing key)."""
    yml = render_iota_descriptor(idp_base_url, IOTA_MONOREPO_SEED_VERSION, name=IOTA_MONOREPO_NAME)
    await _seed_descriptor_version(
        mp,
        index,
        ref=IOTA_MONOREPO_REF,
        repository_url=IOTA_MONOREPO_REPOSITORY_URL,
        tag=IOTA_MONOREPO_SEED_TAG,
        plugin_yml=yml,
        version=IOTA_MONOREPO_SEED_VERSION,
        docs=_descriptor_docs("iota"),
        docs_subpath=IOTA_MONOREPO_SUBPATH,
        default_branch=True,
    )


async def seed_kappa_listing(mp: MarketplaceService, index: FixturePackageIndex, python: str) -> None:
    """Publish the kappa no-auth (``kind: none``) connector descriptor through the real
    admin-seed + ingest pipeline, launching the managed stdio MCP server with ``python``.

    Kept OUT of :func:`seed_fixture_catalog` like the other descriptor fixtures — the
    kappa spec seeds the fixture it installs itself."""
    await _seed_descriptor_version(
        mp,
        index,
        ref=KAPPA_REF,
        repository_url=KAPPA_REPOSITORY_URL,
        tag=KAPPA_TAG,
        plugin_yml=render_kappa_descriptor(python),
        version=KAPPA_VERSION,
        docs=_descriptor_docs("kappa"),
    )
