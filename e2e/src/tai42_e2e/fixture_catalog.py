"""Forged marketplace fixture artifacts and their identity: the pypi/github
fixture-plugin wheels + tarballs, the descriptor-only (source='spec') connector
templates, and the workspace-contract band every contract-bearing fixture is
stamped to."""

from __future__ import annotations

import importlib.metadata
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tai42_e2e.pkgsource import (
    BuiltTarball,
    BuiltWheel,
    build_fixture_source_tarball,
    build_fixture_wheel,
)

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
THETA_PACKAGE = "tai-e2e-market-theta"
ZETA_PACKAGE = "tai-e2e-market-zeta"
ETA_PACKAGE = "tai-e2e-market-eta"
ALPHA_REF = "tai42/e2e-alpha"
BETA_REF = "tai42/e2e-beta"
GAMMA_REF = "tai42/e2e-gamma"
DELTA_REF = "tai42/e2e-delta"
EPSILON_REF = "tai42/e2e-epsilon"
THETA_REF = "tai42/e2e-theta"
ZETA_REF = "tai42/e2e-zeta"
ETA_REF = "tai42/e2e-eta"

# Epsilon's second published version: a router item bumped to carry an additional
# declared PUBLIC route the 0.1.0 spec does not. Forged from its own source tree
# (``epsilon_v2``) rather than a version stamp of ``epsilon``, since a bump that
# ADDS a route must ship different module + spec bytes, not just a new version
# number. Same distribution (``tai-e2e-market-epsilon``) and ref (``EPSILON_REF``)
# as 0.1.0 — the update flow moves the one installed plugin onto it.
EPSILON_V2_VERSION = "0.2.0"

# Eta is the mcp-server fixture: its one provided item is kind ``mcp-server`` whose
# ``mcp.command`` launches the fixture's own one-tool stdio server. An mcp-server
# package imports no tai42-contract, so its spec declares NO contract range and the
# forge stamps none (:func:`forge_fixture_artifacts`). The install writes a manifest
# ``mcp`` entry titled by the item name, under which the mounted tool binds.
ETA_MCP_TITLE = "e2e_eta_mcp"
ETA_MCP_TOOL = "e2e_eta_mcp_ping"

# Zeta is the plugin-compat fixture: two published versions whose DECLARED
# contract ranges straddle the running tai42-contract major. 0.1.0 declares the
# wide range, which admits the running major and everything below it; 0.2.0
# declares the narrow future range — the next major up — which excludes the
# running contract. Both ranges are computed from ``RUNNING_CONTRACT_MAJOR`` — the
# major of the tai42-contract installed in this environment — so the bracket around
# the running contract holds at every contract major with no hand edit (asserted at
# seed time by ``assert_zeta_ranges_bracket_running_contract``). The wheels are
# forged per compat spec via :func:`forge_zeta_wheel`, never through
# ``forge_fixture_artifacts``.
RUNNING_CONTRACT_MAJOR = int(importlib.metadata.version("tai42-contract").split(".", 1)[0])
"""Major component of the tai42-contract installed in this environment; the origin
both zeta compat ranges are derived from."""
ZETA_COMPAT_VERSION = "0.1.0"
ZETA_INCOMPAT_VERSION = "0.2.0"
ZETA_WIDE_CONTRACT_RANGE = f">=0.1,<{RUNNING_CONTRACT_MAJOR + 1}"
"""Zeta 0.1.0's declared contract range. Its upper bound is the major above the
running contract, so the range admits the running contract major and every major
below it — the compat verdict for this version resolves as INSTALLABLE."""
ZETA_NARROW_CONTRACT_RANGE = f">={RUNNING_CONTRACT_MAJOR + 1},<{RUNNING_CONTRACT_MAJOR + 2}"
"""Zeta 0.2.0's declared contract range. It spans only the single major above the
running contract, so it excludes the running contract major — the compat verdict
for this version resolves as INCOMPATIBLE."""
# The module zeta's one tool item provides — the manifest config row an install
# persists ({"title": <module>, "module": <module>}) targets this module.
ZETA_TOOLS_MODULE = "tai_e2e_market_zeta.tools"

# Delta is the one github-sourced fixture: seeded repo-form against this URL (which
# its checked-in ``tai-plugin.yml`` declares), never via a wheel. The webhook-ingest
# spec matches its tag-push deliveries on this repository URL.
DELTA_REPOSITORY_URL = "https://github.com/tai42ai/tai-e2e-market-delta"

# ---- descriptor-only (source='spec') fixtures ---------------------------
#
# iota / kappa are yml-only connector descriptors: they ship NO package, so the forge
# builds no wheel and the registry classifies them ``source='spec'`` (the raw
# ``tai-plugin.yml`` at the tag is the artifact, its sha256 the ingest-time integrity
# digest). The served yml carries real values the model demands (an http(s) MCP url, a
# stdio interpreter path), which are only known once a stack allocates them, so each
# fixture ships as a ``tai-plugin.yml.tmpl`` template rendered PER STACK by
# :func:`render_descriptor_fixture`; the github-API stub serves the rendered text at the
# tag. The template is never published.

# iota — a yml-only OAuth connector over the harness OAuth/MCP stub. Standalone repo
# (``v<version>`` tags); two versions publish, 0.2.0 adding a scope.
IOTA_NAMESPACE = "tai42"
IOTA_NAME = "iota"
IOTA_REF = "tai42/iota"
IOTA_PROVIDER_ID = "iota"
IOTA_REPOSITORY_URL = "https://github.com/tai42ai/tai-e2e-market-iota"
IOTA_VERSION_V1 = "0.1.0"
IOTA_VERSION_V2 = "0.2.0"
IOTA_TAG_V1 = "v0.1.0"
IOTA_TAG_V2 = "v0.2.0"
IOTA_CLIENT_ID_ENV = "CONNECTORS_IOTA_CLIENT_ID"
IOTA_CLIENT_SECRET_ENV = "CONNECTORS_IOTA_CLIENT_SECRET"
# The single scope 0.1.0 grants and the extra scope 0.2.0 adds (the visible update delta).
IOTA_SCOPE_V1 = "read"
IOTA_SCOPE_V2_ADDED = "write"

# iota, seeded MONOREPO-style (the shape the shipped connectors use): a subdir listing
# with a ``<component>-v<version>`` tag whose component equals ``<namespace>-<name>``.
# A distinct plugin name so the tag component routes to it. The listing
# is seeded at 0.1.0 (registering it under the tree URL), then a 0.2.0 tag push is routed
# by its component and published by the webhook.
IOTA_MONOREPO_NAME = "connector-iota"
IOTA_MONOREPO_REF = "tai42/connector-iota"
IOTA_MONOREPO_REPOSITORY_URL = "https://github.com/tai42ai/tai42/tree/main/plugins/connector-iota"
# The repo-relative subpath the ``/tree/<branch>/<path>`` URL names — where the descriptor
# and its docs sit in the monorepo, the segment path the github docs-ingest walks.
IOTA_MONOREPO_SUBPATH = "plugins/connector-iota"
IOTA_MONOREPO_SEED_VERSION = "0.1.0"
IOTA_MONOREPO_SEED_TAG = "tai42-connector-iota-v0.1.0"
IOTA_MONOREPO_VERSION = "0.2.0"
IOTA_MONOREPO_TAG = "tai42-connector-iota-v0.2.0"

# kappa — a yml-only no-auth (``kind: none``) connector launching the managed stdio MCP
# server. One version; installs with NO env dialog, connects with user-supplied config.
KAPPA_NAMESPACE = "tai42"
KAPPA_NAME = "kappa"
KAPPA_REF = "tai42/kappa"
KAPPA_PROVIDER_ID = "kappa"
KAPPA_REPOSITORY_URL = "https://github.com/tai42ai/tai-e2e-market-kappa"
KAPPA_VERSION = "0.1.0"
KAPPA_TAG = "v0.1.0"
# The two client-supplied config fields (both target the stdio env channel). The managed
# server reflects each back through ``reflect_env``.
KAPPA_CONFIG_FIELD_REQUIRED = "kappa_alpha"
KAPPA_CONFIG_FIELD_SECRET = "kappa_beta"

# The ``{{TOKEN}}`` grammar the descriptor templates use (upper-snake names). Any token
# still present after substitution is a fixture bug, rejected loudly by the renderer.
_DESCRIPTOR_TOKEN_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


@dataclass(frozen=True)
class FixtureArtifacts:
    """The forged fixture artifacts a marketplace-area run needs: the pypi-sourced
    wheels (alpha 0.1.0/0.2.0, beta 0.1.0, gamma 0.1.0, epsilon 0.1.0/0.2.0,
    theta 0.1.0, eta 0.1.0) and the github-sourced delta source tarballs (0.1.0/0.2.0).
    Immutable and shareable across modules — forging is pure and the built files never
    change. ``epsilon_v2`` is the same distribution as ``epsilon_v1`` at a bumped
    version whose spec declares an extra public route (see ``EPSILON_V2_VERSION``);
    ``theta_v1`` is a router whose declared route shape-collides with epsilon's."""

    alpha_v1: BuiltWheel
    alpha_v2: BuiltWheel
    beta_v1: BuiltWheel
    gamma_v1: BuiltWheel
    epsilon_v1: BuiltWheel
    epsilon_v2: BuiltWheel
    theta_v1: BuiltWheel
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
    """Forge every fixture artifact into ``out_dir``: the eight wheels and the two
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
        epsilon_v2=build_fixture_wheel(src / "epsilon_v2", EPSILON_V2_VERSION, out_dir, contract_range=contract_range),
        theta_v1=build_fixture_wheel(src / "theta", "0.1.0", out_dir, contract_range=contract_range),
        eta_v1=build_fixture_wheel(src / "eta", "0.1.0", out_dir),
        delta_v1=build_fixture_source_tarball(src / "delta", "0.1.0", out_dir, contract_range=contract_range),
        delta_v2=build_fixture_source_tarball(src / "delta", "0.2.0", out_dir, contract_range=contract_range),
    )


# ---- descriptor templating (source='spec') ------------------------------


def render_descriptor_fixture(tmpl_path: Path, values: Mapping[str, str]) -> bytes:
    """Render a descriptor-only fixture template to the exact ``tai-plugin.yml`` bytes
    the github-API stub serves and the registry digests (``sha256``, ``source='spec'``).

    Substitutes every ``{{TOKEN}}`` from ``values`` (e.g. ``IDP_BASE_URL`` — the
    per-stack OAuth/MCP stub base a sub-service/oauth endpoint points at; ``PYTHON`` —
    the interpreter a stdio sub-service launches with; plus the listing's identity
    tokens). The values differ per test stack, so a template is rendered PER STACK,
    never once and shared. ANY ``{{...}}`` token still present after substitution raises
    loudly — a served descriptor must carry no placeholder (the model rejects a
    non-http(s) MCP url and a non-https icon), so an unrendered token is a fixture bug,
    never shipped. Returns UTF-8 bytes; the registry's ``sha256`` is taken over exactly
    these bytes and the raw yml URL at the tag is the install pointer."""
    text = tmpl_path.read_text(encoding="utf-8")
    for token, value in values.items():
        text = text.replace(f"{{{{{token}}}}}", value)
    remaining = sorted(set(_DESCRIPTOR_TOKEN_RE.findall(text)))
    if remaining:
        raise RuntimeError(
            f"descriptor template {tmpl_path} still carries unrendered token(s) {remaining} "
            f"after substitution with {sorted(values)}; a served descriptor must carry no placeholder"
        )
    return text.encode("utf-8")


def _descriptor_template(fixture: str, fixtures_dir: Path | None = None) -> Path:
    """The ``tai-plugin.yml.tmpl`` of a descriptor-only fixture dir under the resolved
    marketplace fixtures tree (``fixtures_dir`` / ``TAI_E2E_MARKETPLACE_FIXTURES`` /
    in-repo default). A missing template raises loudly — never a silent empty render."""
    path = _resolve_fixtures_dir(fixtures_dir) / fixture / "tai-plugin.yml.tmpl"
    if not path.is_file():
        raise RuntimeError(f"descriptor template {path} does not exist")
    return path


def _descriptor_docs(fixture: str, fixtures_dir: Path | None = None) -> dict[str, str]:
    """The docs tree a descriptor-only fixture publishes, keyed docs-relative
    (``index.mdx`` -> its text) — the shape the registry's github docs-ingest fetches
    over the tree/blob surfaces. Every published version must carry its docs index."""
    docs_dir = _resolve_fixtures_dir(fixtures_dir) / fixture / "docs"
    if not docs_dir.is_dir():
        raise RuntimeError(f"descriptor docs dir {docs_dir} does not exist")
    return {
        p.relative_to(docs_dir).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(docs_dir.rglob("*"))
        if p.is_file()
    }


def render_iota_descriptor(idp_base_url: str, version: str, *, name: str = IOTA_NAME) -> str:
    """Render the iota OAuth-connector descriptor for ``version`` against ``idp_base_url``.

    ``0.1.0`` grants the single ``read`` scope; ``0.2.0`` adds ``write`` (the visible
    update delta). ``name`` overrides the top-level plugin name for the monorepo-style
    listing (its component tag routes to ``<namespace>-<name>``); the provider id stays
    ``iota`` regardless. The MCP/oauth endpoints resolve to the stack's OAuth/MCP stub.

    The declared ``contract`` range is stamped to the workspace's own contract band
    (:func:`_workspace_contract_range`), exactly as the wheel forge stamps every
    contract-bearing fixture: the registry stores the descriptor's declared range and
    returns it at resolve, where the installer gates it against the running
    ``tai42-contract`` — a static range would go stale the moment a release window moved
    the workspace contract past it."""
    scope_lines = [IOTA_SCOPE_V1] if version == IOTA_VERSION_V1 else [IOTA_SCOPE_V1, IOTA_SCOPE_V2_ADDED]
    scopes = "\n".join(f"            - {scope}" for scope in scope_lines)
    values = {
        "IDP_BASE_URL": idp_base_url.rstrip("/"),
        "NAME": name,
        "VERSION": version,
        "SCOPES": scopes,
        "CONTRACT": _workspace_contract_range(),
    }
    return render_descriptor_fixture(_descriptor_template("iota"), values).decode("utf-8")


def render_kappa_descriptor(python: str) -> str:
    """Render the kappa no-auth-connector descriptor, launching the managed stdio MCP
    server with ``python`` (the SUT interpreter that already has ``tai42_e2e_fixtures``).

    The declared ``contract`` range is stamped to the workspace band
    (:func:`_workspace_contract_range`) like :func:`render_iota_descriptor`, so the
    installer's resolve-time contract gate tracks the running ``tai42-contract``."""
    values = {"PYTHON": python, "CONTRACT": _workspace_contract_range()}
    return render_descriptor_fixture(_descriptor_template("kappa"), values).decode("utf-8")
