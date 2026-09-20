"""The venv/pip/prefix side of a flow: pip install a pinned version, remove a package, and env-shadow refusal.

The pip transaction boundary: an unwind fully reverts skeleton state, but the venv
is only as transactional as pip itself — ``pip uninstall`` removes just the named
distribution, so dependencies pip installed or upgraded in place during an attempt
remain.

The prefix env-shadow rule: with a prefix configured, the prefix sits at the END of
``sys.path`` so the environment shadows it for any package present in both. A prefix
install/update whose distribution the environment already provides at the SAME
version is a harmless no-op and proceeds; at a DIFFERENT version it is refused loudly
before any state change (:func:`guard_env_shadow`), because the prefix copy would
never import. Uninstall mirrors this: a distribution absent from the prefix but
present in the environment is the env-shadowed no-op install's footprint, so
:func:`remove_package` removes no files and returns ``False``; absent from BOTH is a
loud failure.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from tai42_skeleton.marketplace.errors import EnvironmentShadowError, PluginPrefixError
from tai42_skeleton.marketplace.pip import (
    PipRunner,
    fetch_verified_artifact,
    install_args,
    uninstall_args,
)


async def pip_install(
    pip_runner: PipRunner,
    prefix: str | None,
    package: str,
    version: str,
    source: str,
    artifact_ref: str | None,
    sha256: str | None,
) -> str:
    """Run the ``pip install`` for a pinned version and return its output.

    A pypi source pins ``package==version`` directly. A github source first
    downloads the registry-named artifact and verifies its sha256 through
    :func:`fetch_verified_artifact` into a temporary directory kept alive
    across the whole pip run (pip must read the local tarball before it is
    cleaned up), then installs that verified tarball — never a mutable
    ``git+url@tag`` clone. A checksum mismatch or fetch failure raises out of
    here with no pip call and no fallback.

    When a prefix is configured, ``--prefix`` targets it: pip resolves shared
    dependencies against the running environment and installs the plugin plus
    only its genuinely-new dependencies under the prefix (the image's packages
    are never duplicated there).
    """
    if source == "github":
        with tempfile.TemporaryDirectory() as tmp:
            verified = await fetch_verified_artifact(package, version, artifact_ref or "", sha256 or "", Path(tmp))
            return await pip_runner(install_args(package, version, source, verified, prefix=prefix))
    return await pip_runner(install_args(package, version, source, prefix=prefix))


async def remove_package(
    package: str,
    *,
    prefix: str | None,
    pip_runner: PipRunner,
    prefix_uninstall: Callable[[str, str], None],
    prefix_has_dist: Callable[[str, str], bool],
    env_dist_version: Callable[[str, str], str | None],
) -> bool:
    """Remove ``package``'s own distribution and report whether files were removed.

    Environment path (no prefix): ``pip uninstall`` — always removes, returns
    ``True``. Prefix path: remove from the prefix by its ``RECORD`` when present
    there (``True``); when ABSENT from the prefix but present in the ENVIRONMENT
    nothing ever landed in the prefix (an env-shadowed no-op install), so remove
    no files and return ``False`` — never a loud failure for a state the install
    rule made legitimate. Absent from BOTH the prefix and the environment is a
    loud :class:`PluginPrefixError`. Only the named distribution goes on every
    path; dependencies stay.
    """
    if prefix is None:
        await pip_runner(uninstall_args(package))
        return True
    if prefix_has_dist(package, prefix):
        prefix_uninstall(package, prefix)
        return True
    if env_dist_version(package, prefix) is not None:
        return False
    raise PluginPrefixError(
        f"{package!r} is installed in neither the plugin prefix {prefix} nor the environment; cannot remove it"
    )


def guard_env_shadow(
    package: str,
    pinned_version: str,
    prefix: str,
    *,
    env_dist_version: Callable[[str, str], str | None],
) -> None:
    """Refuse a prefix install/update the running environment would shadow.

    The prefix sits at the END of ``sys.path``, so a distribution the environment
    already provides wins for every version. Same version → the prefix install is
    a harmless no-op (the manifest wiring is the whole value), so proceed. A
    DIFFERENT environment version would silently hide the prefix copy → refuse
    loudly before any package, manifest, or attribution state changes,
    naming both versions.
    """
    env_version = env_dist_version(package, prefix)
    if env_version is not None and env_version != pinned_version:
        raise EnvironmentShadowError(
            f"the environment provides {package} {env_version}, which sits ahead of the plugin prefix on "
            f"sys.path and would shadow a prefix install of {pinned_version}; refusing to install a version "
            f"the environment will hide (re-pin to {env_version} or rebuild the image)"
        )
