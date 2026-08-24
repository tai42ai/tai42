"""The stable ``ErrorKind`` taxonomy and its :func:`error_kind` resolver.

Covers the closed-set compatibility snapshot, the per-exception stamp table over
EVERY stamped contract error, the fallback registry's MRO walk, the bounded
``__cause__`` recursion, the instance-beats-class rule, and the rename-immunity
pin — the exact fragility (matching an error by its class NAME or message text)
this feature removes.
"""

from __future__ import annotations

import pytest

from tai42_contract.agent.base import AgentInterruptedError
from tai42_contract.channels import ChannelDeliveryError, ChannelInputError
from tai42_contract.errors import (
    ERROR_KIND_ATTR,
    ClientConnectError,
    ClientDisconnectedError,
    ErrorKind,
    error_kind,
    register_error_kind,
)
from tai42_contract.monitoring.errors import (
    MonitoringError,
    MonitoringReadNotSupportedError,
    TraceNotFoundError,
)
from tai42_contract.presets.errors import (
    PresetError,
    PresetExistsError,
    PresetNameConflictError,
    PresetNotFoundError,
    PresetVersionNotFoundError,
)
from tai42_contract.sandbox.errors import (
    SandboxError,
    SandboxExecTimeoutError,
    SandboxSessionNotFoundError,
    SandboxSpecRejectedError,
    SandboxUnavailableError,
)
from tai42_contract.settings_profiles.errors import (
    SettingsProfileError,
    SettingsProfileExistsError,
    SettingsProfileNotFoundError,
    SettingsProfileVersionNotFoundError,
)
from tai42_contract.tool_meta.errors import (
    FolderCycleError,
    FolderNameConflictError,
    FolderNotEmptyError,
    FolderNotFoundError,
    ToolMetaError,
)
from tai42_contract.versioning.errors import (
    DocumentExistsError,
    DocumentNotFoundError,
    DocumentStoreError,
    DocumentVersionNotFoundError,
)

# -- Closed-set compatibility snapshot -----------------------------------------


def test_error_kind_is_the_closed_promised_set():
    # This exact value set is the compatibility promise: a value is never renamed
    # or removed, and a NEW value here is a deliberate minor bump — so this snapshot
    # is the wall a rename/removal breaks against.
    assert {k.value for k in ErrorKind} == {
        "delivery_failed",
        "timed_out",
        "bad_input",
        "unauthorized",
        "not_found",
        "conflict",
        "unavailable",
        "upstream_error",
        "cancelled",
        "unknown",
    }


def test_error_kind_values_equal_their_strings():
    # StrEnum: a member IS its wire string, so a persisted/transported value round-trips.
    assert ErrorKind.NOT_FOUND == "not_found"
    assert ErrorKind("not_found") is ErrorKind.NOT_FOUND


# -- The full stamp table over every stamped contract exception ----------------

_STAMPED_CONTRACT_ERRORS: list[tuple[BaseException, ErrorKind]] = [
    # channels
    (ChannelDeliveryError("boom"), ErrorKind.DELIVERY_FAILED),
    (ChannelInputError("boom"), ErrorKind.BAD_INPUT),
    # shared client errors
    (ClientDisconnectedError(), ErrorKind.UNAVAILABLE),
    (ClientConnectError(), ErrorKind.UNAVAILABLE),
    # sandbox family
    (SandboxError("x"), ErrorKind.UPSTREAM_ERROR),
    (SandboxUnavailableError("x"), ErrorKind.UNAVAILABLE),
    (SandboxSessionNotFoundError("sid"), ErrorKind.NOT_FOUND),
    (SandboxExecTimeoutError(timeout_seconds=1.0, stdout_len=0, stderr_len=0), ErrorKind.TIMED_OUT),
    (SandboxSpecRejectedError("x"), ErrorKind.BAD_INPUT),
    # agent
    (AgentInterruptedError([]), ErrorKind.CANCELLED),
    # versioned-document store
    (DocumentStoreError("k", "n", "m"), ErrorKind.UPSTREAM_ERROR),
    (DocumentExistsError("k", "n"), ErrorKind.CONFLICT),
    (DocumentNotFoundError("k", "n"), ErrorKind.NOT_FOUND),
    (DocumentVersionNotFoundError("k", "n", 2), ErrorKind.NOT_FOUND),
    # presets view
    (PresetError("n", "m"), ErrorKind.UPSTREAM_ERROR),
    (PresetNotFoundError("n"), ErrorKind.NOT_FOUND),
    (PresetExistsError("n"), ErrorKind.CONFLICT),
    (PresetVersionNotFoundError("n"), ErrorKind.NOT_FOUND),
    (PresetNameConflictError("n"), ErrorKind.CONFLICT),
    # settings-profiles view
    (SettingsProfileError("n", "m"), ErrorKind.UPSTREAM_ERROR),
    (SettingsProfileNotFoundError("n"), ErrorKind.NOT_FOUND),
    (SettingsProfileExistsError("n"), ErrorKind.CONFLICT),
    (SettingsProfileVersionNotFoundError("n"), ErrorKind.NOT_FOUND),
    # tool-metadata store
    (ToolMetaError("m"), ErrorKind.UPSTREAM_ERROR),
    (FolderNotFoundError("fid"), ErrorKind.NOT_FOUND),
    (FolderNotEmptyError("fid"), ErrorKind.CONFLICT),
    (FolderCycleError("fid"), ErrorKind.CONFLICT),
    (FolderNameConflictError("n", None), ErrorKind.CONFLICT),
    # monitoring
    (MonitoringError("m"), ErrorKind.UPSTREAM_ERROR),
    (TraceNotFoundError("m"), ErrorKind.NOT_FOUND),
    (MonitoringReadNotSupportedError("m"), ErrorKind.UNAVAILABLE),
]


@pytest.mark.parametrize(
    ("exc", "expected"),
    _STAMPED_CONTRACT_ERRORS,
    ids=lambda v: v if isinstance(v, ErrorKind) else type(v).__name__,
)
def test_stamped_contract_exception_resolves_to_its_kind(exc: BaseException, expected: ErrorKind):
    assert error_kind(exc) is expected


def test_every_stamped_class_carries_the_well_known_attribute():
    # The stamp is the documented well-known class attribute, not a resolver-internal
    # detail: a consumer may read it directly.
    for exc, expected in _STAMPED_CONTRACT_ERRORS:
        assert getattr(type(exc), ERROR_KIND_ATTR) is expected


# -- Fallback registry: MRO walk, most-derived wins ----------------------------


def test_registry_resolves_subclass_via_registered_base():
    class _VendorBase(Exception):
        pass

    class _VendorDerived(_VendorBase):
        pass

    register_error_kind(_VendorBase, ErrorKind.UNAVAILABLE)
    # A subclass with no stamp of its own inherits the base's registration.
    assert error_kind(_VendorDerived()) is ErrorKind.UNAVAILABLE


def test_registry_most_derived_registration_wins():
    class _Base(Exception):
        pass

    class _Derived(_Base):
        pass

    register_error_kind(_Base, ErrorKind.UNAVAILABLE)
    register_error_kind(_Derived, ErrorKind.CONFLICT)
    # The MRO walk hits the most-derived registered ancestor first.
    assert error_kind(_Derived()) is ErrorKind.CONFLICT


def test_builtin_registry_seeds():
    assert error_kind(TimeoutError()) is ErrorKind.TIMED_OUT
    assert error_kind(ConnectionError()) is ErrorKind.UNAVAILABLE
    assert error_kind(ValueError("x")) is ErrorKind.BAD_INPUT
    assert error_kind(TypeError("x")) is ErrorKind.BAD_INPUT
    assert error_kind(PermissionError()) is ErrorKind.UNAUTHORIZED
    assert error_kind(NotImplementedError()) is ErrorKind.BAD_INPUT


# -- __cause__ recursion + the depth bound -------------------------------------


def _wrap(times: int, leaf: BaseException) -> BaseException:
    """Nest ``leaf`` under ``times`` unclassified RuntimeError ``__cause__`` links."""
    exc: BaseException = leaf
    for _ in range(times):
        wrapper = RuntimeError("opaque wrapper")
        wrapper.__cause__ = exc
        exc = wrapper
    return exc


def test_cause_walk_recovers_typed_error_beneath_wrapper():
    # The core motivation: a typed error projected under a generic wrapper (as the
    # op->tool projection raises ``ToolError`` from the OperationError) still classifies.
    typed = DocumentNotFoundError("k", "n")
    wrapped = _wrap(1, typed)
    assert error_kind(wrapped) is ErrorKind.NOT_FOUND


def test_cause_walk_at_depth_bound_still_resolves():
    # Five cause hops away is within the bound and is recovered.
    typed = DocumentExistsError("k", "n")
    assert error_kind(_wrap(5, typed)) is ErrorKind.CONFLICT


def test_cause_walk_beyond_depth_bound_gives_up():
    # Six hops away is past the bound: the resolver stops rather than spinning an
    # arbitrarily long (or cyclic) chain, and honestly returns UNKNOWN.
    typed = DocumentNotFoundError("k", "n")
    assert error_kind(_wrap(6, typed)) is ErrorKind.UNKNOWN


# -- Fallbacks -----------------------------------------------------------------


def test_bare_exception_is_unknown():
    assert error_kind(Exception("no idea")) is ErrorKind.UNKNOWN


# -- Rename immunity: the fragility this feature kills --------------------------


def test_rename_immunity_pin():
    # Subclass a stamped error under a TOTALLY different name and mutate ``__name__``
    # / ``__qualname__`` — the exact thing that breaks a ``type(e).__name__ == "..."``
    # or message-substring check. ``error_kind`` still resolves via the inherited stamp.
    class _AliasedAway(SandboxSessionNotFoundError):
        pass

    _AliasedAway.__name__ = "CompletelyUnrelatedError"
    _AliasedAway.__qualname__ = "CompletelyUnrelatedError"

    err = _AliasedAway("sid")
    assert type(err).__name__ == "CompletelyUnrelatedError"  # the name is gone
    assert type(err).__name__ != "SandboxSessionNotFoundError"
    assert error_kind(err) is ErrorKind.NOT_FOUND  # ...the kind is not


# -- Instance stamp beats class stamp ------------------------------------------


def test_instance_stamp_beats_class_stamp():
    err = ChannelInputError("boom")  # class stamp: BAD_INPUT
    assert error_kind(err) is ErrorKind.BAD_INPUT
    setattr(err, ERROR_KIND_ATTR, ErrorKind.CONFLICT)  # a per-instance override
    assert error_kind(err) is ErrorKind.CONFLICT


def test_invalid_stamp_never_masquerades_as_a_real_kind():
    # A garbage stamp is not a recognised member: it is IGNORED (never coerced into a
    # real kind and never raised). Resolution proceeds as if unstamped — here nothing
    # else classifies it, so it lands on UNKNOWN rather than a fabricated value.
    err = ValueError("real builtin below the bad stamp")  # registry would give BAD_INPUT
    setattr(err, ERROR_KIND_ATTR, "not-a-real-kind")
    # The invalid instance stamp is skipped; the builtin registry still classifies it.
    assert error_kind(err) is ErrorKind.BAD_INPUT

    bare = Exception("nothing else to go on")
    setattr(bare, ERROR_KIND_ATTR, "also-bogus")
    assert error_kind(bare) is ErrorKind.UNKNOWN
