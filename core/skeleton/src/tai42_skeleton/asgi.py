"""Public ASGI application factory for a user-owned tai server process.

``create_app`` is the programmatic entry point for embedding the tai MCP
application inside a host-owned ASGI process — a plain ``uvicorn main:app`` run or
a mount inside an existing FastAPI/Starlette host:

```python
from tai42_skeleton.asgi import create_app

app = create_app(manifest_path="manifest.yml")
```

The returned Starlette app carries the full worker lifespan (manifest load,
``app_context``, transport selection, and the inner FastMCP lifespan), so the host
serves it exactly as the runtime CLI serves its own workers.

One app per process. The ``tai42_app`` contract handle and the built app are
process-global singletons, so a second ``create_app`` lifespan entered while one
is already active raises loudly rather than silently rebinding the handle;
sequential lifespans in one process (enter -> exit -> enter) stay legal.

Deliberately CLI-owned and absent here: root-logger configuration (an embedded
app never touches the host's logging), the Prometheus multiprocess metrics
environment (the embedded app serves its own in-app ``/metrics`` route in
IN-PROCESS mode, rendering this process's default registry; it does not set up the
MULTIPROCESS dir — a separate reader over a shared multiproc dir is yours to run),
and process-fleet orchestration. Fleet config-reload fan-out is the app's internal
worker bus: an embedded worker joins it like any other process when
``TAI_BUS_REDIS_URL`` is set (the rules cannot count sibling processes in an
embedded host, so set it in any multi-process embed).
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Literal, get_args
from uuid import uuid4

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

from tai42_skeleton.app import epoch, instance
from tai42_skeleton.app.epoch import EpochAdmissionApp
from tai42_skeleton.app.sub_mcp_app import SubAppLifespan
from tai42_skeleton.config.config_mode import config_mode

__all__ = ["Transport", "create_app", "lifespan"]

logger = logging.getLogger(__name__)

Transport = Literal["http", "streamable-http", "sse"]

_VALID_TRANSPORTS = get_args(Transport)

# Marker naming the active app's manifest in the one-app guard message when that
# app was created without an explicit ``manifest_path`` (its manifest resolves
# from the environment / config dir at lifespan start).
_ENV_RESOLVED_MARKER = "<env-resolved manifest>"

# One-app-per-process guard. ``_guard_lock`` makes the claim and the release
# atomic so two lifespans entering from different threads cannot both pass the
# check and rebind the process-global handle. ``_app_active`` is the token;
# ``_active_manifest_marker`` names the holder's manifest for the raise message.
_guard_lock = threading.Lock()
_app_active = False
_active_manifest_marker = ""


def create_app(
    manifest_path: str | None = None,
    *,
    transport: Transport = "http",
    stateless_http: bool = False,
) -> Starlette:
    """Build the public ASGI app for a user-owned process.

    One app per process: the returned app's lifespan claims a process-global
    one-app token, and a second lifespan entered while one is active raises. The
    ``transport`` and ``stateless_http`` arguments are validated at call time.

    Args:
        manifest_path: The manifest this app loads. Omit it to let the existing
            environment / config-dir resolution apply.
        transport: The MCP transport to serve.
        stateless_http: Run an http/streamable-http transport in fastmcp's
            stateless mode. Has no ``sse`` equivalent, so pairing it with the
            ``sse`` transport is rejected at call time.

    Returns:
        A Starlette app whose lifespan claims the one-app token, stamps the
        manifest env, builds the app singleton, enters ``app_context`` and the
        inner FastMCP lifespan, and releases the token (restoring the env) on exit.
    """
    if transport not in _VALID_TRANSPORTS:
        raise ValueError(
            f"transport must be one of {', '.join(repr(t) for t in _VALID_TRANSPORTS)}; got {transport!r}."
        )
    if stateless_http and transport == "sse":
        raise ValueError(
            "stateless_http requires an http transport ('http' or 'streamable-http'); "
            "the 'sse' transport has no stateless mode."
        )

    app_state: dict = {}

    @asynccontextmanager
    async def worker_lifespan(_app):
        global _app_active, _active_manifest_marker

        saved_manifest_env: str | None = None

        # Claim the one-app token and stamp the manifest env under the same lock,
        # BEFORE the try whose finally releases the token — a failed claim must
        # raise without touching the active holder's token or environment.
        with _guard_lock:
            if _app_active:
                raise RuntimeError(
                    "a tai app lifespan is already active in this process; the tai42_app handle "
                    "and the built app are process-global singletons (one app per process). "
                    f"The active app's manifest is {_active_manifest_marker}."
                )
            _app_active = True
            _active_manifest_marker = manifest_path if manifest_path is not None else _ENV_RESOLVED_MARKER
            if manifest_path is not None:
                saved_manifest_env = os.environ.get("TAI_MANIFEST_PATH")
                try:
                    os.environ["TAI_MANIFEST_PATH"] = manifest_path
                except Exception:
                    # The stamp raised after the token was claimed (e.g. a manifest
                    # path with an embedded NUL byte). Roll the claim back under the
                    # same lock so a stamp failure never wedges the one-app guard,
                    # then re-raise loudly. The failed assignment left the env
                    # untouched, so there is nothing to restore.
                    _app_active = False
                    _active_manifest_marker = ""
                    raise

        try:
            app = instance.build_app()
            logger.info("Configuration mode: %s", config_mode())
            # Bridge the persisted env store into ``os.environ`` and resolve the
            # manifest under it in one seam, so a store-only ``!ENV ${VAR}`` marker
            # resolves to its real value before ``start()`` probes any mount.
            manifest = app.lifecycle.read_boot_manifest()

            # Initialize the core app context
            async with app.app_context(manifest):
                # Select the appropriate transport mode
                if transport == "sse":
                    logger.info("Initializing Legacy SSE App")
                    inner_app = app.sse_app()
                elif stateless_http:
                    logger.info("Initializing stateless Streamable HTTP App")
                    inner_app = app.http_app(stateless_http=True)
                else:
                    logger.info("Initializing Streamable HTTP App")
                    inner_app = app.http_app()

                # Enter the boot epoch's FastMCP lifespan through a dedicated-task
                # supervisor (not a hand-entered ``async with`` here): the FastMCP
                # lifespan initialises the streamable-http session-manager task group,
                # the mounted dispatch below swallows the lifespan scope, and a profile
                # apply must be able to close THIS lifespan from the swap task — which
                # only the supervisor's one-task/one-context pattern allows.
                # ``finalize`` records the lifespan-bearing FastMCP app as
                # ``mcp_lifespan_app`` so the lifespan is entered even when middleware
                # wraps ``inner_app``.
                boot_epoch = epoch.current_epoch()
                lifespan_app = getattr(inner_app, "mcp_lifespan_app", inner_app)
                if getattr(lifespan_app, "lifespan", None) is not None:
                    supervisor = SubAppLifespan(lifespan_app)
                    await supervisor.start()
                    boot_epoch.supervisor = supervisor
                # Point the dispatch slot at the boot serving app, wrapped in this
                # generation's admission counter so a later retire drains its work.
                epoch.attach_boot_serving_app(EpochAdmissionApp(inner_app, boot_epoch), app_state)
                yield

        except Exception:
            logger.exception("Worker application lifespan failed")
            raise
        finally:
            # The serving generation (and its FastMCP lifespan) is dropped by
            # ``app_context`` on its own exit, above.
            # Release the token and restore the manifest env under the same lock
            # so a failed boot never wedges the process and a later no-param app
            # resolves the config-dir default rather than this app's path.
            with _guard_lock:
                if manifest_path is not None:
                    if saved_manifest_env is not None:
                        os.environ["TAI_MANIFEST_PATH"] = saved_manifest_env
                    else:
                        os.environ.pop("TAI_MANIFEST_PATH", None)
                _app_active = False
                _active_manifest_marker = ""

    async def dispatch(scope, receive, send):
        """Forward requests to the inner app."""
        # The outer Starlette app owns the lifespan (worker_lifespan). If a
        # server delivers a lifespan scope to this mounted sub-app anyway,
        # swallow it here: the request paths below speak HTTP and would emit an
        # invalid response against a lifespan scope.
        if scope["type"] == "lifespan":
            return

        if "app" in app_state:
            try:
                await app_state["app"](scope, receive, send)
            except Exception as exc:
                # A normal API exception is caught by the base app's own
                # ServerErrorMiddleware, which commits the generic {"error",
                # "error_id"} envelope and stamps that id on the exception before
                # re-raising it here. When that id is present the response is already
                # sent and logged under it — do not re-log or attempt a second send.
                handled_id = getattr(exc, "error_id", None)
                if handled_id is not None:
                    return
                # Backstop for an exception raised OUTSIDE the base app's handler (an
                # outer finalize wrapper, or dispatch itself): mint a correlation id,
                # log the traceback under it, and commit the same generic envelope.
                error_id = uuid4().hex
                logger.exception("Error processing request in mcp app [error_id=%s]", error_id)
                try:
                    # Fixed generic body: internal exception text (hosts,
                    # paths) must never reach the client.
                    response = JSONResponse({"error": "Internal Server Error", "error_id": error_id}, status_code=500)
                    await response(scope, receive, send)
                except RuntimeError:
                    # The response already started before the failure — nothing
                    # more can be sent on this connection. The original error is
                    # logged above; record the double-fault too.
                    logger.warning(
                        "could not send the 500 response; response already started [error_id=%s]",
                        error_id,
                        exc_info=True,
                    )
        else:
            response = JSONResponse({"error": "Service Unavailable", "detail": "Initializing..."}, status_code=503)
            await response(scope, receive, send)

    return Starlette(lifespan=worker_lifespan, routes=[Mount("/", app=dispatch)])


def lifespan(app: Starlette):
    """Return a ``create_app`` app's lifespan context manager, for composing into a
    host lifespan when the app is mounted.

    Mounting the factory app is not enough on its own: Starlette does not run a
    mounted sub-app's lifespan, so the host must run it. Enter the returned context
    manager inside the host's own lifespan so the tai app's worker startup (manifest
    load, ``app_context``, transport selection, the inner FastMCP lifespan) runs for
    the host process's lifetime.

    Args:
        app: A ``create_app`` result whose lifespan the host runs.

    Returns:
        The app's lifespan context manager (``app.router.lifespan_context(app)``),
        to be entered inside the host lifespan.
    """
    return app.router.lifespan_context(app)
