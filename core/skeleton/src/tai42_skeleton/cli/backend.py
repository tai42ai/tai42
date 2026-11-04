"""The ``backend`` CLI command — runs an execution-backend runtime (worker / beat / dashboard)."""

import asyncio
import functools
import logging
import os

import click
from dotenv import find_dotenv
from tai42_cli import process_prelude
from tai42_cli.boot_env import load_boot_dotenv
from tai42_contract.backend import CONSUMING_RUNTIME, PROCESS_PRELUDE_ENV, ProcessPreludeRequired
from tai42_kit.backend.reshaped_threads import bind_future_chaining
from tai42_kit.logging import install_record_redaction, logging_settings, setup_logging
from tai42_kit.signals import STOP_SIGNALS, signal_chain

from tai42_skeleton.app import instance
from tai42_skeleton.app.boot_rules import (
    require_bus_for_backend,
    require_bus_for_shared_config,
    require_hooks_store_for_backend,
)
from tai42_skeleton.app.bus import WorkerKind
from tai42_skeleton.backend.settings import base_backend_settings
from tai42_skeleton.config.config_mode import ConfigMode, config_mode
from tai42_skeleton.config.deployment_env import captured_deployment_env
from tai42_skeleton.connectors.meta_log_redactor import register_connector_log_redaction

logger = logging.getLogger(__name__)


async def run_backend(extra_args):
    """Boot the app and launch the execution-backend runtime, tearing down cleanly on a stop signal."""
    # Handle each stop signal — the orchestrator's SIGTERM and the terminal's SIGINT —
    # by cancelling this main task: the cancellation unwinds through ``app_context``'s
    # ``finally``, which runs the stop (shutdown handlers + ``_teardown_resources``),
    # where an unhandled SIGTERM would kill the process outright and skip it. A later
    # stop signal while that stop runs cancels it in turn and cuts it short; the stop
    # logs how it ended either way. Unix/uvloop only, matching the CLI's target.
    #
    # It goes through the composed chain because a backend runtime installs its own
    # drain handler on the same signals; asyncio's ``add_signal_handler`` is
    # last-writer-wins, so a direct install here would be destroyed by it — taking
    # the signal-driven stop with it.
    main_task = asyncio.current_task()
    if main_task is None:
        raise AssertionError
    for sig in STOP_SIGNALS:
        signal_chain.add(sig, main_task.cancel, name="backend-main-task-cancel")

    # Worker-bus boot rule (fail loud, naming TAI_BUS_REDIS_URL). Run BEFORE the app
    # (and its config manager) is built, so a shared-config busless backend refuses on
    # the bus var rather than failing first on the external provider's connection.
    require_bus_for_shared_config()

    app = instance.build_app()
    # The host observes the stop's start: the first stop signal begins this worker's leaving, so
    # its census row reads ``recycling`` for the whole drain and a roll that reaches it meanwhile
    # finds it already leaving. A later one begins nothing again.
    for sig in STOP_SIGNALS:
        signal_chain.add(sig, functools.partial(app.stop_began, f"{sig.name} received"), name="backend-stop-began")
    # This CLI-configured backend process keeps its root logger on the applied env:
    # once the boot env bridge has applied the stored env, and across config reloads;
    # fastmcp's settings are read again once the bridge has applied it. The public
    # factory registers nothing, so an embedded host's root logger and fastmcp
    # settings stay untouched.
    instance.register_cli_process_state()
    # This CLI-owned process owns its whole logging surface, so the connector-secret
    # redactor covers every record in the process, not just the tai logger family.
    register_connector_log_redaction()
    install_record_redaction(scope="process")
    logger.info("Configuration mode: %s", config_mode())
    # Build the backend's worker bus, bridge the persisted env store into ``os.environ``
    # and resolve the manifest under it in one seam, so a store-only ``!ENV ${VAR}``
    # marker resolves to its real value — including in the resolved manifest dumped
    # below for fork children.
    manifest = await app.lifecycle.read_boot_manifest(kind=WorkerKind.backend)
    # Read after the bridge, so the key is the one the applied env names, as the forked
    # children read it. Only the key NAME is needed below, and this coroutine frame is
    # suspended for the process lifetime: holding the settings MODEL here would keep a
    # boot-epoch instance reachable past every reload's cache reset, which the
    # stale-settings sweep reports (truthfully) as a leak. Retain the plain string instead.
    manifest_key = base_backend_settings().manifest_key

    # A dedicated backend runtime always registers a task backend, so it requires the
    # worker bus (the backend-runtime and server processes must converge on config
    # reloads) and a shared hooks registry (the events this process raises are matched
    # against its own registry). Refuse loudly, naming the setting, before entering
    # app_context.
    require_bus_for_backend(manifest)
    require_hooks_store_for_backend(manifest)

    # The resolved (``!ENV``-expanded) manifest is placed into this env var for the
    # WORKER runtime only — the one that forks job children. It carries the resolved
    # secrets and is readable by same-user process inspection, so it is not exported
    # into runtimes that fork nothing (a scheduler ``beat``, a dashboard): those need
    # no manifest view, and widening the export would spread the secrets for nothing.
    # The sniff runs BEFORE the plugin module is imported, so it cannot ask the
    # backend which of its runtimes consumes work: the name is a host/backend
    # agreement the contract holds.
    if CONSUMING_RUNTIME in extra_args:
        os.environ[manifest_key] = manifest.model_dump_json()

    # Every backend invocation launches inside ``app_context``: ``start()`` binds
    # the global ``tai42_app`` handle and THEN imports the manifest's
    # ``backend_module``, so a plugin module that registers its Backend at import
    # (``tai42_app.backends.register_backend``) lands the registration in the bound
    # app's holder before ``run_backend`` reaches the registered backend's
    # ``launch`` — on the worker path and the non-worker (beat/flower-style) path
    # alike. Importing the plugin module outside the context would hit the
    # unbound handle and crash.
    async with app.app_context(manifest=manifest):
        await app.run_backend(extra_args)


@click.command("backend", context_settings={"ignore_unknown_options": True})
@click.option(
    "--manifest-path",
    help="Path to a YAML manifest file (file mode only; ignored under a non-file config mode).",
    default=None,
    show_default=True,
)
@click.argument("extra_args", nargs=-1)
@click.pass_context
def main(ctx, manifest_path, extra_args):
    """Run a tai execution-backend runtime (worker / beat / dashboard).

    \b
    A backend runtime always registers a task backend, so it requires the worker
    bus — set TAI_BUS_REDIS_URL. The backend-runtime and server processes must
    converge on config reloads, so a busless backend is refused at boot (and again
    on any reload that would leave a backend without the bus). A non-file
    TAI_CONFIG_MODE is likewise refused without the bus.

    \b
    It also requires the shared hooks registry — set HOOKS_REDIS_URL (or
    TAI_DEFAULT_REDIS_URL). The events this process raises are matched against
    its own registry, and an in-memory one holds none of the hooks registered
    through the server, so a backend over it is refused at boot and on reload.

    \b
    A runtime whose engine must patch the interpreter before anything else is
    imported (a green worker pool) starts once, reads its configuration, and then
    restarts itself in place with that patch applied first: same process id, same
    arguments, one "restarting with process prelude" log line.
    """  # noqa: D301 - a plain backslash-b is Click's no-rewrap marker
    if config_mode() == ConfigMode.file:
        load_boot_dotenv(find_dotenv())

    # Configure the root logger at process start, right after the env bootstrap, so
    # ``TAI_LOG_LEVEL`` (from a local ``.env`` or the environment) takes effect for
    # the backend worker's logging.
    setup_logging(logging_settings())

    if manifest_path:
        os.environ["TAI_MANIFEST_PATH"] = manifest_path

    # Label this process's tool metrics as the backend runtime so scrapes can tell
    # backend-run increments from the server's. ``setdefault`` respects an explicit
    # operator override.
    os.environ.setdefault("PROMETHEUS_RUNTIME", "backend")

    # Publish the multiproc dir BEFORE the first ``prometheus_client`` import: the
    # library freezes its value backend from this env var at that first import, and
    # importing ``routers.prometheus`` pulls ``prometheus_client`` in. So set the env
    # first, THEN import the assert helper. This process writes tool counters, so
    # verify the mmap backend froze rather than the in-process mutex whose counters
    # no scrape sees.
    from tai42_skeleton.routers.metrics_settings import activate_multiproc_env

    activate_multiproc_env()

    # This process writes tool counters into the shared multiproc dir, so the dir must
    # exist before the first write. The backend worker is a top-level process with its
    # own filesystem — it cannot rely on the mcp_app master's once-per-run wipe having
    # created the dir — so ensure it here (non-wiping: never destroys a populated dir),
    # then verify the mmap backend froze rather than the in-process mutex whose counters
    # no scrape sees.
    from tai42_skeleton.routers.prometheus import assert_multiproc_value_class, init_prometheus_multiproc_dir

    init_prometheus_multiproc_dir()
    assert_multiproc_value_class()

    # A process started through a process prelude runs on the standard library's loop: the
    # prelude replaced the interpreter's blocking primitives, and a native loop bypasses them.
    # The prelude may also run the process's threads on one OS thread, so asyncio's future
    # chaining is bound to tell those threads apart before any loop of the process runs.
    if os.environ.get(PROCESS_PRELUDE_ENV):
        bind_future_chaining()
        loop = asyncio
    else:
        try:
            import uvloop

            loop = uvloop
        except ImportError:
            loop = asyncio

    all_args = list(ctx.args) + list(extra_args)
    try:
        loop.run(run_backend(all_args))
    except asyncio.CancelledError:
        # Only the stop-signal handler cancels the main task, so this is the deliberate
        # signal-driven stop. Whether that stop finished or a later signal cut it short is
        # the stop's own line to say (``app_context``'s ``finally``), and a signal landing
        # before ``app_context`` was entered had no stop to run. Convert it into a clean exit.
        logger.info("stop signal received — backend worker exiting")
    except ProcessPreludeRequired as exc:
        # The launch refused the runtime before starting any of it, and the app context has
        # unwound. Start this process again, in place, through the prelude the runtime declares;
        # the new image runs the prelude first, so its launch proceeds. It starts with the
        # deployment env, the one this process was started with: its own boot applies the stored
        # env again, and no stored key this boot applied passes for one the deployment sets.
        # Exec runs no exit hook, so the logging is flushed and closed here.
        logger.info("restarting with process prelude %s", exc.prelude)
        logging.shutdown()
        process_prelude.relaunch(exc.prelude, captured_deployment_env())
