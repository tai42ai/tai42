"""Translate a registry resolve-response or stored attribution row into a validated :class:`PluginSpec`.

Adds its pin provenance, parsed ref, and running-core version stamps.
Every function here is a pure translation of registry/local data with no I/O and
no app handle: it validates the shipped spec, vets advisories and source, parses a
``namespace/name`` ref, and reads the running core versions, so the flows can order
the resolve step without owning its data checks.
"""

from __future__ import annotations

import importlib.metadata
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import ValidationError
from tai42_contract.plugins import PluginSpec

from tai42_skeleton.marketplace.compat import running_contract_version
from tai42_skeleton.marketplace.errors import (
    ContractIncompatibleError,
    LocalStateError,
    MalformedRefError,
    RegistryResponseError,
    VersionRefusedError,
)
from tai42_skeleton.marketplace.store import InstallRecord


def prepare_resolved(resolved: dict[str, Any]) -> tuple[PluginSpec, str]:
    """Validate and vet a resolve response, returning ``(spec, source)``.

    Refuses a non-withdrawn critical advisory, validates the shipped
    ``PluginSpec`` (a malformed spec is a registry-data fault → 502, never a
    caller 400), requires the github artifact provenance (repository_url + tag
    for display, artifact_ref + sha256 for the verified fetch), and checks the
    plugin's ``contract_range`` against the installed ``tai42-contract`` version
    WHEN one is declared — a contract-less plugin (an mcp-server, or a
    descriptor-only connector shipping no package) imports no ``tai42-contract``,
    so the registry serves ``contract_range`` null and there is nothing to gate.
    """
    for advisory in require_list(resolved, "advisories"):
        if advisory.get("withdrawn_at") is None and advisory.get("severity") == "critical":
            summary = advisory.get("summary", "no summary")
            raise VersionRefusedError(f"a critical advisory affects this version: {summary}")

    try:
        spec = PluginSpec.model_validate(require(resolved, "spec"))
    except ValidationError as exc:
        raise RegistryResponseError(f"registry served an invalid plugin spec: {exc}", status=None) from exc

    source = require(resolved, "source")
    if source in ("github", "spec"):
        # A github source ships a wheel; a ``spec`` source ships a descriptor-only
        # ``tai-plugin.yml`` (a plugin with no package). Both carry the SAME pointer
        # fields — repository_url + tag for display, artifact_ref + sha256 for the
        # verified fetch/integrity — so the same presence requirements apply.
        if not resolved.get("repository_url") or not resolved.get("tag"):
            raise RegistryResponseError(
                f"registry resolve response for a {source} source is missing repository_url or tag",
                status=None,
            )
        if not resolved.get("artifact_ref") or not resolved.get("sha256"):
            raise RegistryResponseError(
                f"registry resolve response for a {source} source is missing artifact_ref or sha256",
                status=None,
            )
    elif source != "pypi":
        raise RegistryResponseError(f"registry returned an unknown install source {source!r}", status=None)

    # A contract-BEARING plugin declares a range; a contract-less one (mcp-server /
    # descriptor-only connector) has ``contract_range`` null — no constraint to gate,
    # counted compatible, MATCHING ``compat.update_targets`` (a null range there also
    # counts compatible). Only a present (non-null) range is checked.
    contract_range = resolved.get("contract_range")
    if contract_range is not None:
        check_contract(contract_range)
    return spec, source


def check_contract(contract_range: str) -> None:
    """Require the installed ``tai42-contract`` version to satisfy the plugin's declared ``contract_range``.

    ``prereleases=True`` so a dev-versioned tai42-contract (an editable checkout
    reporting e.g. ``0.5.0.dev3``) inside the range still passes — a developer
    environment is not spuriously refused. A malformed range is registry data
    → 502, never a caller 400.
    """
    # The caller only invokes this with a PRESENT (non-null) range — a contract-less
    # plugin is skipped upstream — and the registry client's resolve boundary types
    # the field when present, so this check owns only the string's FORMAT: a non-PEP440
    # specifier set is garbled registry data → 502, never a caller 400.
    try:
        specifier = SpecifierSet(contract_range)
    except InvalidSpecifier as exc:
        raise RegistryResponseError(
            f"registry served an unusable contract_range {contract_range!r}: {exc}", status=None
        ) from exc
    installed = running_contract_version()
    if not specifier.contains(installed, prereleases=True):
        raise ContractIncompatibleError(
            f"plugin requires tai42-contract {contract_range}, but {installed} is installed"
        )


def parse_ref(ref: str) -> tuple[str, str]:
    """Split ``namespace/name`` into its two lowercase halves.

    Raises :class:`MalformedRefError` (surfaced by the boundary as a 400) on
    anything but exactly one ``/`` with two non-empty lowercase halves. The typed
    error is distinct from a server-side invariant fault, so the operation layer
    maps ONLY a malformed ref to a bad-request response.
    """
    parts = ref.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise MalformedRefError(f"ref must be 'namespace/name', got {ref!r}")
    namespace, name = parts
    if namespace != namespace.lower() or name != name.lower():
        raise MalformedRefError(f"ref must be lowercase 'namespace/name', got {ref!r}")
    return namespace, name


def spec_from_row(row: InstallRecord) -> PluginSpec:
    """Reconstruct the stored ``PluginSpec`` from an attribution row — LOCAL truth, no registry call.

    A row that no longer validates is corrupt local state
    (:class:`LocalStateError`, a 500), never the caller's request.
    """
    try:
        return PluginSpec.model_validate(row.spec)
    except ValidationError as exc:
        raise LocalStateError(f"the stored spec for {row.ref} is corrupt: {exc}") from exc


def pin_provenance(resolved: dict[str, Any], source: str) -> tuple[str | None, str | None, str | None, str | None]:
    """The ``(repository_url, tag, artifact_ref, sha256)`` to store for the pin.

    The resolve values for a github OR a ``spec`` source (both carry the pointer
    fields), else all ``None`` — so a pypi row keeps every pin column NULL even when
    the resolve response carries them. The stored ``artifact_ref`` + ``sha256`` are
    what let update-unwind reinstall the prior github pin through the same verified
    fetch path (a descriptor ``spec`` pin stores them for provenance/integrity).
    """
    if source in ("github", "spec"):
        return (
            resolved.get("repository_url"),
            resolved.get("tag"),
            resolved.get("artifact_ref"),
            resolved.get("sha256"),
        )
    return None, None, None, None


def core_version_stamps() -> tuple[str, str]:
    """The ``(tai42-contract, tai42-skeleton)`` versions running right now.

    The diagnostics stamp every attribution write carries.
    """
    return running_contract_version(), importlib.metadata.version("tai42-skeleton")


def require(resolved: dict[str, Any], key: str) -> Any:
    """A required resolve-response field — present AND non-null — or a typed registry-data fault (502).

    The client boundary type-checks a field only when it is present and non-null
    (a null there is legitimate for the github-only optional pins and for a
    contract-less plugin's ``contract_range``), so ``require`` has to mean non-null
    here: a null in an always-present field (``version``, ``spec``, ``source``) is
    missing data, and rejecting it keeps the ``str``-typed parsers downstream honest —
    they never see ``None``. (``contract_range`` is NOT required here: a contract-less
    plugin legitimately carries a null one, gated by presence at its own use site.)
    """
    value = resolved.get(key)
    if value is None:
        raise RegistryResponseError(f"registry resolve response is missing {key!r}", status=None)
    return value


def require_list(resolved: dict[str, Any], key: str) -> list[Any]:
    """A required list-shaped resolve-response field, or a typed registry-data fault (502)."""
    value = require(resolved, key)
    if not isinstance(value, list):
        raise RegistryResponseError(f"registry resolve response {key!r} is not a list", status=None)
    return value
