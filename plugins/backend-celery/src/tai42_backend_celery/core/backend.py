"""The Celery execution backend: its launchable runtimes and their bindings.

Every Celery runtime is the same umbrella CLI under a different subcommand, so
each binding differs only in the argv it builds and in how it stops. ``worker``
blocks, so the host runs it on a worker thread and this process's asyncio loop
(which reads the app's worker-bus subscription) stays alive; the host owns the
worker process's signals on every pool, and the worker installs none of its own
(:class:`~tai42_backend_celery.core.app.HostedWorker`). ``beat`` and ``flower``
run nothing else on that loop, so they block it deliberately and keep Celery's
native signal handling.

A green pool (``-P gevent``, ``-P eventlet``) needs the process patched before
anything else is imported, as Celery's own entry does it: the worker runtime
declares that patch as its process prelude, so the host starts the process
through it, and the pool's jobs run their coroutines on the host's loop.

Every runtime runs only on the objects its build produced: the Celery CLI runs on the app
object the runtime holds (:func:`_run_celery_command`), never on a module name, and the state the
build sets lives on that app or on the objects the runtime holds. A config reload re-imports this
package while the process runs. The running command reaches a fresh copy only where Celery takes
an import path rather than an object: the Redis result backend class its loader names
(:class:`~tai42_backend_celery.core.redis_seam.KitLoader`), resolved again in each thread that
first uses the result backend, and the beat's scheduler class (``beat_scheduler``), resolved when
a beat starts. Neither class keeps state of its own, so a copy resolved after a reload behaves as
the built one.

Everything host-shaped around these bindings — subcommand dispatch, the
boot-readiness gate that keeps a pool child from forking against a half-built
tool registry, the composed SIGTERM/SIGINT wiring, drain-on-cancellation, and the
prefork-pool turnover after a registry-mutating bus op — belongs to
:class:`~tai42_kit.backend.ManagedBackend`, which drives these objects.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import ClassVar, NoReturn, Self

import click
from celery import signals
from tai42_contract.app import tai42_app
from tai42_contract.backend.runtime import BackendRuntime, ExecutionMode, HttpLiveness, ProgressLiveness
from tai42_kit.backend import (
    DRAIN_MARGIN_SECONDS,
    BackendDispatchSettings,
    ManagedBackend,
    derived_queue,
    fork_safety,
    queue_label_option,
)
from tai42_kit.fork_gate import JOB_SPAN_WAIT_SECONDS, fork_gate
from tai42_kit.liveness import backend_liveness_settings

from tai42_backend_celery.core import beat_scheduler, embedded_beat, liveness_step, prefork, worker_signals
from tai42_backend_celery.core.app import HostedCelery, celery_app
from tai42_backend_celery.core.settings import celery_settings
from tai42_backend_celery.core.tasks import AsyncTask


def _leave_logging_to_the_launch(**_kwargs: object) -> None:
    """Celery's ``setup_logging`` receiver: its presence keeps Celery off this process's logging.

    The launch configured the root logger before it runs the Celery CLI and re-applies it on
    every config reload, so it is the logging's one owner. With a receiver connected, Celery
    neither replaces the root handlers nor redirects ``sys.stdout``/``sys.stderr`` into logging.
    """


def _run_celery_command(app: HostedCelery, name: str, args: Sequence[str]) -> NoReturn:
    """Run the Celery command ``name`` with ``args`` on ``app``, leaving the process's logging to the launch.

    The umbrella CLI runs as Celery's own entry runs it (its ``CELERY_`` envvar prefix, standalone,
    so it exits with ``SystemExit`` and its exit code, a usage error printed first), with its ``-A``
    option holding ``app`` itself: the CLI takes an app object as it is and looks no module up, and
    no ``-A`` on the command line nor an ``APP``/``CELERY_APP`` envvar can take its place. The option
    is given back as it was once the command exits.
    """
    from celery.bin.celery import celery

    signals.setup_logging.connect(
        _leave_logging_to_the_launch, weak=False, dispatch_uid="tai42-celery-leave-logging-to-the-launch"
    )
    app_option = next(param for param in celery.params if isinstance(param, click.Option) and param.name == "app")
    held = (app_option.default, app_option.envvar, app_option.allow_from_autoenv)
    app_option.default, app_option.envvar, app_option.allow_from_autoenv = app, None, False
    try:
        celery.main(args=[name, *args], prog_name="celery", auto_envvar_prefix="CELERY")
    finally:
        app_option.default, app_option.envvar, app_option.allow_from_autoenv = held
    # Standalone mode exits; a return would be the CLI's own defect.
    raise AssertionError("the Celery CLI returned instead of exiting")


# The pools whose jobs run as green threads: Celery's own entry patches the process for them,
# before anything else is imported, from the pool it finds in the argv.
_GREEN_POOLS = frozenset({"gevent", "eventlet"})
# The process prelude of a green pool: Celery's own patch, reading the pool off ``sys.argv``.
_GREEN_POOL_PRELUDE = "celery:maybe_patch_concurrency"


@contextmanager
def _parsed_command(name: str, args: Sequence[str]) -> Iterator[click.Context]:
    """The context the Celery command ``name`` parses ``args`` into, under the ``CELERY`` envvar prefix.

    Parsing is resilient: an argv the command rejects for a reason of its own is left to the
    Celery CLI to report when it runs.
    """
    from celery.bin.beat import beat
    from celery.bin.celery import celery
    from celery.bin.worker import worker

    command = {"worker": worker, "beat": beat}[name]
    parent = celery.context_class(celery, info_name="celery", auto_envvar_prefix="CELERY", resilient_parsing=True)
    with command.make_context(name, list(args), parent=parent, resilient_parsing=True) as ctx:
        yield ctx


def _sniffed_pool(args: Sequence[str]) -> str | None:
    """The pool Celery's patch (``celery.maybe_patch_concurrency``) reads off ``args``, ``None`` when it finds none.

    The first ``-P x``, ``--pool x`` or ``--pool=x``, read as the patch reads it; it sees no other
    spelling of the option. An option with no value after it is none.
    """
    for i, arg in enumerate(args):
        if arg.startswith("--"):
            option, sep, value = arg.partition("=")
            if option == "--pool":
                return value if sep else (args[i + 1] if i + 1 < len(args) else None)
        elif arg == "-P":
            return args[i + 1] if i + 1 < len(args) else None
    return None


def _green_pool_prelude(args: Sequence[str]) -> str | None:
    """The process prelude the worker's pool needs: Celery's patch for a green pool, else ``None``.

    The pool is read twice, as the worker's own parser reads it (every spelling of ``-P``/``--pool``,
    the last one winning, and ``CELERY_WORKER_POOL`` from the environment) and as the patch reads it
    (:func:`_sniffed_pool`). Where either finds a green pool and the two disagree, the patch would
    run for another pool than the worker starts, or for none, so the launch is refused, naming the
    spellings the patch reads.
    """
    from celery.bin.worker import worker

    pool_option = next(param for param in worker.params if param.name == "pool")
    with _parsed_command("worker", args) as ctx:
        options, _, _ = worker.make_parser(ctx).parse_args(list(args))
        parsed, source = pool_option.consume_value(ctx, options)
    sniffed = _sniffed_pool(args)
    if parsed != sniffed and (parsed in _GREEN_POOLS or sniffed in _GREEN_POOLS):
        green = parsed if parsed in _GREEN_POOLS else sniffed
        given = (
            f"CELERY_WORKER_POOL={parsed} sets it from the environment"
            if source is not None and source.name == "ENVIRONMENT"
            else f"these arguments give the worker the pool {parsed!r} and the patch {sniffed!r}"
        )
        raise ValueError(
            f"tai backend worker patches the process for a green pool before anything else is imported, reading "
            f"the pool only from the first -P {green}, --pool {green} or --pool={green} argument, and {given}; "
            f"give the pool once, in one of those spellings"
        )
    if sniffed not in _GREEN_POOLS:
        return None
    return _GREEN_POOL_PRELUDE


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

    Parsing is resilient (:func:`_parsed_command`): a configuration item the command rejects for
    another reason is left to the Celery CLI to report too.
    """
    from celery import Celery

    with _parsed_command(name, args) as ctx:
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


# The worker's one option of its own, taken off the argv before the Celery CLI sees the rest.
_QUEUE_LABEL_COMMAND = click.Command(
    "celery-worker-queue",
    params=[
        click.Option(
            ["--queue"],
            default=None,
            callback=queue_label_option,
            help="Queue label: consume <CELERY_QUEUE_NAME>.<label> instead of CELERY_QUEUE_NAME",
        )
    ],
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True, "help_option_names": []},
)


def _worker_queue_args(args: Sequence[str]) -> list[str]:
    """The Celery worker argv for the launch argv ``args``: its ``--queue`` label as Celery's ``-Q``.

    ``--queue <label>`` is taken out (a label outside the grammar raises ``click.BadParameter``)
    and the worker consumes the queue it derives, ``<CELERY_QUEUE_NAME>.<label>``, or the settings
    queue without one; every other argument reaches the Celery CLI in its order. Celery's own
    ``-Q``/``--queues`` is refused, from the argv or as ``CELERY_WORKER_QUEUES`` from the
    environment: a worker consumes the queue of its label alone.
    """
    with _QUEUE_LABEL_COMMAND.make_context("worker", list(args)) as ctx:
        label: str | None = ctx.params["queue"]
        celery_args = list(ctx.args)
    with _parsed_command("worker", celery_args) as ctx:
        queues = ctx.params.get("queues")
        source = ctx.get_parameter_source("queues")
    if queues and source is not None and source.name in {"COMMANDLINE", "ENVIRONMENT"}:
        # Celery parses the option into a list of names; the refusal names them as they were given.
        given = ",".join(queues)
        spelling = f"{ctx.auto_envvar_prefix}_QUEUES={given}" if source.name == "ENVIRONMENT" else f"-Q {given}"
        raise ValueError(
            f"tai backend worker consumes the queue of its --queue label (CELERY_QUEUE_NAME without one); "
            f"drop {spelling} and give the label as --queue <label>"
        )
    return [*celery_args, "-Q", derived_queue(celery_settings().queue_name, label)]


class _CeleryRuntime(BackendRuntime):
    """What every Celery runtime shares.

    It parses nothing of its own but the worker's and the beat's refusal of the one option that
    would replace their scheduler (the options belong to the Celery CLI, which validates them),
    and starts by handing that CLI its own subcommand plus the rest of the launch argv, on the app
    the runtime holds.

    The runtime takes the app as it is made, before any build: the launch wires the turnover and
    the stop requests to the runtime before its build runs, and they act on the same app.
    """

    def __init__(self, args: Sequence[str]) -> None:
        self._args = list(args)
        self._app = celery_app

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        return cls(args)


class CeleryWorkerRuntime(_CeleryRuntime):
    """The consuming runtime: a Celery worker whose prefork children persist across jobs.

    So a registry mutation obliges a pool turnover. On a green pool the worker declares Celery's
    patch as its process prelude (:func:`_green_pool_prelude`).
    """

    name = "worker"
    mode = ExecutionMode.worker_thread
    consumes_work = True
    pool_turnover_required = True
    liveness = ProgressLiveness()

    def __init__(self, args: Sequence[str]) -> None:
        """Hold the worker's argv, the app, and the pool turnover of the worker that runs the app."""
        super().__init__(args)
        self._turnover = prefork.PoolTurnover(self._app)

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        """Take the worker's argv, refusing ``--scheduler``, ``-Q`` and a green pool the launch cannot start.

        ``--queue <label>`` becomes Celery's ``-Q`` of the derived queue (:func:`_worker_queue_args`).
        An embedded beat (``-B``) runs the reporting scheduler. A green pool sets the process prelude;
        it is refused when its patch cannot read it or its library is not installed
        (:func:`_green_pool_prelude`).
        """
        celery_args = _worker_queue_args(args)
        _refuse_scheduler_option(cls.name, celery_args)
        runtime = cls(celery_args)
        runtime.process_prelude = _green_pool_prelude(celery_args)
        return runtime

    async def build(self) -> None:
        """Hand over the host's loop, prepare the turnover, connect the receivers, add the steps, guard forks.

        All on the app the runtime holds, and all before the readiness gate. A green pool's jobs run
        their coroutines on the host's loop, the one loop the process's OS thread runs; the other
        pools' jobs run loops of their own and leave it.

        Connecting Celery's setup/ready signals and adding a consumer bootstep must happen before the worker
        boots past them, and none of them consumes work. The embedded beat's reporter is handed over
        here too, before ``-B`` forks the beat from this process. The fork gate's guard then holds a job
        span around every fork this process makes — the prefork pool's children at start, on respawn and
        on turnover, and the ``-B`` beat child — so none is forked into a config reload's re-import,
        and every child evicts the monitoring client it inherited. A fork waiting on the re-import
        reports the worker's progress each tick only on the hub's own thread (:meth:`run_blocking`).
        The worker's signal receivers (:mod:`~tai42_backend_celery.core.worker_signals`) are connected
        here, for the app, never at import.
        """
        AsyncTask.share_loop(self._app, asyncio.get_running_loop())
        self._turnover.register()
        worker_signals.connect(self._app)
        embedded_beat.register(self._app, backend_liveness_settings())
        liveness_step.register(self._app, self.report_progress, task_timeout=celery_settings().task_timeout)
        fork_safety.guard_forks(fork_gate, timeout=JOB_SPAN_WAIT_SECONDS)

    def run_blocking(self) -> None:
        """Run the Celery worker, blocking this thread until it exits.

        The worker's consumer loop (the hub on an async transport) runs on this thread, so it runs
        as the fork gate's engine loop: a fork or a ``solo`` job waiting on the gate here holds up
        the loop's next pass and reports the worker's progress each tick meanwhile; a wait on any
        other thread reports nothing.
        However the worker exits, it has left: a pool turnover still in flight is abandoned.
        """
        try:
            with liveness_step.hub_as_engine_loop(self._app):
                _run_celery_command(self._app, self.name, self._args)
        finally:
            self._turnover.mark_worker_leaving()

    def request_drain(self) -> None:
        """Ask the in-process worker to stop warmly, by driving ``celery.worker.state`` directly.

        The worker runs off the main thread, where Celery skips its own signal handlers.
        Idempotent — a second request is the same request, and only a flag that was never set is set.
        The worker is leaving from here on, so a pool turnover in flight is abandoned: the
        stopping consumer would never answer its control commands.
        """
        from celery.worker import state

        self._turnover.mark_worker_leaving()
        if state.should_stop is None:
            state.should_stop = 0

    def request_terminate(self) -> None:
        """Ask the in-process worker for a cold stop, by driving ``celery.worker.state`` directly.

        Celery acts on it only while its warm stop has not begun: it then runs its cold stop, which
        stops the pool without waiting for in-flight tasks. Once Celery has begun its warm stop, the
        request is not acted on, and the warm stop goes on as it was.
        """
        from celery.worker import state

        state.should_terminate = 0

    async def turn_over_pool(self, *, reason: str, budget: float) -> None:
        """Re-fork this worker's prefork pool and confirm it, off the serving loop.

        The turnover's control I/O blocks. A raise fails the op named by ``reason``, which is the point:
        an unconfirmed turnover means a child may still serve the pre-mutation registry.
        """
        await asyncio.to_thread(self._turnover.turnover_local_pool, reason, budget)

    async def aclose(self) -> None:
        """Reap the embedded beat and remove its liveness file, off the loop: the reap waits on the beat."""
        await asyncio.to_thread(embedded_beat.release, self._app)


class _CeleryInlineRuntime(_CeleryRuntime):
    """A Celery command that owns its process.

    Nothing else runs on this loop, so it blocks the loop deliberately and keeps Celery's native signal
    handling.
    """

    mode = ExecutionMode.inline

    def run_blocking(self) -> None:
        _run_celery_command(self._app, self.name, self._args)


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
        """Hand the app's beat this runtime's progress reporter before the beat starts ticking."""
        beat_scheduler.register(self._app, self.report_progress)


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
        return float(celery_settings().task_timeout) + DRAIN_MARGIN_SECONDS


# Plain call (not decorator) so ``CeleryBackend`` keeps its plain class type.
tai42_app.backends.register_backend(CeleryBackend)
