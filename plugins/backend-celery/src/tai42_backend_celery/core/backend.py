"""The Celery execution backend: its launchable runtimes and their bindings.

Every Celery runtime is the same umbrella CLI under a different subcommand, so
each binding differs only in the argv it builds and in how it stops. ``worker``
blocks, so the host runs it on a worker thread and this process's asyncio loop
(which reads the app's worker-bus subscription) stays alive; ``beat`` and
``flower`` run nothing else on that loop, so they block it deliberately and keep
Celery's native signal handling.

Everything host-shaped around these bindings — subcommand dispatch, the
boot-readiness gate that keeps a pool child from forking against a half-built
tool registry, the composed SIGTERM/SIGINT wiring, drain-on-cancellation, and the
prefork-pool turnover after a registry-mutating bus op — belongs to
:class:`~tai42_kit.backend.ManagedBackend`, which drives these objects.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Mapping, Sequence
from typing import ClassVar, NoReturn, Self

from tai42_contract.app import tai42_app
from tai42_contract.backend.runtime import BackendRuntime, ExecutionMode, HttpLiveness, ProgressLiveness
from tai42_kit.backend import BackendDispatchSettings, ManagedBackend
from tai42_kit.liveness import backend_liveness_settings

from tai42_backend_celery.core import prefork
from tai42_backend_celery.core.app import celery_app
from tai42_backend_celery.core.settings import celery_settings

# The Celery application module handed to the Celery CLI (`-A`).
_CELERY_APP_PATH = "tai42_backend_celery.core.app"

# Margin the drain budget adds on top of the engine's own job budget: the warm
# drain has to outlast the longest in-flight job AND the engine's own teardown, or
# the wait is abandoned on exactly the work it exists to protect.
_DRAIN_MARGIN_SECONDS = 30.0


def _celery_cli_main() -> None:
    """Run the Celery umbrella CLI against ``sys.argv``."""
    from celery.__main__ import main as celery_main

    celery_main()


def _refuse_scheduler_option(name: str, args: Sequence[str]) -> None:
    """Refuse every way of handing the Celery command ``name`` a scheduler class of its own.

    A scheduler class reaches the command three ways, and each is read the way Celery reads it:

    - the ``--scheduler`` option (the beat's ``-S``) in ``args``: the command's own parser reads
      the argv, so every spelling it accepts is caught (``-S x``, ``-Sx``, ``--scheduler=x``,
      combined short flags) and nothing else it accepts is;
    - the same option from the environment: the Celery CLI runs with the ``CELERY`` envvar
      prefix, so ``CELERY_WORKER_SCHEDULER`` and ``CELERY_BEAT_SCHEDULER`` fill it;
    - the ``beat_scheduler`` setting as command-line configuration (``beat_scheduler=x``,
      ``beat.scheduler=x``, in any case), which the command applies to the app's configuration,
      read with Celery's own command-line configuration parser.

    Parsing is resilient: an argv or a configuration item the command rejects for another reason
    is left to the Celery CLI to report.
    """
    from celery import Celery
    from celery.bin.beat import beat
    from celery.bin.celery import celery
    from celery.bin.worker import worker

    command = {"worker": worker, "beat": beat}[name]
    parent = celery.context_class(celery, info_name="celery", auto_envvar_prefix="CELERY", resilient_parsing=True)
    with command.make_context(name, list(args), parent=parent, resilient_parsing=True) as ctx:
        scheduler = ctx.params.get("scheduler")
        source = ctx.get_parameter_source("scheduler")
        source_name = source.name if source is not None else None
        envvar = f"{ctx.auto_envvar_prefix}_SCHEDULER"
        extra_args = list(ctx.args)
    if scheduler and source_name == "ENVIRONMENT":
        _raise_scheduler_refusal(name, f"{envvar}={scheduler}")
    if scheduler and source_name == "COMMANDLINE":
        _raise_scheduler_refusal(name, f"--scheduler {scheduler}")

    # The worker applies its command-line configuration in the ``worker`` namespace, the beat in
    # Celery's default one; both resolve ``beat_scheduler`` / ``beat.scheduler`` to the same setting.
    namespace = "worker" if name == "worker" else "celery"
    parse = Celery(set_as_current=False).loader.cmdline_config_parser
    for item in extra_args:
        try:
            setting = parse([item], namespace)
        except (KeyError, ValueError):
            continue
        if "beat_scheduler" in setting:
            _raise_scheduler_refusal(name, item)


def _raise_scheduler_refusal(name: str, spelling: str) -> NoReturn:
    raise ValueError(
        f"tai backend {name} runs RedBeat through its progress-reporting scheduler, which reports the "
        f"process's liveness; drop {spelling}"
    )


class _CeleryRuntime(BackendRuntime):
    """What every Celery runtime shares.

    It parses nothing of its own but the worker's and the beat's refusal of the one option that
    would replace their scheduler (the options belong to the Celery CLI, which validates them),
    and starts by handing that CLI its own subcommand plus the rest of the launch argv.
    """

    def __init__(self, args: Sequence[str]) -> None:
        self._args = list(args)

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        return cls(args)

    def _set_argv(self) -> None:
        """Point ``sys.argv`` at this runtime's Celery command.

        The CLI reads the process argv rather than taking arguments.
        """
        sys.argv = ["celery", "-A", _CELERY_APP_PATH, self.name, *self._args]


class CeleryWorkerRuntime(_CeleryRuntime):
    """The consuming runtime: a Celery worker whose prefork children persist across jobs.

    So a registry mutation obliges a pool turnover.
    """

    name = "worker"
    mode = ExecutionMode.worker_thread
    consumes_work = True
    pool_turnover_required = True
    liveness = ProgressLiveness()

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        """Take the worker's argv, refusing ``--scheduler``: an embedded beat (``-B``) runs the reporting scheduler."""
        _refuse_scheduler_option(cls.name, args)
        return cls(args)

    async def build(self) -> None:
        """Set the CLI's argv, prepare the pool turnover and add the progress step, before the boot-readiness gate.

        Connecting Celery's setup/ready signals and adding a consumer bootstep must happen before the worker
        boots past them, and none of them consumes work. The embedded beat's reporter is handed over
        here too, before ``-B`` forks the beat from this process.
        """
        # Imported on the path that runs it: only a worker process adds the consumer step.
        from tai42_backend_celery.core import embedded_beat, liveness_step

        self._set_argv()
        prefork.register()
        embedded_beat.register(celery_app, backend_liveness_settings())
        liveness_step.register(celery_app, self.report_progress, task_timeout=celery_settings().task_timeout)

    def run_blocking(self) -> None:
        """Run the Celery worker, blocking this thread until it exits."""
        _celery_cli_main()

    def request_drain(self) -> None:
        """Ask the in-process worker to stop warmly, by driving ``celery.worker.state`` directly.

        The worker runs off the main thread, where Celery skips its own signal handlers.
        Idempotent — a second request is the same request, and only a flag that was never set is set.
        """
        from celery.worker import state

        if state.should_stop is None:
            state.should_stop = 0

    def request_terminate(self) -> None:
        """Cold stop: abandon in-flight tasks now."""
        from celery.worker import state

        state.should_terminate = 0

    async def turn_over_pool(self, *, reason: str, budget: float) -> None:
        """Re-fork this worker's prefork pool and confirm it, off the serving loop.

        The turnover's control I/O blocks. A raise fails the op named by ``reason``, which is the point:
        an unconfirmed turnover means a child may still serve the pre-mutation registry.
        """
        await asyncio.to_thread(prefork.turnover_local_pool, reason, budget)

    async def aclose(self) -> None:
        """Reap the embedded beat and remove its liveness file, off the loop: the reap waits on the beat."""
        from tai42_backend_celery.core import embedded_beat

        await asyncio.to_thread(embedded_beat.release)


class _CeleryInlineRuntime(_CeleryRuntime):
    """A Celery command that owns its process.

    Nothing else runs on this loop, so it blocks the loop deliberately and keeps Celery's native signal
    handling.
    """

    mode = ExecutionMode.inline

    def run_blocking(self) -> None:
        self._set_argv()
        _celery_cli_main()


class CeleryBeatRuntime(_CeleryInlineRuntime):
    """The RedBeat scheduler, reporting each tick of the beat loop as this process's progress."""

    name = "beat"
    liveness = ProgressLiveness()

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        """Take the beat's argv, refusing ``-S``/``--scheduler``: the beat runs the reporting scheduler."""
        _refuse_scheduler_option(cls.name, args)
        return cls(args)

    async def build(self) -> None:
        """Hand the beat scheduler this runtime's progress reporter before the beat starts ticking."""
        # Imported on the path that runs it: only a beat process loads the scheduler.
        from tai42_backend_celery.core import beat_scheduler

        beat_scheduler.register(self.report_progress)


class CeleryFlowerRuntime(_CeleryInlineRuntime):
    """The Flower dashboard.

    An orchestrator probes it over HTTP on ``/healthcheck``, which Flower answers without
    authentication.
    """

    name = "flower"
    liveness = HttpLiveness("/healthcheck")


class CeleryBackend(ManagedBackend):
    """Celery execution backend (see the module docstring for the design)."""

    label = "celery"
    runtimes: ClassVar[Mapping[str, type[BackendRuntime]]] = {
        "worker": CeleryWorkerRuntime,
        "beat": CeleryBeatRuntime,
        "flower": CeleryFlowerRuntime,
    }

    @property
    def dispatch_settings(self) -> BackendDispatchSettings:
        """The ``CELERY_`` env group.

        Its ``manifest_key`` is the env var the re-forked pool children read the live manifest from.
        """
        return celery_settings()

    @property
    def drain_timeout(self) -> float:
        """The base's drain budget, sized to THIS deployment's Celery task budget.

        Read live rather than frozen at import, so a settings epoch flip that
        widens ``CELERY_TASK_TIMEOUT`` widens the drain with it instead of leaving
        a warm shutdown abandoning tasks it should have waited for.
        """
        return float(celery_settings().task_timeout) + _DRAIN_MARGIN_SECONDS


# Plain call (not decorator) so ``CeleryBackend`` keeps its plain class type.
tai42_app.backends.register_backend(CeleryBackend)
