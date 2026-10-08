"""The embed deployment shape served under a path prefix: a host-owned ASGI process
that mounts the tai app at ``/mnt``.

The same host as :mod:`tai42_e2e_fixtures.embed_main` — ``create_app()`` with the
manifest from ``TAI_MANIFEST_PATH``, its lifespan entered through the
``tai42_skeleton.asgi.lifespan`` helper — but ``host.mount("/mnt", tai)``: every tai
path (``/mnt/health``, ``/mnt/api/*``, ``/mnt/mcp``) is served under the prefix, and the
mounted app sees ``root_path="/mnt"`` on every request. Serve it with
``uvicorn tai42_e2e_fixtures.embed_prefix_main:app``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.applications import Starlette
from tai42_skeleton.asgi import create_app, lifespan

# The prefix the tai app is mounted under; a stack serving this host sets the same
# ``path_prefix`` so the harness reaches the app beneath it.
MOUNT_PREFIX = "/mnt"

tai: Starlette = create_app()


@asynccontextmanager
async def host_lifespan(_host: FastAPI) -> AsyncIterator[None]:
    """Run the mounted tai app's lifespan for the duration of the host's."""
    async with lifespan(tai):
        yield


host = FastAPI(lifespan=host_lifespan)
host.mount(MOUNT_PREFIX, tai)

app = host
