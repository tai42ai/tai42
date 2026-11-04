"""The typed failure hierarchy the marketplace client and installer raise.

Every failure family has one class under :class:`MarketplaceError`. These are
the internal, load-bearing errors: they carry the attributes the installer and
the operation boundary branch on (an HTTP status, a not-installed flag, a pip
argv + return code, the two errors of a failed unwind). The operation layer
catches them at its edge and translates each to the shared operation-error
vocabulary; nothing here leaks a raw third-party exception.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from tai42_skeleton.exceptions.exceptions import TaiMCPServerError

if TYPE_CHECKING:
    from tai42_skeleton.config.recycle_roll import RollOutcome
    from tai42_skeleton.marketplace.environments.findings import Finding


class MarketplaceError(TaiMCPServerError):
    """Base for every marketplace-client failure.

    ``roll`` is the fleet recycle roll a failed operation still ran for the config changes it
    landed before failing (its forward change, its unwind's revert, or both), ``None`` when none
    ran; the operation boundary renders it into the error reply.
    """

    roll: RollOutcome | None = None


class RegistryUnreachableError(MarketplaceError):
    """An upstream the registry directed us to could not be reached, errored, or was named unusably.

    The message carries the failing URL and the transport detail. The gateway
    case: this server reached out to the registry (by its base URL) or to a
    registry-named artifact host (by the artifact URL) and got no valid upstream
    response. Distinct from a this-server fault; maps to a 502.
    """


class RegistryResponseError(MarketplaceError):
    """The registry answered an unexpected status or a malformed envelope.

    ``status`` is the upstream HTTP status when the fault originates from an HTTP
    response, and ``None`` when the fault is the shape of registry-served data
    with no status to attribute it to (a malformed resolve spec, a pin field
    that fails validation). One constructor serves both: ``status`` defaults to
    ``None`` and the HTTP site passes the code.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        """Build the error with the optional upstream ``status`` (``None`` for a data-shape fault)."""
        super().__init__(message)
        self.status: int | None = status


class MalformedRefError(MarketplaceError):
    """The caller's ``ref`` is not a well-formed lowercase ``namespace/name``.

    The caller's own author error, mapped at the boundary to a 400. Distinct from
    every registry/environment fault so the operation layer maps ONLY this to a
    bad-request response.
    """


class ListingNotFoundError(MarketplaceError):
    """Unknown listing ref or version.

    The message names the ref (and the version, when one was requested) exactly.
    """


class VersionRefusedError(MarketplaceError):
    """The target version is refused.

    A registry refusal of a killed or otherwise unpublished version, or a
    non-withdrawn critical advisory affects it — the message carries the reason.
    """


class ContractIncompatibleError(MarketplaceError):
    """The plugin's ``contract_range`` excludes the ``tai42-contract`` version installed in this environment.

    The message names both. Nothing was installed.
    """


class ManifestCollisionError(MarketplaceError):
    """A provides item collides with an existing manifest entry.

    The message names every colliding field/module so the operator can resolve
    it.
    """


class ManifestBindingError(MarketplaceError):
    """A spec provides an item kind this repo's manifest bindings do not name.

    Contract drift past the bindings, a server-side invariant fault (500), never
    the caller's request. Raised in place of a silently skipped item.
    """


class InstallStateError(MarketplaceError):
    """A state conflict: the ref is already/not installed, or the target version equals the installed one.

    ``not_installed`` is ``True`` only for the not-installed case, so the
    boundary maps that one to a not-found response and the others to a conflict.
    """

    def __init__(self, message: str, *, not_installed: bool = False) -> None:
        """Build the error, flagging ``not_installed`` for the not-installed case."""
        super().__init__(message)
        self.not_installed = not_installed


class OperationInProgressError(MarketplaceError):
    """Another marketplace operation holds the fleet-wide advisory lock (or the per-worker fast-path lock).

    Retriable; surfaced as a temporary-unavailable response the caller may retry.
    """


class PipUnavailableError(MarketplaceError):
    """The running environment cannot perform the install — the ``pip`` module is missing.

    A uv-synced venv can ship without it. The message names the exact fix.
    """


class PipFailedError(MarketplaceError):
    """A ``pip`` subprocess exited non-zero.

    Carries the credential-free ``argv``, the ``returncode``, and the captured
    combined ``output``. The message summarizes them; the full output stays on
    the attribute so the log can keep it whole while the boundary truncates for
    the response envelope.
    """

    def __init__(self, argv: list[str], returncode: int, output: str) -> None:
        """Build the error from the credential-free ``argv``, the ``returncode``, and the captured ``output``."""
        super().__init__(f"pip {' '.join(argv)} exited with code {returncode}")
        self.argv = argv
        self.returncode = returncode
        self.output = output


class ManifestComposeError(MarketplaceError):
    """A composed manifest failed ``Manifest.model_validate``.

    The registry spec plus the local manifest produced an invalid document. A
    server-side fault, never the caller's request.
    """


class InstallEnvError(MarketplaceError):
    """An mcp-server install/update whose required ``!ENV`` markers were not satisfied.

    A dangling-marker refusal (each missing var + json-pointer named), another
    env-boundary refusal (X-band / key-material), or a tool-extension combo the composed
    manifest turns refused (named by its manifest path), raised by the combined
    env+manifest pipeline before anything persisted. The caller's own input error, mapped
    at the boundary to a 400. Names only, never values.
    """


class EnvRevertConflictError(MarketplaceError):
    """An unwind cannot revert an env key the operation wrote: another write replaced its value since.

    The message names each such key. The revert writes nothing, so every value stands.
    """


class ManifestRefusedError(MarketplaceError):
    """The config pipeline refused the composed manifest of an install, update or uninstall.

    A boundary refusal of the composed document (a tool-extension combo whose dry-run bind
    refuses, named by its manifest path and the extension's reason, or a dangling ``!ENV``
    marker) raised before anything persisted. The deployment's own manifest state is what the
    operator changes, so the boundary maps it to a 400 carrying the refusal text.
    """


class InstallUnwindError(MarketplaceError):
    """A step failed AND the unwind of the already-applied steps also failed.

    Carries both the original ``step_error`` and the ``unwind_error`` raised
    while rolling back, and a composed message stating exactly what was left
    behind so the operator can finish the cleanup by hand. ``span_release_error`` is the
    failed span releases of the config writes the unwind landed before it failed, joined in
    write order (``None`` when each released, or the unwind landed no write).
    """

    def __init__(
        self, step_error: BaseException, unwind_error: Exception, *, span_release_error: str | None = None
    ) -> None:
        """Build the error from the original ``step_error`` and the ``unwind_error`` raised while rolling back.

        A step error with no text (a cancellation) is named by its type.
        """
        super().__init__(
            f"install step failed ({str(step_error) or type(step_error).__name__}); the unwind then also failed "
            f"({unwind_error}) — the environment was left partially changed"
        )
        self.step_error = step_error
        self.unwind_error = unwind_error
        self.span_release_error = span_release_error


class UnrunRollError(MarketplaceError):
    """An operation's config changes landed and the fleet recycle roll they owe did not complete.

    Raised from the error that stopped the roll (a dropped database session, a broken bus),
    which is logged whole where the roll stopped. The message names only that error's type, so
    its text — hosts, driver detail — stays out of the reply; the boundary maps it to a 500
    carrying the message.
    """


class LocalStateError(MarketplaceError):
    """The local attribution-store data is corrupt.

    A row that cannot be reconstructed into an install record. A server-side
    fault, never the caller's request.
    """


class PluginsDirError(MarketplaceError):
    """The plugins directory (``TAI_PLUGINS_DIR``) cannot serve the operation.

    It is unset when a packaged plugin needs it, not writable when an operation builds an
    environment in it, it resolves onto the running environment itself, a build record in it
    cannot be read, a distribution an environment build reads reports no version, a server
    package's current build holds no distribution of that package, or two server packages
    declare the same console script. A deployment/local fault, never the caller's request.
    """


class UnreadableBuildRecordError(PluginsDirError):
    """An environment build's record (``tai42-build.json``) is not the document a build writes.

    ``path`` is the record file and ``reason`` why it does not read; the message names both.
    """

    def __init__(self, path: Path, reason: str) -> None:
        """Build the error for the record at ``path`` that does not read for ``reason``."""
        super().__init__(f"plugin build record {path} is unreadable: {reason}")
        self.path = path
        self.reason = reason


class CurrentRecordError(PluginsDirError):
    """An environment's ``current`` build has no record, or one that does not read, where its record is needed.

    ``finding`` is the verification's finding for it
    (:func:`~tai42_skeleton.marketplace.environments.current_record.current_record`); the
    message is its report text.
    """

    def __init__(self, finding: Finding) -> None:
        """Build the error for ``finding``."""
        super().__init__(finding.report_text())
        self.finding = finding


class PluginMigrationError(MarketplaceError):
    """The migration runner refused or failed the plugin's schema chain during an install or update.

    The message names the plugin and carries the runner's own text (a rewritten chain, an
    out-of-order migration, a malformed migrations directory); the runner's error is the
    ``__cause__``. Raised after the new environment was current, so the operation's unwind
    swaps it back and notes that on this error.
    """


class PluginDependencyConflictError(MarketplaceError):
    """A plugin's dependencies cannot sit in the plugin layer with the installed plugins and this image.

    pip found no resolution of the layer's plugins together with the image's pinned
    distributions, or a build would add a distribution beside the image's own copy. Refused
    before the new build is used; the message names the plugin and the conflict. A state
    conflict the operator resolves (another version, another plugin, another image), never a
    retry.
    """


class UndeclaredServerCommandError(MarketplaceError):
    """An ``mcp-server`` item names a ``command`` that is not a console script of the package built for it.

    A server package's environment exposes only the console scripts its package declares, so
    such a command could never launch from it. Refused after the build and before it is used;
    the message names the plugin, the item, the command and the package's scripts. A plugin
    spec fault the operator resolves by choosing another version or plugin, never a retry.
    """


class ServerCommandCollisionError(MarketplaceError):
    """Two server packages would expose one console script name.

    A bare ``mcp`` command names one package's script, so a second server package declaring a
    script another installed server package already exposes would leave that name with two
    owners. Refused after the build and before it is used; the earlier package stays live. The
    message names the plugin, both packages and the script. A state conflict the operator
    resolves by uninstalling the other plugin or choosing another release, never a retry.
    """


class RouteMountError(MarketplaceError):
    """A ``route_mounts`` override names a non-route-carrying item or a malformed base.

    The item is not route-carrying in the resolved spec, or the base is not the
    relative mount charset — the caller's own input error, mapped at the boundary
    to a 400. Names the offending item or base, never guesses.
    """


class RouteCollisionError(MarketplaceError):
    """One or more declared routes collide (path SHAPE + method) with a route the live registry already owns.

    The owner is core or another installed plugin. Carries the collision rows so
    the operator sees each clash and its owner; the remedy, stated in the message,
    is to remap the item's mount base. Mapped at the boundary to a 409 with a
    machine-readable ``ROUTE_COLLISION`` code.
    """

    def __init__(self, collisions: list[dict[str, Any]]) -> None:
        """Build the error from the ``collisions`` rows (each clashing route and its owner)."""
        super().__init__(
            "declared route(s) collide with a route the server already owns; remap the "
            f"item's mount base to resolve: {collisions}"
        )
        self.collisions = collisions


class PublicRoutesNotAcceptedError(MarketplaceError):
    """Declared PUBLIC route(s) require the operator's explicit acceptance and the accept flag was not set.

    They answer UNAUTHENTICATED once installed. Carries the rows requiring
    acceptance (an install lists every public row; an update lists only rows not
    already approved in the installed version). Mapped at the boundary to a 400
    with a machine-readable ``PUBLIC_ROUTES_NOT_ACCEPTED`` code.
    """

    def __init__(self, public_routes: list[dict[str, Any]]) -> None:
        """Build the error from the ``public_routes`` rows requiring acceptance."""
        super().__init__(
            "installing would open route(s) that answer WITHOUT authentication; pass "
            f"accept_public_routes to proceed: {public_routes}"
        )
        self.public_routes = public_routes


class ReservedRoutePrefixError(MarketplaceError):
    """A declared PUBLIC route resolves under a reserved never-public prefix.

    The operator remapped a base into it, or the declaration does — refused
    before any state change. Carries the offending resolved paths and the
    reserved prefixes. Mapped at the boundary to a 409 with a machine-readable
    ``ROUTE_RESERVED_PREFIX`` code.
    """

    def __init__(self, offenders: list[str], reserved_prefixes: list[str]) -> None:
        """Build the error from the ``offenders`` (resolved paths) and the ``reserved_prefixes``."""
        super().__init__(
            f"declared public route(s) {offenders} resolve under a reserved never-public "
            f"prefix {reserved_prefixes}; a public route may not mount there — remap the base"
        )
        self.offenders = offenders
        self.reserved_prefixes = reserved_prefixes


class ArtifactIntegrityError(MarketplaceError):
    """A fetched github artifact's sha256 does not match the digest the registry captured at release ingest.

    The download is refused, not installed.

    This is its OWN type, distinct from :class:`RegistryResponseError`: the
    registry data was well-formed (a valid ``https://`` artifact_ref and a valid
    hex digest), but the FETCHED BYTES disagree with that digest — the likely
    cause is a release tag re-pointed to a different commit since ingest. Carries
    the expected and actual digests and the artifact URL so the operator can see
    exactly which download was rejected and why.
    """

    def __init__(self, *, expected_sha256: str, actual_sha256: str, artifact_ref: str) -> None:
        """Build the error from ``expected_sha256``, ``actual_sha256``, and the ``artifact_ref``."""
        super().__init__(
            f"artifact sha256 mismatch for {artifact_ref}: expected {expected_sha256}, got {actual_sha256} "
            "— the release tag may have been re-pointed since registry ingest; refusing to install"
        )
        self.expected_sha256 = expected_sha256
        self.actual_sha256 = actual_sha256
        self.artifact_ref = artifact_ref
