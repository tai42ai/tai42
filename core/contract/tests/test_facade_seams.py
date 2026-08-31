"""Tests for the additive facade seams a plugin reads without importing the
skeleton: the monitoring run-attribution model + ``attribute_run`` wrapper, the
``ResolvedConnectionAuth`` model + ``resolve_connection_auth`` accessor, and the
``sandbox_exec`` preset-mechanism accessors on ``AppPresets``."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, cast, get_type_hints

from ._helpers import protocol_members

# -- Monitoring: RunAttribution + attribute_run --------------------------------


def test_run_attribution_is_a_pure_key_value_envelope():
    from tai42_contract.monitoring import RunAttribution

    empty = RunAttribution()
    assert empty.tags == []
    assert empty.metadata == {}
    # The two identity dimensions default to unset (a door that lacks either omits it).
    assert empty.user_id is None
    assert empty.session_id is None
    # No tenant/client/domain qualifier — attribution only, plus the two optional
    # backend-native identity dimensions.
    assert set(RunAttribution.model_fields) == {"tags", "metadata", "user_id", "session_id"}


class _RecordingWriter:
    """A minimal :class:`MonitoringWriter`-shaped double exercising the
    ``attribute_run`` free function over a recording ``trace_attributes``."""

    def __init__(self, trace_id: str | None):
        self._trace_id = trace_id
        self.trace_attr_calls: list[dict[str, Any]] = []
        self.entered = 0
        self.exited = 0

    def current_trace_id(self) -> str | None:
        return self._trace_id

    def trace_attributes(
        self,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> AbstractContextManager[None]:
        self.trace_attr_calls.append(
            {"name": name, "tags": tags, "metadata": metadata, "user_id": user_id, "session_id": session_id}
        )
        outer = self

        @contextmanager
        def _cm() -> Generator[None]:
            outer.entered += 1
            try:
                yield
            finally:
                outer.exited += 1

        return _cm()


def test_attribute_run_returns_the_wrapping_cm_without_entering_it():
    from tai42_contract.monitoring import RunAttribution, attribute_run
    from tai42_contract.monitoring.writer import RUN_ATTRIBUTION_TRACE_NAME, MonitoringWriter

    writer = _RecordingWriter("trace-1")
    attribution = RunAttribution(tags=["flow"], metadata={"k": "v"}, user_id="u1", session_id="s1")
    # attribute_run is a free function composing over trace_attributes; the double
    # implements only the two methods the helper reads.
    cm = attribute_run(cast(MonitoringWriter, writer), attribution)

    # It stamps under the stable run name with the attribution tags/metadata AND the
    # two identity dimensions, and RETURNS the manager — a one-shot enter/exit here
    # would tear the scope down before any span exists, so it must NOT be entered yet.
    assert writer.trace_attr_calls == [
        {
            "name": RUN_ATTRIBUTION_TRACE_NAME,
            "tags": ["flow"],
            "metadata": {"k": "v"},
            "user_id": "u1",
            "session_id": "s1",
        }
    ]
    assert writer.entered == 0
    assert writer.exited == 0

    with cm:
        assert writer.entered == 1
        assert writer.exited == 0
    assert writer.exited == 1


def test_attribute_run_stamps_even_before_a_trace_is_open():
    # GUARD REWORK: attribute_run no longer short-circuits to a nullcontext when
    # current_trace_id() is None. langfuse's propagate_attributes is OTel-context-
    # scoped, so a deposit made BEFORE the run's first span is lifted onto that span's
    # trace root when it opens INSIDE the scope. The old guard dropped exactly that
    # common case (a door deposits, then opens the first span), so it is gone: the
    # stamp is ALWAYS attempted, and its fail-safety is trace_attributes' own.
    from tai42_contract.monitoring import RunAttribution, attribute_run
    from tai42_contract.monitoring.writer import RUN_ATTRIBUTION_TRACE_NAME, MonitoringWriter

    writer = _RecordingWriter(None)  # no active trace yet
    cm = attribute_run(cast(MonitoringWriter, writer), RunAttribution(tags=["x"], user_id="u9"))
    # The stamp is composed regardless of the (absent) trace, carrying the attribution.
    assert writer.trace_attr_calls == [
        {"name": RUN_ATTRIBUTION_TRACE_NAME, "tags": ["x"], "metadata": {}, "user_id": "u9", "session_id": None}
    ]
    with cm:
        assert writer.entered == 1
    assert writer.exited == 1


# -- Connectors: ResolvedConnectionAuth + accessor -----------------------------


def test_resolved_connection_auth_defaults_empty():
    from tai42_contract.connectors import ResolvedConnectionAuth

    auth = ResolvedConnectionAuth()
    assert auth.access_token is None
    assert auth.env == {}
    assert auth.headers == {}


def test_resolved_connection_auth_masks_every_channel():
    from pydantic import SecretStr

    from tai42_contract.connectors import ResolvedConnectionAuth

    auth = ResolvedConnectionAuth.model_validate(
        {
            "access_token": "tok-AAAA",
            "env": {"STDIO_KEY": "stdio-BBBB"},
            "headers": {"Authorization": "hdr-CCCC"},
        }
    )
    assert auth.access_token is not None
    assert isinstance(auth.access_token, SecretStr)
    assert isinstance(auth.env["STDIO_KEY"], SecretStr)
    assert isinstance(auth.headers["Authorization"], SecretStr)
    assert auth.access_token.get_secret_value() == "tok-AAAA"
    # None of the three channel secrets leak into repr.
    for secret in ("tok-AAAA", "stdio-BBBB", "hdr-CCCC"):
        assert secret not in repr(auth)


def test_resolve_connection_auth_accessor_return_type_is_optional_model():
    import inspect

    from tai42_contract.app import AppConnectors
    from tai42_contract.connectors import ResolvedConnectionAuth

    hints = get_type_hints(AppConnectors.resolve_connection_auth)
    assert hints["return"] == ResolvedConnectionAuth | None
    # The accessor resolves credentials over async I/O (OAuth refresh under the connection
    # lock), so the contract types it ``async`` — callers ``await`` it and the fail-close
    # raise fires as the awaited coroutine runs.
    assert inspect.iscoroutinefunction(AppConnectors.resolve_connection_auth)


# -- Presets: the four sandbox_exec preset-mechanism accessors -----------------


def test_app_presets_declares_the_four_new_accessors():
    from tai42_contract.app import AppPresets

    members = protocol_members(AppPresets)
    for name in (
        "register_input_schema_support",
        "input_schema_support",
        "register_registration_tier",
        "registration_tier",
    ):
        assert name in members, f"AppPresets is missing {name}"


def test_input_schema_support_accessor_return_type():
    from tai42_contract.app import AppPresets
    from tai42_contract.presets import PresetInputSchemaSupport

    hints = get_type_hints(AppPresets.input_schema_support)
    assert hints["return"] == PresetInputSchemaSupport | None


def test_registration_tier_reuses_the_route_action_vocabulary():
    from tai42_contract.app import RouteAction
    from tai42_contract.app.facets import AppPresets

    # The declaration takes a RouteAction (``fenced`` = admin) and the read returns
    # RouteAction | None — the SAME admin-fence vocabulary, not a new tier type.
    reg_hints = get_type_hints(AppPresets.register_registration_tier)
    assert reg_hints["tier"] == RouteAction
    read_hints = get_type_hints(AppPresets.registration_tier)
    assert read_hints["return"] == RouteAction | None
