"""Resolve-response guards against garbled registry data, and ref normalization."""

from __future__ import annotations

import pytest

from tai42_skeleton.marketplace import installer as installer_module
from tai42_skeleton.marketplace.errors import (
    MalformedRefError,
    RegistryResponseError,
)

from ._specs import make_resolved, make_spec
from .test_installer import (
    Harness,
    _assert_no_pip,
)

# -- resolve-response guards (garbled/compromised registry data) -------------


@pytest.mark.parametrize(
    ("key", "absent"),
    [
        ("source", True),  # a required field entirely absent
        ("version", False),  # present-but-null
    ],
)
async def test_install_resolve_missing_or_null_required_field_is_registry_response_error(
    key: str, absent: bool
) -> None:
    # A required resolve field that is absent OR present-but-null is garbled upstream
    # data (a 502 at the boundary), never a caller error, and ``_require`` catches it
    # before any pip call. Null matters specially: the client's resolve boundary
    # type-checks a field only when it is present and non-null (a null is legitimate
    # for the github-only optional pins), so a null ``version`` would otherwise reach
    # ``Version(None)`` — a ``TypeError``, not a typed parse error — and escape the
    # operation boundary as an untyped 500. (``contract_range`` is deliberately NOT in
    # this set: a contract-less plugin legitimately carries a null one — see
    # ``test_install_null_contract_range_is_contract_less_and_proceeds``.)
    h = Harness()
    spec = make_spec()
    resolved = make_resolved(spec)
    if absent:
        del resolved[key]
    else:
        resolved[key] = None
    h.registry.resolved = resolved
    with pytest.raises(RegistryResponseError, match=key):
        await h.installer().install("tai42/toolbox")
    _assert_no_pip(h)


async def test_install_null_contract_range_is_contract_less_and_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # A contract-less plugin (an mcp-server, or a descriptor-only connector shipping no
    # package) imports no tai42-contract, so the registry serves ``contract_range`` null.
    # That is NOT garbled data: there is no constraint to gate, so the install proceeds —
    # matching ``compat.update_targets``, which also counts a null range compatible. The
    # installed contract version is irrelevant here (nothing checks it).
    monkeypatch.setattr(installer_module.importlib.metadata, "version", lambda name: "2.0.0")
    h = Harness()
    spec = make_spec()
    h.registry.resolved = make_resolved(spec, contract_range=None)
    await h.installer().install("tai42/toolbox")
    assert h.pip.calls  # proceeded to install, no contract refusal on a null range


async def test_install_resolve_advisories_not_list_is_registry_response_error(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness()
    spec = make_spec()
    resolved = make_resolved(spec)
    resolved["advisories"] = {"not": "a list"}
    h.registry.resolved = resolved
    with pytest.raises(RegistryResponseError, match="advisories"):
        await h.installer().install("tai42/toolbox")
    _assert_no_pip(h)


async def test_install_unknown_source_is_registry_response_error(monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness()
    spec = make_spec()
    h.registry.resolved = make_resolved(spec, source="svn")
    with pytest.raises(RegistryResponseError, match="unknown install source"):
        await h.installer().install("tai42/toolbox")
    _assert_no_pip(h)


async def test_install_github_missing_provenance_is_registry_response_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A github source with no repository_url/tag cannot be pinned — garbled
    # upstream data, refused before pip.
    h = Harness()
    spec = make_spec()
    h.registry.resolved = make_resolved(spec, source="github", repository_url=None, tag=None)
    with pytest.raises(RegistryResponseError, match="repository_url or tag"):
        await h.installer().install("tai42/toolbox")
    _assert_no_pip(h)


async def test_install_github_missing_artifact_ref_is_registry_response_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A github source with provenance but no artifact_ref/sha256 cannot be
    # verified — garbled upstream data, refused before any fetch or pip.
    h = Harness()
    spec = make_spec()
    resolved = make_resolved(spec, source="github", repository_url="https://github.com/tai42ai/toolbox", tag="v1.0.0")
    resolved["artifact_ref"] = None
    resolved["sha256"] = None
    h.registry.resolved = resolved
    with pytest.raises(RegistryResponseError, match="artifact_ref or sha256"):
        await h.installer().install("tai42/toolbox")
    _assert_no_pip(h)


async def test_install_malformed_contract_range_is_registry_response_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A ``contract_range`` string that does not parse as a specifier set is
    # garbled registry data → RegistryResponseError (a 502), never a caller 400.
    # A non-STRING value never reaches this parse — the registry client's resolve
    # boundary types the field. The refusal is before pip runs.
    h = Harness()
    spec = make_spec()
    h.registry.resolved = make_resolved(spec, contract_range="not a specifier!!")
    with pytest.raises(RegistryResponseError, match="unusable contract_range"):
        await h.installer().install("tai42/toolbox")
    _assert_no_pip(h)


# -- ref normalization -------------------------------------------------------


async def test_install_mixed_case_ref_is_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    # A ref with the right shape (one slash, two non-empty halves) but an uppercase
    # half is still malformed — refs are lowercase 'namespace/name'.
    h = Harness()
    with pytest.raises(MalformedRefError, match="lowercase"):
        await h.installer().install("Tai42/Toolbox")
    _assert_no_pip(h)
