"""Contract-compat verdicts for installed plugin distributions.

The one place the "does this installed plugin run against the running
``tai42-contract``?" question is answered. A plugin distribution declares its
supported contract line as an ordinary ``Requires-Dist: tai42-contract<range>``
in its packaging metadata, so the verdict needs no registry call and no stored
state — it is read from the installed dist via :mod:`importlib.metadata` at
evaluation time, which is what makes it correct even for a dist installed
BEFORE a core upgrade (the stale-plugin case this mechanism exists for).

Verdict semantics (:class:`CompatVerdict`):

* ``compatible`` — the dist declares a ``tai42-contract`` requirement and the
  running version satisfies it. ``prereleases=True`` everywhere, so a
  dev-versioned editable contract checkout is never spuriously refused.
* ``incompatible`` — declared and NOT satisfied. The reason names both versions
  and the remedy, because it becomes the boot-abort message the
  operator acts on.
* ``unknown`` — no verdict is derivable: the module maps to no installed dist,
  the dist has no metadata, or it declares no ``tai42-contract`` requirement.
  Treated as compatible (a plugin that predates the declaration must keep
  loading), but the caller logs a note — never a silent pass.

The same verdict is given for a DECLARED range — an install-store row's
``spec.contract`` (:func:`declared_compat`) — in the same words. The plugin layer
build leaves out every plugin whose declared range the running contract is
outside and records it in the layer's build record; the import seam's snapshot
(:func:`distribution_map`) carries those records, so a module of a plugin left
out reads ``incompatible`` with the recorded reason. A range the registry serves —
the resolve response's ``contract_range`` the installer checks before an install, and
each published version's range the update picture is computed from — is judged by
:func:`registry_range_admits` with the same semantics.

A requirement line gated behind an extra (``; extra == "dev"``) is not part of
the runtime dependency set and is skipped; a malformed requirement line in a
dist's metadata cannot be interpreted and is skipped with a warning (only the
``tai42-contract`` line matters here, and refusing to boot over a stranger's
garbled metadata line would be the crash-loop this module removes).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from importlib import metadata
from logging import getLogger
from typing import Any, Literal

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from tai42_skeleton.marketplace.environments.current_record import current_record
from tai42_skeleton.marketplace.environments.findings import LAYER_SUBJECT
from tai42_skeleton.marketplace.environments.layout import layer_dir
from tai42_skeleton.marketplace.environments.settings import configured_plugins_dir
from tai42_skeleton.marketplace.errors import CurrentRecordError, LocalStateError, RegistryResponseError
from tai42_skeleton.marketplace.installed_packages import (
    InstalledDistribution,
    InstalledPackages,
    LeftOutModule,
    installed_packages,
)

logger = getLogger(__name__)

# The canonical dist name whose requirement line carries a plugin's supported
# contract range — the same dist whose running version is checked against it.
_CONTRACT_DIST = "tai42-contract"


class CorePluginBootError(RuntimeError):
    """A manifest-declared plugin is contract-incompatible, requires a platform feature that is off, or cannot load.

    Raised to ABORT boot — and, on a live reload, to abort THAT rebuild so the
    previous generation keeps serving. Every manifest-named module the operator
    chose to load flows through this one seam: the scalar slots
    (backend/sandbox/storage/monitoring), the additive roles
    (lifecycle/webhook_verifier/channel/router/middleware/agents/extensions/tools),
    and the studio plugins. A module that cannot load is corrupt configuration, not
    a degradation to serve around. The message names what cannot load (the module and
    its kind, or the plugin and the feature it requires), why (the versions in play, the
    feature's enabling variable), and the remedy.
    """


#: A dist's compatibility verdict against the running contract.
CompatState = Literal["compatible", "incompatible", "unknown"]

#: One ref's upgrade-all outcome: moved to the latest compatible version, already on it, no published
#: version supports the running contract, or the move failed.
UpgradeOutcome = Literal["upgraded", "up-to-date", "no-compatible-version", "failed"]


@dataclass(frozen=True)
class CompatVerdict:
    """One dist's (or module's) verdict against the running contract.

    ``reason`` is ``None`` only for ``compatible``; for ``incompatible`` it is the
    operator-facing boot-abort text, for ``unknown`` the note to log.
    """

    status: CompatState
    reason: str | None = None

    def as_payload(self) -> dict[str, str | None]:
        """The ``{status, reason}`` object the installed listing serves per row."""
        return {"status": self.status, "reason": self.reason}


def running_contract_version() -> str:
    """The installed ``tai42-contract`` version.

    The value every verdict here checks against and the ``contract`` param the
    resolve client sends.
    """
    return metadata.version(_CONTRACT_DIST)


def distribution_map() -> InstalledPackages:
    """A snapshot of top-level package → owning distribution(s), reusable across a boot pass.

    Read by :func:`~tai42_skeleton.marketplace.installed_packages.installed_packages`, so an
    editable install that records only a ``.pth``, and one served by an import hook, map to
    their distribution too. The read walks every installed dist, so per-module fresh reads
    would be quadratic; one boot pass reuses this snapshot across every manifest module.
    The snapshot's ``quarantined`` holds each module item of a plugin the current plugin layer
    left out (:func:`layer_exclusions`).
    """
    return replace(installed_packages(), quarantined=layer_exclusions())


def layer_exclusions() -> dict[str, LeftOutModule]:
    """Each module item of a plugin the current plugin layer build left out, by import path, with its distribution.

    Read from the build record of the layer's ``current`` build through
    :func:`~tai42_skeleton.marketplace.environments.current_record.current_record`; empty when
    ``TAI_PLUGINS_DIR`` is unset or the layer has no current build. A current build without its
    record, or with one that does not read, raises
    :class:`~tai42_skeleton.marketplace.errors.CurrentRecordError` carrying the verification's
    finding, which names ``tai plugins rebuild``.
    """
    plugins_dir = configured_plugins_dir()
    if plugins_dir is None:
        return {}
    current = current_record(layer_dir(plugins_dir), LAYER_SUBJECT)
    if current.finding is not None:
        raise CurrentRecordError(current.finding)
    if current.record is None:
        return {}
    return {
        module: LeftOutModule(distribution=excluded.input.package, reason=excluded.reason)
        for excluded in current.record.excluded
        for module in excluded.modules
    }


def _declared_contract_specifier(dist_name: str, requires: list[str] | None) -> SpecifierSet | None:
    """The ``tai42-contract`` specifier the dist's ``requires`` lines declare, or ``None`` when none is derivable.

    ``None`` covers a dist not installed, no metadata, or no declaration.
    Extra-gated requirement lines are skipped (not part of the runtime set);
    multiple runtime declarations AND together. A malformed line is skipped with
    a warning — see the module docstring for why that is not a boot failure.
    """
    if not requires:
        return None
    combined: SpecifierSet | None = None
    for line in requires:
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            logger.warning("distribution %s carries an unparsable Requires-Dist line %r; skipping it", dist_name, line)
            continue
        if canonicalize_name(requirement.name) != _CONTRACT_DIST:
            continue
        # ``extra == "..."`` markers never hold for the runtime dependency set;
        # the empty extra makes them evaluate False while the ordinary
        # environment markers (python_version, sys_platform) still apply.
        if requirement.marker is not None and not requirement.marker.evaluate({"extra": ""}):
            continue
        combined = requirement.specifier if combined is None else combined & requirement.specifier
    return combined


def distribution_compat(read: InstalledDistribution) -> CompatVerdict:
    """The verdict for an installed distribution already read, wherever it is installed, by its declared range."""
    return _verdict(read.name, read.dist.requires)


def declared_compat(dist_name: str, contract_range: str | None) -> CompatVerdict:
    """The verdict for distribution ``dist_name`` declaring ``contract_range``, a plugin spec's ``contract``.

    ``None`` declares no range (``unknown``). The range is the validated spec field, so it
    always parses.
    """
    return _range_verdict(dist_name, None if contract_range is None else SpecifierSet(contract_range))


def _verdict(dist_name: str, requires: list[str] | None) -> CompatVerdict:
    """The verdict for distribution ``dist_name`` whose ``Requires-Dist`` lines are ``requires``."""
    return _range_verdict(dist_name, _declared_contract_specifier(dist_name, requires))


def _range_verdict(dist_name: str, specifier: SpecifierSet | None) -> CompatVerdict:
    """The verdict for distribution ``dist_name`` whose ``tai42-contract`` range is ``specifier`` (``None``: none)."""
    if specifier is None:
        return CompatVerdict(
            "unknown",
            f"distribution {dist_name!r} declares no tai42-contract requirement; treating it as compatible",
        )
    installed = running_contract_version()
    if _admits(specifier, installed):
        return CompatVerdict("compatible")
    return CompatVerdict(
        "incompatible",
        f"{dist_name} requires tai42-contract {specifier}, but {installed} is running — "
        f"update the plugin to a release supporting {installed} (tai plugins update) or move the core "
        f"onto a contract the plugin supports",
    )


def module_compat(module: str, installed: InstalledPackages | None = None) -> CompatVerdict:
    """The verdict for a manifest MODULE, from the owning distribution(s) of its top-level package.

    The top-level package is mapped to the owning distribution(s) via
    :func:`distribution_map` and each mapped dist is evaluated from the snapshot's own read
    of it (:func:`distribution_compat`), so the verdict holds for a distribution wherever the
    snapshot found it — any incompatible dist decides the verdict (naming that dist), else
    compatible when at least one dist declared a range, else unknown. A module the snapshot
    records as a module item of a plugin left out of the plugin layer, or a submodule of one,
    is incompatible with the recorded reason unless that plugin's own distribution provides
    the top-level package (a copy the import system serves from elsewhere decides); a
    namespace package other plugins share does not hide it. ``installed`` lets one
    boot pass snapshot the (sys.path-scan-priced) mapping once and reuse it across every
    manifest module; omitted, the mapping is read fresh.
    """
    top_level = module.partition(".")[0]
    snapshot = distribution_map() if installed is None else installed
    dists = snapshot.distributions_for(top_level)
    left_out = _left_out(module, snapshot)
    if left_out is not None and canonicalize_name(left_out.distribution) not in {
        canonicalize_name(dist_name) for dist_name in dists
    }:
        return CompatVerdict("incompatible", left_out.reason)
    if not dists:
        return CompatVerdict(
            "unknown",
            f"module {module!r} maps to no installed distribution; treating it as compatible",
        )
    verdicts = [distribution_compat(snapshot.distribution(dist_name)) for dist_name in dict.fromkeys(dists)]
    for verdict in verdicts:
        if verdict.status == "incompatible":
            return verdict
    if any(verdict.status == "compatible" for verdict in verdicts):
        return CompatVerdict("compatible")
    return verdicts[0]


def _left_out(module: str, snapshot: InstalledPackages) -> LeftOutModule | None:
    """The left-out module item ``module`` is or lies under, from the snapshot's ``quarantined``."""
    for item, left_out in snapshot.quarantined.items():
        if module == item or module.startswith(f"{item}."):
            return left_out
    return None


def refuse_incompatible(module: str, kind: str, installed: InstalledPackages | None = None) -> None:
    """Refuse a manifest-declared ``kind`` module whose distribution is contract-incompatible, before it imports.

    The one compat refusal every door that loads a manifest-named module crosses: an
    INCOMPATIBLE verdict (:func:`module_compat`) raises :class:`CorePluginBootError` naming
    the module, its kind, the versions in play and the remedy — importing it is exactly what
    crash-loops or misbehaves. An ``unknown`` verdict proceeds with a logged note, never a
    silent pass. ``installed`` is the caller's :func:`distribution_map` snapshot; omitted,
    the mapping is read fresh.
    """
    verdict = module_compat(module, installed)
    refuse_incompatible_verdict(module, kind, verdict)
    if verdict.status == "unknown":
        logger.info("plugin compat unknown for %s module %s: %s", kind, module, verdict.reason)


def refuse_incompatible_verdict(module: str, kind: str, verdict: CompatVerdict) -> None:
    """Raise :class:`CorePluginBootError` for ``module`` when ``verdict`` is INCOMPATIBLE; else return.

    The refusal :func:`refuse_incompatible` raises, for a caller that already holds the
    module's verdict and reads the module's packaged spec before the module imports.
    """
    if verdict.status == "incompatible":
        raise CorePluginBootError(
            f"{kind} plugin {module!r} is incompatible and cannot load: {verdict.reason}; "
            "a manifest-declared plugin that cannot load aborts boot — fix or update the plugin, "
            "remove it from the manifest, or point the manifest at a working one"
        )


@dataclass(frozen=True)
class UpdateTargets:
    """The update picture for one installed ref, computed from the registry's version rows.

    Computed against the running contract: the newest published version (``latest``),
    the newest published COMPATIBLE version (``latest_compatible``), whether that
    compatible version beats the installed one (``update_available``), and the newest
    published INCOMPATIBLE version newer than the installed one (``incompatible_newer``
    — the "an update exists, but it needs a newer core" signal), or ``None`` where no
    such version exists.
    """

    latest: str | None
    latest_compatible: str | None
    update_available: bool
    incompatible_newer: str | None


def _published_version(row: Any) -> Version | None:
    """The published version of one registry version row, or ``None`` when the row is not published.

    A non-object element or a published row whose version is missing / non-PEP440 is
    garbled registry data → the typed registry-data fault (502), never a silent skip.
    """
    if not isinstance(row, dict):
        raise RegistryResponseError("registry versions response contains a non-object element", status=None)
    if row.get("status") != "published":
        return None
    raw_version = row.get("version")
    try:
        return Version(raw_version)  # pyright: ignore[reportArgumentType] — the except is the type guard
    except (InvalidVersion, TypeError) as exc:
        raise RegistryResponseError(
            f"registry served an unusable published version {raw_version!r}: {exc}", status=None
        ) from exc


def _row_compatible(row: dict[str, Any], version: Version, contract_version: str) -> bool:
    """Whether one published row supports the running contract, by its ``contract_range``.

    An absent (``None``) range counts compatible, a present one is checked with
    ``prereleases=True``. A non-string or malformed range is garbled registry data →
    the typed registry-data fault (502).
    """
    contract_range = row.get("contract_range")
    if contract_range is None:
        return True
    if not isinstance(contract_range, str):
        raise RegistryResponseError(
            f"registry versions response contract_range for {version} is not a string", status=None
        )
    return registry_range_admits(contract_range, contract_version, version=version)


def registry_range_admits(contract_range: str, contract_version: str, *, version: Version | None = None) -> bool:
    """Whether a registry-served ``contract_range`` admits ``contract_version``, by the verdict's own semantics.

    ``version`` names the published version the range was served for, when there is one, in the
    error. A range that does not parse as a PEP 440 specifier set is garbled registry data → the
    typed registry-data fault (502), never a caller 400.
    """
    try:
        specifier = SpecifierSet(contract_range)
    except InvalidSpecifier as exc:
        served_for = "" if version is None else f" for version {version}"
        raise RegistryResponseError(
            f"registry served an unusable contract_range {contract_range!r}{served_for}: {exc}", status=None
        ) from exc
    return _admits(specifier, contract_version)


def _admits(specifier: SpecifierSet, contract_version: str) -> bool:
    """Whether ``specifier`` admits ``contract_version``.

    ``prereleases=True``, so a dev-versioned contract (an editable checkout reporting e.g.
    ``0.5.0.dev3``) inside the range is admitted.
    """
    return specifier.contains(contract_version, prereleases=True)


def update_targets(version_rows: list[Any], *, installed_version: str, contract_version: str) -> UpdateTargets:
    """Compute :class:`UpdateTargets` from the registry's version rows.

    Only ``status == "published"`` rows count. A row's ``contract_range`` gates
    compatibility with the SAME semantics as the dist verdict (``None`` — no
    declared range — counts compatible; ``prereleases=True``). Garbled registry
    data (a non-object row, a published row with a missing/non-PEP440 version,
    a non-string or malformed range) raises the typed registry-data fault so the
    boundary answers 502, never a silent skip; a garbled LOCAL installed version
    is corrupt local state, never blamed on the registry.
    """
    try:
        installed = Version(installed_version)
    except (InvalidVersion, TypeError) as exc:
        raise LocalStateError(f"the stored installed version {installed_version!r} is not PEP 440: {exc}") from exc

    latest: Version | None = None
    latest_compatible: Version | None = None
    incompatible_newer: Version | None = None
    for row in version_rows:
        version = _published_version(row)
        if version is None:
            continue
        compatible = _row_compatible(row, version, contract_version)
        if latest is None or version > latest:
            latest = version
        if compatible and (latest_compatible is None or version > latest_compatible):
            latest_compatible = version
        if not compatible and version > installed and (incompatible_newer is None or version > incompatible_newer):
            incompatible_newer = version

    return UpdateTargets(
        latest=str(latest) if latest is not None else None,
        latest_compatible=str(latest_compatible) if latest_compatible is not None else None,
        update_available=latest_compatible is not None and latest_compatible > installed,
        incompatible_newer=str(incompatible_newer) if incompatible_newer is not None else None,
    )
