"""The published pytest plugin: session infra, the ``fresh_stack`` factory, the
variant-run identification, and the failure-diagnostics hook.

Registered as the ``tai42-e2e`` pytest11 entry point, so any repo that installs
``tai42-e2e`` gets these fixtures without copying conftest wiring. External stack
suites (a product shipping its own ``StackConfig`` builders) build on ``infra``
and ``fresh_stack`` exactly as this repo's own ``tests/conftest.py`` does.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

import pytest

from tai42_e2e import diagnostics
from tai42_e2e.booting import allocate_and_build
from tai42_e2e.harness import InfraUnavailable, connect_infra
from tai42_e2e.manifests import build_core_stack
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import Infra, StackConfig, StackResources, TaiStack
from tai42_e2e.tcprelay import TcpRelay
from tai42_e2e.variants import Variants


def gated_collect_ignore(gates: Mapping[str, bool]) -> list[str]:
    """``collect_ignore_glob`` entries for each opt-in suite dir whose gate is off.

    A conftest passes ``{"<dir>": <settings-flag>, ...}``; the ``<dir>/*`` glob is
    ignored at collection whenever its flag is ``False``, so a suite that needs an
    external prerequisite never imports until its flag is set."""
    return [f"{name}/*" for name, on in gates.items() if not on]


class RealSelectionError(Exception):
    """A ``TAI_E2E_REAL`` selection that cannot run as asked: a required credential
    is absent, or an inbound seam is selected without ``E2E_PUBLIC_BASE_URL``.
    Raised at collection, never downgraded to a skip or a silent mock — choosing
    mock is the operator's explicit lever (drop the seam from ``TAI_E2E_REAL``),
    never an automatic fallback."""


def assert_real_selection_ready(settings: HarnessSettings, environ: Mapping[str, str]) -> None:
    """The loud-fail gate for the REAL/MOCK switch. For every seam selected real,
    require its credentials (``REAL_SERVICES``) present in ``environ`` and, if any
    selected seam is inbound, require ``E2E_PUBLIC_BASE_URL``. Any gap raises
    :class:`RealSelectionError` naming the exact missing env vars and services.

    Distinct from ``gated_collect_ignore`` (which only SKIPs opt-in suites): this
    is the additive loud-fail the switch introduces — a real leg never boots half
    configured."""
    problems: list[str] = []
    for service, missing in settings.missing_real_creds(environ).items():
        problems.append(f"{service}: missing required env var(s) {', '.join(missing)}")
    inbound = settings.real_inbound_services
    if inbound and not settings.public_base_url:
        problems.append(
            f"inbound real seam(s) {', '.join(sorted(inbound))} require E2E_PUBLIC_BASE_URL, which is unset"
        )
    # The storage seams are dual-knob: a ``storage-s3`` / ``storage-github`` name on
    # TAI_E2E_REAL passes the cred gate but runs the hermetic mock unless the storage
    # AXIS (TAI_E2E_STORAGE) is also set to the matching real variant. Fail loudly on
    # that mismatch rather than silently no-op back to the mock.
    problems.extend(settings.storage_axis_mismatches())
    if problems:
        raise RealSelectionError(
            "TAI_E2E_REAL selects real leg(s) that are not fully configured. "
            "Set the missing env vars, or drop the seam from TAI_E2E_REAL to run it mock:\n  " + "\n  ".join(problems)
        )


def pytest_configure(config: pytest.Config) -> None:
    """Enforce the REAL/MOCK switch at session start (published to every repo that
    installs the plugin). With ``TAI_E2E_REAL`` empty this is a no-op, so the mock
    suite is byte-for-byte today's behavior; a real selection missing creds or the
    public base URL aborts here, before any stack boots, naming the gap."""
    del config
    assert_real_selection_ready(HarnessSettings(), os.environ)


def pytest_report_header() -> str:
    """Stamp the variant triple this process runs under into the run header so a
    console log or CI artifact is self-identifying."""
    s = HarnessSettings()
    return f"tai42-e2e variants: backend={s.backend} identity={s.identity} storage={s.storage}"


@pytest.fixture(scope="session", autouse=True)
def _stamp_variant_properties(record_testsuite_property: Callable[[str, object], None]) -> None:
    """Record the variant triple as junit testsuite properties so a CI xml
    artifact carries which backend/identity/storage leg produced it."""
    s = HarnessSettings()
    record_testsuite_property("tai42_e2e_backend", s.backend)
    record_testsuite_property("tai42_e2e_identity", s.identity)
    record_testsuite_property("tai42_e2e_storage", s.storage)


@pytest.fixture(scope="session")
def harness_settings() -> HarnessSettings:
    return HarnessSettings()


@pytest.fixture(scope="session")
def infra(harness_settings: HarnessSettings) -> Iterator[Infra]:
    """Verify Redis + Postgres reachability (loudly, with the compose hint on
    failure), create the DDL-applied template DB, and expose the admin clients."""
    try:
        infra = connect_infra(harness_settings)
    except InfraUnavailable as exc:
        pytest.exit(str(exc), returncode=1)
    try:
        yield infra
    finally:
        infra.redis.close()
        if infra.checkpoint_redis is not None:
            infra.checkpoint_redis.close()


@pytest.fixture
def fresh_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Callable[..., TaiStack]]:
    """A function-scoped factory for tests that mutate global stack state
    (restarts, config races, the M3 CWD variants). Every stack it builds is torn
    down when the test ends."""
    booted: list[TaiStack] = []

    def make(
        builder: Callable[[StackResources, Variants], StackConfig] = build_core_stack,
        *,
        cwd_overrides: dict[str, str] | None = None,
        env_overrides: dict[str, str] | None = None,
        resource_kwargs: dict[str, Any] | None = None,
        relays: Sequence[TcpRelay] | None = None,
        allocate_checkpoint_db: bool = False,
    ) -> TaiStack:
        root = tmp_path_factory.mktemp("fresh")
        # Relays the caller started to front this stack's stores: the stack adopts
        # them so teardown stops and leak-checks them, and an allocation/build
        # failure here stops them too rather than leaving a listener behind.
        owned_relays = list(relays or [])
        try:
            resources, config = allocate_and_build(infra, root, builder, resource_kwargs, allocate_checkpoint_db)
        except BaseException:
            for relay in owned_relays:
                relay.stop()
            raise
        if cwd_overrides or env_overrides:
            from dataclasses import replace

            new_env = {**config.env, **(env_overrides or {})}
            config = replace(config, env=new_env, cwd_overrides=cwd_overrides or config.cwd_overrides)
        stack = TaiStack(config, infra, resources, root)
        for relay in owned_relays:
            stack.attach_relay(relay)
        try:
            stack.boot()
        except BaseException:
            # A boot failure must still reap the resources allocated above (stack DB, Redis
            # index, broker vhost lease); the stack is registered for teardown only after a
            # successful boot.
            stack.teardown()
            raise
        booted.append(stack)
        # Track it so a failure in a fresh-stack test (restarts, config races,
        # CWD variants) attaches this stack's diagnostics to the report.
        diagnostics.register(stack)
        return stack

    try:
        yield make
    finally:
        errors: list[str] = []
        for stack in booted:
            diagnostics.unregister(stack)
            try:
                stack.teardown()
            except Exception as exc:
                errors.append(repr(exc))
        if errors:
            raise RuntimeError("fresh_stack teardown leaks: " + "; ".join(errors))


@pytest.fixture
def uniq() -> Callable[[str], str]:
    """A unique-name helper: ``uniq("preset")`` -> ``preset_<8hex>``. Tests share
    a stack, so every resource they create must be uniquely named."""

    def _uniq(prefix: str = "e2e") -> str:
        return f"{prefix}_{uuid.uuid4().hex[:8]}"

    return _uniq


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        dump = diagnostics.report()
        if dump:
            report.sections.append(("tai42-e2e stack diagnostics", dump))
