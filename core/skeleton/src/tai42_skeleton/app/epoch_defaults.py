"""The running app's implementations of the serving-epoch build seams.

:func:`~tai42_skeleton.app.epoch.build_and_swap_epoch` and
:func:`~tai42_skeleton.app.epoch.build_and_discard_epoch` take each step as an injectable seam;
these are the defaults they fall back to, each acting on the process's built app
(``instance.app``). The epoch module resolves them on this module at call time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tai42_skeleton.app.admission import EpochAdmissionApp

if TYPE_CHECKING:
    from starlette.types import ASGIApp

    from tai42_skeleton.app.epoch import Epoch


def default_streamable_http_app() -> ASGIApp:
    """The serving app of a process that attached none of its own: the default streamable-HTTP app."""
    from tai42_skeleton.app import instance

    return instance.app.http_app()


def default_rebuild() -> None:
    """Build the fresh serving core off to the side and re-initialise the process registries into it.

    Runs under the ALREADY applied proposed env (the primitive applied it before
    calling this). A fresh ``ServingCore`` (fresh FastMCP under a freshly-read AuthAdapter) is set
    on the app's ``_building`` slot, so ``start()`` and the epoch handlers register
    into it — the live core is NEVER touched. The env is NOT re-read from the store: the
    proposed env is live in ``os.environ`` and the settings caches were cleared, so this
    resolves every settings read under the env about to be persisted. On ANY
    failure the half-built core is discarded (``_building`` dropped) and re-raised, so
    the live epoch keeps serving untouched.
    """
    from tai42_skeleton.app import instance
    from tai42_skeleton.manifest import Manifest

    app = instance.app
    app._building = app._build_serving_core()
    try:
        manifest = Manifest.model_validate(app.config.config_manager.read_manifest())
        app.lifecycle.reload_registries(manifest)
    except BaseException:
        # Discard the half-built core so every read resolves back to the live epoch.
        app._building = None
        raise


async def default_build_serving_app(epoch: Epoch) -> ASGIApp:
    """Build this generation's fresh dispatch handle off the just-built core and enter its FastMCP lifespan.

    A fresh serving app on the process's transport is built off the ``_building`` core's
    fresh FastMCP, so its route table — including a reload-added router — is snapshotted
    anew and actually serves. Its FastMCP lifespan (a fresh session manager) is entered
    through a dedicated-task supervisor so the swap task can later close it in the same
    context; the built core (the retire drops it) and, weakly, the lifespan-bearing app
    (the serving surface every request's scope points at) are recorded on the epoch, and
    ``_building`` is cleared so post-swap reads resolve through ``current_epoch()``.
    """
    from tai42_skeleton.app import instance
    from tai42_skeleton.app.epoch import serving_app_builder
    from tai42_skeleton.app.sub_mcp_app import SubAppLifespan

    app = instance.app
    core = app._building
    if core is None:
        raise RuntimeError("no core was built for this epoch — the rebuild step must run first")
    build_inner = serving_app_builder()
    try:
        inner = build_inner()
        lifespan_app = getattr(inner, "mcp_lifespan_app", inner)
        supervisor = SubAppLifespan(lifespan_app)
        # Enter the fresh FastMCP lifespan (fresh session-manager task group); a
        # lifespan-enter failure has already unwound itself when start() re-raises.
        await supervisor.start()
        epoch.core = core
        epoch.supervisor = supervisor
        epoch.record_serving_surface(lifespan_app)
        return EpochAdmissionApp(inner, epoch)
    finally:
        app._building = None


async def default_establish_background_loops() -> None:
    """Re-establish the swapped-in generation's loop-affine background loops on the serving loop.

    Covers the advisories poll and the conversations delivery sweep, registering
    each with the now-current epoch so it retires with the generation. The
    per-epoch handlers ran on a throwaway build-thread loop and could not spawn a
    loop that survives the build, so these are (re)started here instead. Loud but
    non-fatal on a single establisher's failure — the fresh epoch already serves.
    """
    from tai42_skeleton.app import instance

    await instance.app._run_post_swap_handlers(raise_on_error=False)


async def default_bring_credentials_up_to_date() -> None:
    """The live app's credentials step: one reload probe budget, raising nothing, idle with no live refresh owner."""
    from tai42_skeleton.app import instance

    await instance.app._bring_listed_credentials_up_to_date()
